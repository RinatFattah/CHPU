"""
nx_sim.py — симуляция G-Code на виртуальном станке NX ISV и захват результата.

Что делает: берёт сгенерированный G-Code и заготовку в координатах программы
(<gcode>_stock.stp, её пишет worker), собирает в NX CAM-проект с виртуальным
станком из библиотеки (по умолчанию sim01_mill_3ax_sinumerik), прогоняет
программу через виртуальную стойку (CSE) со съёмом материала и сохраняет
результат — обработанную заготовку — файлом STEP (фасетное тело) + машинное
время.

Этапы (хостовая часть):
  1. G-Code (grbl) → .mpf под стойку Sinumerik: комментарии `(...)` → Parse
     error — вычищаются (с учётом вложенных скобок); G21 стойка не знает —
     удаляется; смена инструмента — строго `T1` перед `M6`; M2 → M30.
  2. TO_INI.SPF — данные инструмента стойки ($TC_*): без них «Tool 1 not
     defined» и нулевой вылет инструмента. Файл пишется в cse_driver станка
     (Program Files — может требовать прав администратора).
  3. nx_sim_journal.py исполняется в ПОЛНОМ NX (ugraf -auto): сборка проекта,
     станок, прогон со съёмом материала. `SaveAsPartfile=True` заставляет ISV
     САМ сохранить вырезанный IPW отдельным файлом <stem>_..._ipw.prt — это
     единственный надёжный API-путь к результату (кнопка «Создать фасетное
     тело для ЗвПО» программного аналога не имеет, а лента NX не видна UIA;
     см. guide.md). NX закрывается сам по завершении журнала.
  4. IPW-файл → STEP AP242 ED2 batch-журналом (nx_sim_export_journal.py):
     фасетное тело результата выгружается штатным StepCreator.

Запуск через ugraf (а не run_journal): движку съёма CSE нужен живой событийный
цикл GUI — в чистом headless он программу не исполняет. Окно NX на время прогона
появляется на экране, но взаимодействия не требует. Грабли — см. guide.md.
"""

import glob
import json
import os
import re
import subprocess
import tempfile
import time

import sys

# при прямом запуске файла корень репозитория добавляется в sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
from nx import nx_export


def _log(msg):
    print(f"[sim] {msg}")


# ── 1. G-Code → Sinumerik .mpf ────────────────────────────────────────────────────

def _strip_comments(line: str) -> str:
    """Убирает круглые комментарии G-кода с учётом ВЛОЖЕННОСТИ: у FreeCAD в шапке
    есть `(Tool: endmill (flat), ...)` — наивное `\\([^)]*\\)` обрежет до первой
    `)` и оставит мусорный хвост `, ...)` отдельной строкой, на котором стойка
    падает. Считаем глубину скобок и выкидываем всё, что внутри."""
    out, depth = [], 0
    for ch in line:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out).strip()


def gcode_to_mpf(gcode_path: str, mpf_path: str, tool_number: int = 1) -> int:
    """Адаптирует grbl/linuxcnc G-Code под виртуальную стойку Sinumerik (CSE).
    Возвращает число строк .mpf. Правила — из guide.md (часть 4):
      - комментарии в скобках (в т.ч. вся строка) → удаляются (Parse error);
      - G21 (метрическая система) → удаляется (Sinumerik не знает);
      - смена инструмента: `T<n>` строкой ПЕРЕД `M6` (обратный порядок —
        «Tool not defined»); grbl пишет её комментарием `( M6 T1 )` — после
        чистки комментариев вставляем настоящую пару перед первым M3;
      - M2 → M30."""
    out = []
    tool_inserted = False
    with open(gcode_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = _strip_comments(raw)  # комментарии (...), с учётом вложенности
            if not line:
                continue
            words = line.upper().split()
            if "G21" in words:
                line = " ".join(w for w in line.split() if w.upper() != "G21")
                if not line:
                    continue
            if words == ["M2"] or words == ["M02"]:
                line = "M30"
            if not tool_inserted and re.match(r"M0?3\b", line, re.IGNORECASE):
                out.append(f"T{tool_number}")
                out.append("M6")
                tool_inserted = True
            out.append(line)
    if out and out[-1].upper() != "M30":
        out.append("M30")
    with open(mpf_path, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        f.write("\n".join(out) + "\n")
    return len(out)


# ── 2. Станок из библиотеки и данные инструмента стойки ──────────────────────────

def find_machine_dir(machine: str) -> str | None:
    """Папка станка в библиотеке NX по имени из библиотеки
    (sim01_mill_3ax_sinumerik → ...\\installed_machines\\sim01_mill_3ax)."""
    base = nx_export.find_nx_base()
    if not base:
        return None
    lib = os.path.join(base, "MACH", "resource", "library", "machine",
                       "installed_machines")
    hits = glob.glob(os.path.join(lib, "*", f"{machine}.dat"))
    return os.path.dirname(hits[0]) if hits else None


def write_to_ini(machine_dir: str, tool_diameter: float, tool_number: int = 1,
                 tool_length: float = 75.0) -> None:
    """Пишет TO_INI.SPF — таблицу инструментов виртуальной стойки Sinumerik.
    Без неё: «Program name 'TO_INI' not found», «Tool 1 not defined», нулевой
    вылет (корпус шпинделя «лижет» деталь) и невидимый инструмент. Значения:
    DP1=120 — тип «концевая фреза», DP3 — длина, DP6 — РАДИУС."""
    sub = os.path.join(machine_dir, "cse_driver", "sinumerik", "subprog")
    content = (
        f'$TC_TP1[{tool_number}]={tool_number}\n'
        f'$TC_TP2[{tool_number}]="MILL_D{tool_diameter:g}"\n'
        f'$TC_DP1[{tool_number},1]=120\n'
        f'$TC_DP2[{tool_number},1]=1\n'
        f'$TC_DP3[{tool_number},1]={tool_length:g}\n'
        f'$TC_DP6[{tool_number},1]={tool_diameter / 2.0:g}\n'
        f'M17\n'
    )
    path = os.path.join(sub, "TO_INI.SPF")
    try:
        if os.path.exists(path):
            with open(path, encoding="ascii", errors="replace") as f:
                if f.read() == content:
                    return  # уже актуален
        with open(path, "w", encoding="ascii", newline="\r\n") as f:
            f.write(content)
        _log(f"TO_INI.SPF обновлён (T{tool_number} Ø{tool_diameter:g})")
    except PermissionError:
        _log(f"warn: нет прав записи в {path} — таблица инструментов стойки "
             f"могла устареть (нужен Ø{tool_diameter:g}). Запустите один раз "
             f"от администратора или дайте себе права на папку станка.")


# ── 3-4. Запуск журнала симуляции и экспорт результата ────────────────────────────

_JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nx_sim_journal.py")
_EXPORT_JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "nx_sim_export_journal.py")


def simulate(gcode_path: str, stock_step_path: str, out_stem: str | None = None) -> dict:
    """Симулирует G-Code на виртуальном станке NX. Возвращает
    {"step": ..., "prt": ..., "machine_time": ...}. Бросает RuntimeError."""
    base = nx_export.find_nx_base()
    if not base:
        raise RuntimeError("Siemens NX не найден — симуляция недоступна "
                           "(укажите NX_BASE_DIR в конфиге)")
    machine = getattr(config, "NX_SIM_MACHINE", "sim01_mill_3ax_sinumerik")
    if "sinumerik" not in machine:
        raise RuntimeError(f"NX_SIM_MACHINE={machine!r}: поддерживаются станки со "
                           f"стойкой Sinumerik (подготовка программы и таблица "
                           f"инструментов написаны под неё)")
    mdir = find_machine_dir(machine)
    if not mdir:
        raise RuntimeError(f"станок {machine!r} не найден в библиотеке NX "
                           f"(MACH\\resource\\library\\machine\\installed_machines)")
    if not os.path.exists(stock_step_path):
        raise RuntimeError(f"файл заготовки не найден: {stock_step_path} "
                           f"(его пишет генерация G-Code)")

    tool_d = float(config.TOOL_DIAMETER)
    write_to_ini(mdir, tool_d)

    if out_stem is None:
        out_stem = os.path.splitext(os.path.abspath(gcode_path))[0]
    out_step = out_stem + "_sim.stp"
    # рабочие файлы NX — во временной ASCII-папке (кириллица в путях + OCCT/NX)
    tdir = tempfile.gettempdir()
    stem = os.path.basename(out_stem)
    mpf_path = os.path.join(tdir, f"{stem}_sim.mpf")
    work_prt = os.path.join(tdir, f"{stem}_sim.prt")
    tmp_step = os.path.join(tdir, f"{stem}_sim.stp")
    import glob as _glob
    # старые артефакты (в т.ч. прошлые *_ipw.prt) убрать, чтобы не спутать
    for f in [work_prt, tmp_step] + _glob.glob(os.path.join(tdir, f"{stem}_sim*_ipw.prt")):
        if os.path.exists(f):
            os.unlink(f)

    n = gcode_to_mpf(gcode_path, mpf_path, tool_number=1)
    _log(f"программа для стойки: {n} строк → {os.path.basename(mpf_path)}")

    from cam.freecad_cam import _ascii_safe
    log_path = os.path.join(tdir, f"{stem}_sim_journal.log")
    if os.path.exists(log_path):
        os.unlink(log_path)
    params = {
        "stock_step": _ascii_safe(os.path.abspath(stock_step_path)),
        "mpf": mpf_path,
        "machine": machine,
        "tool_diameter": tool_d,
        "tool_number": 1,
        "work_prt": work_prt,
        "log_path": log_path,
        # журналу — запас на ожидание конца прогона (сек), меньше общего таймаута
        "sim_timeout": max(60, getattr(config, "NX_SIM_TIMEOUT", 1800) - 300),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    # ФАЗА 1: прогон в ПОЛНОМ NX через `ugraf -auto=журнал`. Журнал сам
    # прогоняет симуляцию, а SaveAsPartfile=True заставляет ISV сохранить
    # вырезанный IPW отдельным *_ipw.prt; путь к нему журнал пишет маркером
    # DONE и закрывает NX. Никакого взаимодействия не требуется.
    journal = os.path.join(tdir, "nx_sim_journal.py")
    import shutil
    shutil.copyfile(_JOURNAL, journal)
    ugraf = os.path.join(base, "NXBIN", "ugraf.exe")
    _log("запускаю NX (на время прогона появится окно NX; трогать его не нужно)...")
    t_launch = time.perf_counter()
    proc = subprocess.Popen(
        [ugraf, f"-auto={journal}"],
        env={**os.environ, "NX_SIM_PARAMS": params_path},
    )
    machine_time, ipw_prt = "", ""
    try:
        done = _wait_marker(log_path, proc, "DONE",
                            timeout=getattr(config, "NX_SIM_TIMEOUT", 1800))
        m = re.search(r"machine_time=(\S+)", done)
        if m:
            machine_time = m.group(1)
        m = re.search(r"ipw=(.+?)\s+machine_time=", done)
        if m:
            ipw_prt = m.group(1).strip()
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
        except Exception:
            pass
        try:
            os.unlink(params_path)
        except OSError:
            pass

    if not ipw_prt or not os.path.exists(ipw_prt):
        raise RuntimeError(f"IPW-файл результата не найден ({ipw_prt or 'н/д'}) — "
                           f"см. лог: {log_path}")
    sim_wall = time.perf_counter() - t_launch
    _log(f"прогон завершён за {sim_wall:.0f} с реального времени "
         f"(машинное время {machine_time or 'н/д'}), "
         f"IPW: {os.path.basename(ipw_prt)}")

    # ФАЗА 2: batch-экспорт IPW-файла в STEP (фасетное тело результата)
    exp_params = {"prt": ipw_prt, "out_step": tmp_step, "min_triangles": 50}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(exp_params, tmp)
        exp_params_path = tmp.name
    rj = os.path.join(base, "NXBIN", "run_journal.exe")
    t_export = time.perf_counter()
    try:
        eproc = subprocess.run(
            [rj, _EXPORT_JOURNAL],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "NX_SIM_EXPORT_PARAMS": exp_params_path},
            timeout=600,
        )
    finally:
        try:
            os.unlink(exp_params_path)
        except OSError:
            pass
    elines = (eproc.stdout or "").splitlines() + (eproc.stderr or "").splitlines()
    for l in elines:
        if "[nxexp]" in l:
            _log(l.split("[nxexp]", 1)[1].strip())
    eok = next((l for l in elines if "[nxexp] OK" in l), None)
    if not eok or not os.path.exists(tmp_step) or os.path.getsize(tmp_step) == 0:
        tail = "\n".join(l for l in elines if "[nxexp]" in l or "rror" in l)[-500:]
        raise RuntimeError(f"экспорт результата не удался "
                           f"(код {eproc.returncode}). {tail}")
    triangles = ""
    m = re.search(r"triangles=(\d+)", eok)
    if m:
        triangles = m.group(1)

    export_wall = time.perf_counter() - t_export
    shutil.move(tmp_step, out_step)
    # результат также в формате NX (.prt) рядом со STEP
    out_prt = out_stem + "_sim.prt"
    try:
        shutil.copyfile(ipw_prt, out_prt)
        _log(f"результат в .prt → {out_prt}")
    except OSError as e:
        out_prt = ""
        _log(f"warn: копия .prt результата не удалась: {e}")
    _log(f"реальное время: прогон {sim_wall:.0f} с + экспорт {export_wall:.0f} с "
         f"= {sim_wall + export_wall:.0f} с (машинное время {machine_time or 'н/д'})")
    return {"step": out_step, "prt": out_prt, "ipw_prt": ipw_prt,
            "machine_time": machine_time, "triangles": triangles,
            "sim_wall": round(sim_wall, 1), "export_wall": round(export_wall, 1)}


def _wait_marker(log_path: str, proc, marker: str, timeout: float) -> str:
    """Ждёт строку с `marker` в логе журнала; ERROR/смерть NX — исключение."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = []
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            for l in lines:
                if "[nxsim]" in l and marker in l:
                    return l
            if any("ERROR" in l for l in lines):
                raise RuntimeError("журнал симуляции упал:\n" + "\n".join(lines[-8:]))
        if proc.poll() is not None:
            raise RuntimeError(f"NX закрылся до маркера {marker} "
                               f"(код {proc.returncode}). " + "\n".join(lines[-8:]))
        time.sleep(3)
    raise RuntimeError(f"маркер {marker} не появился за {timeout:.0f} с")
