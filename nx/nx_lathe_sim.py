"""
nx_lathe_sim.py — ТОКАРНАЯ симуляция G-Code на виртуальном станке NX ISV.

Хостовая часть, токарный аналог nx_sim.py:
  1. G-Code → .mpf под стойку Sinumerik: комментарии `(...)` стойка не понимает,
     G21 не знает; наш код УЖЕ токарный (G18, X в диаметрах, G95/G97), поэтому
     переписывать движения не нужно — только вычистить;
  2. TO_INI.SPF — таблица инструментов стойки. У токарного станка в комплекте
     её НЕТ (в subprog лежат CHAN_DATA.def, ToolChange.SPF, файл циклов), но
     ToolChange.SPF читает $TC_TP1, поэтому таблицу надо положить. Для РЕЗЦА
     она другая, чем для фрезы: тип 500 (токарный) вместо 120, и обязательно
     $TC_DP2 — положение режущей кромки;
  3. nx_lathe_sim_journal.py в полном NX (ugraf -auto) — сборка проекта,
     станок, прогон CSE со съёмом, SaveAsPartfile;
  4. IPW → STEP тем же батч-журналом, что и у фрезеровки.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
from nx import nx_export, nx_sim

_JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "nx_lathe_sim_journal.py")
_EXPORT_JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "nx_sim_export_journal.py")

# Положение режущей кромки (Schneidenlage) для наружного точения справа
# налево. Значения 1..9; 3 — стандарт для правого наружного резца.
INSERT_POSITION_OD_RIGHT = 3


def _log(msg):
    print(f"[lathe-sim] {msg}")


_ROTATE_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_rotate_worker.py")


def _rotate_step(in_path, out_path, axis=(0.0, 1.0, 0.0), angle=90.0):
    """Повернуть BREP-STEP (деталь/заготовку) в другую раму через freecadcmd.

    Нужно, чтобы посадить тело на физическую ось шпинделя NX-станка. OCCT не
    пишет в неASCII-пути → результат идёт в ASCII-temp и копируется на место.
    Только для BREP-солидов (фасетный результат NX так крутить нельзя).
    """
    from cam.freecad_cam import find_freecadcmd
    fc = find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден — поворот в раму станка "
                           "невозможен (укажите FREECAD_CMD в конфиге)")
    # OCCT не читает неASCII-пути И спотыкается о 8.3-имя с усечённым расширением
    # (.step → .STE = «unknown extension»). Поэтому вход копируем в ASCII-temp с
    # СОХРАНЁННЫМ расширением, а не подставляем 8.3-короткое имя.
    ext = os.path.splitext(in_path)[1] or ".step"
    tmp_in = os.path.join(tempfile.gettempdir(),
                          f"roti_{os.getpid()}_{abs(hash(in_path)) % 100000}{ext}")
    shutil.copyfile(in_path, tmp_in)
    tmp_out = os.path.join(tempfile.gettempdir(),
                           f"rot_{os.getpid()}_{abs(hash(out_path)) % 100000}.step")
    params = {"in": tmp_in, "out": tmp_out,
              "axis": list(axis), "angle": float(angle)}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        pp = tmp.name
    # freecadcmd не откроет сам воркер по неASCII-пути (репо под ...\Работа\...)
    # — копируем его в TEMP, как это делает журнал станка
    worker_tmp = os.path.join(tempfile.gettempdir(), "_lathe_rotate_worker.py")
    shutil.copyfile(_ROTATE_WORKER, worker_tmp)
    try:
        r = subprocess.run([fc, worker_tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "ROTATE_PARAMS": pp,
                                "QT_QPA_PLATFORM": "offscreen"}, timeout=180)
    finally:
        for f in (pp, tmp_in):
            try:
                os.unlink(f)
            except OSError:
                pass
    if "[rotate] OK" not in (r.stdout or "") or not os.path.exists(tmp_out):
        raise RuntimeError("поворот STEP не удался: "
                           + ((r.stdout or "") + (r.stderr or ""))[-300:])
    shutil.copyfile(tmp_out, out_path)
    try:
        os.unlink(tmp_out)
    except OSError:
        pass
    return out_path


def gcode_to_mpf(gcode_path, mpf_path, tool_number=1):
    """Токарный G-Code → .mpf для Sinumerik. В отличие от фрезерного варианта
    движения НЕ переписываются: наш код уже в G18 с диаметральным X и G95/G97.
    Убираем только то, чего стойка не понимает."""
    out = []
    with open(gcode_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = nx_sim._strip_comments(raw)
            if not line:
                continue
            words = line.upper().split()
            if "G21" in words:                       # Sinumerik G21 не знает
                line = " ".join(w for w in line.split() if w.upper() != "G21")
                if not line:
                    continue
            if words in (["M2"], ["M02"]):
                line = "M30"
            out.append(line)
    if out and out[-1].upper() != "M30":
        out.append("M30")
    with open(mpf_path, "w", encoding="ascii", errors="replace",
              newline="\r\n") as f:
        f.write("\n".join(out) + "\n")
    return len(out)


def write_to_ini(machine_dir, nose_radius, tool_number=1,
                 insert_position=INSERT_POSITION_OD_RIGHT,
                 length_x=0.0, length_z=0.0):
    """TO_INI.SPF для ТОКАРНОГО резца.

    Отличия от фрезы (см. nx_sim.write_to_ini): $TC_DP1 = 500 — тип «токарный
    инструмент» вместо 120 («концевая фреза»); $TC_DP2 — положение режущей
    кромки, для точения это не формальность, а то, с какой стороны стойка
    считает вершину; $TC_DP6 — радиус ПРИ ВЕРШИНЕ (у фрезы там радиус фрезы).
    """
    sub = os.path.join(machine_dir, "cse_driver", "sinumerik", "subprog")
    content = (
        f'$TC_TP1[{tool_number}]={tool_number}\n'
        f'$TC_TP2[{tool_number}]="TURN_OD"\n'
        f'$TC_DP1[{tool_number},1]=500\n'
        f'$TC_DP2[{tool_number},1]={insert_position}\n'
        f'$TC_DP3[{tool_number},1]={length_x:g}\n'
        f'$TC_DP4[{tool_number},1]={length_z:g}\n'
        f'$TC_DP6[{tool_number},1]={nose_radius:g}\n'
        f'M17\n'
    )
    path = os.path.join(sub, "TO_INI.SPF")
    try:
        if os.path.exists(path):
            with open(path, encoding="ascii", errors="replace") as f:
                if f.read() == content:
                    return path
        os.makedirs(sub, exist_ok=True)
        with open(path, "w", encoding="ascii", newline="\r\n") as f:
            f.write(content)
        _log(f"TO_INI.SPF записан (T{tool_number}, резец, вершина R{nose_radius:g}, "
             f"кромка {insert_position})")
    except PermissionError:
        _log(f"warn: нет прав записи в {path} — таблица инструментов стойки "
             f"могла устареть. Дайте себе права на папку станка "
             f"(icacls ... /grant \"%USERNAME%:(OI)(CI)M\").")
    return path


def simulate(gcode_path, stock_step_path, out_stem=None, nose_radius=0.4,
             machine=None, part_step=None):
    """Прогоняет токарный G-Code на виртуальном станке NX ISV.

    part_step (опц.) — эталонная деталь: её повернут в ту же раму станка и
    вернут как `part_ref`, чтобы результат и эталон накладывались друг на друга.
    """
    base = nx_export.find_nx_base()
    if not base:
        raise RuntimeError("Siemens NX не найден — симуляция недоступна")
    machine = machine or getattr(config, "NX_LATHE_MACHINE",
                                 "sim11_turn_2ax_sinumerik")
    if "sinumerik" not in machine:
        raise RuntimeError(f"NX_LATHE_MACHINE={machine!r}: подготовка программы и "
                           f"таблица инструментов написаны под Sinumerik")
    mdir = nx_sim.find_machine_dir(machine)
    if not mdir:
        raise RuntimeError(f"станок {machine!r} не найден в библиотеке NX")
    if not os.path.exists(stock_step_path):
        raise RuntimeError(f"файл заготовки не найден: {stock_step_path}")

    write_to_ini(mdir, nose_radius)

    if out_stem is None:
        out_stem = os.path.splitext(os.path.abspath(gcode_path))[0]
    out_step = out_stem + "_nxsim.stp"
    tdir = tempfile.gettempdir()
    stem = os.path.basename(out_stem)
    mpf_path = os.path.join(tdir, f"{stem}_lathe.mpf")
    work_prt = os.path.join(tdir, f"{stem}_lathe.prt")
    tmp_step = os.path.join(tdir, f"{stem}_lathe.stp")
    for f in ([work_prt, tmp_step]
              + glob.glob(os.path.join(tdir, f"{stem}_lathe*_ipw.prt"))):
        if os.path.exists(f):
            os.unlink(f)

    n = gcode_to_mpf(gcode_path, mpf_path)
    _log(f"программа для стойки: {n} строк → {os.path.basename(mpf_path)}")

    from cam.freecad_cam import _ascii_safe

    # Посадить заготовку (и эталон) на ФИЗИЧЕСКУЮ ось шпинделя станка. Наш
    # пайплайн канонизирует деталь осью на Z (профиль R(z), G-код Z=ось), а у
    # sim11 шпиндель идёт вдоль мирового X — замерено зондом: MCS_MAIN_SPINDLE
    # Zaxis=(1,0,0), патрон в (−76.2,0,0). Без поворота шпиндель крутит тело
    # вокруг чужой оси → несимметричная стружка. Крутим тело Z→X (+90° вокруг
    # Y); G-код НЕ трогаем — его Z остаётся логической осью, стойка сама мапит
    # Z→ось станка. Только BREP-солиды (фасетный результат так крутить нельзя).
    do_rotate = getattr(config, "NX_LATHE_ROTATE_TO_SPINDLE", True)
    rot_axis = tuple(getattr(config, "NX_LATHE_SPINDLE_ROT_AXIS", (0.0, 1.0, 0.0)))
    rot_angle = float(getattr(config, "NX_LATHE_SPINDLE_ROT_ANGLE", 90.0))
    part_ref = ""
    if do_rotate:
        stock_for_nx = os.path.join(tdir, f"{stem}_stock_nx.stp")
        _rotate_step(stock_step_path, stock_for_nx, rot_axis, rot_angle)
        _log("заготовка повёрнута на ось шпинделя станка (Z→X)")
        if part_step and os.path.exists(part_step):
            try:
                part_ref = out_stem + "_part_nx.step"
                _rotate_step(part_step, part_ref, rot_axis, rot_angle)
                _log("эталон повёрнут в раму станка (для наложения/сверки)")
            except Exception as e:
                _log(f"warn: эталон в раму станка не повёрнут: {e}")
                part_ref = ""
    else:
        stock_for_nx = stock_step_path

    log_path = os.path.join(tdir, f"{stem}_lathe_journal.log")
    if os.path.exists(log_path):
        os.unlink(log_path)
    params = {
        "stock_step": _ascii_safe(os.path.abspath(stock_for_nx)),
        "mpf": mpf_path,
        "machine": machine,
        "nose_radius": nose_radius,
        "tool_number": 1,
        "work_prt": work_prt,
        "log_path": log_path,
        "sim_timeout": max(60, getattr(config, "NX_SIM_TIMEOUT", 1800) - 300),
        "startup_grace": getattr(config, "NX_LATHE_STARTUP_GRACE", 90),
        # Постановка детали на станок. Перебор всех пяти способов показал:
        # съём идёт ТОЛЬКО при KeepAssemblyConstraints (IPW 470 КБ против
        # ~62 КБ у прочих, где сохраняется нетронутый пруток). Деталь у нас
        # уже приведена в координаты программы, и станок надо строить
        # вокруг неё, а не переставлять её под станок.
        "positioning": getattr(config, "NX_LATHE_POSITIONING",
                               "KeepAssemblyConstraints"),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    journal = os.path.join(tdir, "nx_lathe_sim_journal.py")
    shutil.copyfile(_JOURNAL, journal)
    ugraf = os.path.join(base, "NXBIN", "ugraf.exe")
    _log("запускаю NX (появится окно, трогать не нужно)...")
    t0 = time.perf_counter()
    proc = subprocess.Popen([ugraf, f"-auto={journal}"],
                            env={**os.environ, "NX_LATHE_SIM_PARAMS": params_path})
    machine_time, ipw_prt = "", ""
    try:
        done = nx_sim._wait_marker(log_path, proc, "DONE",
                                   timeout=getattr(config, "NX_SIM_TIMEOUT", 1800),
                                   prefix="[nxlathe]")
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
        raise RuntimeError(f"IPW результата не найден ({ipw_prt or 'н/д'}) — "
                           f"см. лог: {log_path}")
    sim_wall = time.perf_counter() - t0
    _log(f"прогон завершён за {sim_wall:.0f} с (машинное время "
         f"{machine_time or 'н/д'}), IPW: {os.path.basename(ipw_prt)}")

    # IPW → STEP тем же батч-журналом, что у фрезеровки
    exp_params = {"prt": ipw_prt, "out_step": tmp_step, "min_triangles": 50}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(exp_params, tmp)
        exp_params_path = tmp.name
    rj = os.path.join(base, "NXBIN", "run_journal.exe")
    t1 = time.perf_counter()
    try:
        eproc = subprocess.run(
            [rj, _EXPORT_JOURNAL], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env={**os.environ, "NX_SIM_EXPORT_PARAMS": exp_params_path},
            timeout=600)
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
        raise RuntimeError(f"экспорт результата не удался (код {eproc.returncode}). {tail}")
    triangles = ""
    m = re.search(r"triangles=(\d+)", eok)
    if m:
        triangles = m.group(1)

    shutil.move(tmp_step, out_step)
    out_prt = out_stem + "_nxsim.prt"
    try:
        shutil.copyfile(ipw_prt, out_prt)
    except OSError:
        out_prt = ""
    _log(f"реальное время: прогон {sim_wall:.0f} с + экспорт "
         f"{time.perf_counter() - t1:.0f} с")
    return {"step": out_step, "prt": out_prt, "machine_time": machine_time,
            "triangles": triangles, "part_ref": part_ref}
