"""
nx_export.py — мост Siemens NX: конвертация .prt → STEP (headless).

FreeCAD не читает закрытый формат .prt, но если на машине установлен NX,
экспорт в STEP делается его штатным командным транслятором (лицензия NX
занимается на время конвертации, GUI не открывается):
  AP242 — <NX>\\TRANSLATORS\\step242\\step242.exe
  AP214 — <NX>\\STEP214UG\\step214ug.exe
  AP203 — <NX>\\STEP203UG\\step203ug.exe
Синтаксис (из штатной обёртки step214ug.cmd):
  step242.exe input.prt o=out.stp d=settings.def l=log.txt
Транслятору нужны NXBIN в PATH (иначе 0xC0000135 — DLL не найдены) и
ROSE/ROSE_DB, указывающие на папку транслятора.

ВАЖНО: путь через NXOpen (DexManager.StepCreator в run_journal.exe) на этой
версии выдаёт STEP без геометрии (одни виды/камеры) независимо от ObjectTypes —
поэтому используется именно командный транслятор.
"""

import glob
import os
import subprocess
import tempfile

import sys

# при прямом запуске файла корень репозитория добавляется в sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config

# (подпапка транслятора, exe, def-файл с направлением "UG to STEP")
_TRANSLATORS = {
    "203": (os.path.join("STEP203UG"), "step203ug.exe", "ugstep203.def"),
    "214": (os.path.join("STEP214UG"), "step214ug.exe", "ugstep214.def"),
    "242": (os.path.join("TRANSLATORS", "step242"), "step242.exe", "ugstep242.def"),
}


def _base_candidates():
    """Кандидаты на корень установки NX (UGII_BASE_DIR), в порядке приоритета."""
    out = []
    if getattr(config, "NX_BASE_DIR", ""):
        out.append(config.NX_BASE_DIR)
    if os.environ.get("UGII_BASE_DIR"):
        out.append(os.environ["UGII_BASE_DIR"])
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", "")):
        if root:
            out += sorted(glob.glob(os.path.join(root, "Siemens", "NX*")),
                          reverse=True)
    return out


def find_nx_base() -> str | None:
    """Возвращает корень установки NX или None (критерий — наличие NXBIN)."""
    for c in _base_candidates():
        if c and os.path.isdir(os.path.join(c, "NXBIN")):
            return c
    return None


def available() -> bool:
    base = find_nx_base()
    if not base:
        return False
    ap = str(getattr(config, "NX_STEP_AP", "242"))
    sub, exe, _ = _TRANSLATORS.get(ap, _TRANSLATORS["242"])
    return os.path.exists(os.path.join(base, sub, exe))


def prt_to_step(prt_path: str, step_path: str | None = None,
                ap: str | None = None) -> str:
    """Конвертирует .prt в STEP через NX. Возвращает путь к STEP-файлу.
    step_path=None — файл кладётся во временную папку (имя как у .prt).
    ap=None — версия из конфига (NX_STEP_AP); фасетные тела требуют "242".
    Бросает RuntimeError, если NX недоступен или трансляция не удалась."""
    base = find_nx_base()
    if not base:
        raise RuntimeError("Siemens NX не найден (UGII_BASE_DIR не задан, "
                           "в Program Files\\Siemens пусто) — укажите NX_BASE_DIR "
                           "в конфиге или экспортируйте STEP из NX вручную")
    ap = str(ap or getattr(config, "NX_STEP_AP", "242"))
    if ap not in _TRANSLATORS:
        raise RuntimeError(f"NX_STEP_AP={ap!r} — поддерживаются 203 / 214 / 242")
    sub, exe, deffile = _TRANSLATORS[ap]
    tdir = os.path.join(base, sub)
    translator = os.path.join(tdir, exe)
    if not os.path.exists(translator):
        raise RuntimeError(f"транслятор STEP AP{ap} не найден: {translator}")

    # NX/OCCT-цепочке дальше нужен ASCII-путь; вход конвертируем в 8.3 на месте
    from cam.freecad_cam import _ascii_safe
    prt_path = _ascii_safe(os.path.abspath(prt_path))
    if step_path is None:
        stem = os.path.splitext(os.path.basename(prt_path))[0]
        step_path = os.path.join(tempfile.gettempdir(), f"{stem}_ap{ap}.stp")
    step_path = os.path.abspath(step_path)
    log_path = os.path.splitext(step_path)[0] + ".log"
    for f in (step_path, log_path):
        if os.path.exists(f):
            os.unlink(f)  # транслятор не любит существующие файлы

    env = {**os.environ,
           "PATH": os.pathsep.join([tdir, os.path.join(base, "NXBIN"),
                                    os.environ.get("PATH", "")]),
           "ROSE": tdir + os.sep,
           "ROSE_DB": tdir + os.sep}
    cmd = [translator, prt_path, f"o={step_path}",
           f"d={os.path.join(tdir, deffile)}", f"l={log_path}"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", env=env,
            cwd=tempfile.gettempdir(),  # транслятор пишет времянки в текущую папку
            timeout=getattr(config, "NX_TIMEOUT", 600),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"NX превысил таймаут {getattr(config, 'NX_TIMEOUT', 600)} с "
                           f"(поднимите NX_TIMEOUT в конфиге)")

    if (proc.returncode == 0 and os.path.exists(step_path)
            and os.path.getsize(step_path) > 0):
        return step_path

    tail = [l for l in (proc.stdout or "").splitlines()
            if "ERROR" in l.upper() or "FATAL" in l.upper()]
    if not tail and os.path.exists(log_path):
        with open(log_path, errors="replace") as f:
            tail = [l for l in f if "ERROR" in l.upper()]
    raise RuntimeError(f"NX не сконвертировал .prt в STEP (код {proc.returncode}). "
                       + " ".join(t.strip() for t in tail[-3:])[:500]
                       + (f" Полный лог: {log_path}" if os.path.exists(log_path) else ""))
