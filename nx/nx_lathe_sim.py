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
import math
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
_SPINDLE_PROBE_JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "nx_spindle_probe_journal.py")
_SPINDLE_MEM = {}   # кэш оси шпинделя на процесс: machine -> [x,y,z]

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


def spindle_axis(machine, sample_step):
    """Ось шпинделя станка в МИРОВЫХ координатах [x,y,z] или None.

    Разовый headless-зонд NX (кэш на процесс + на диск в папке станка), чтобы
    поворот детали на шпиндель считался автоматически, а не был зашит под
    конкретный станок. sample_step — любое тело, нужно лишь чтобы у зонда был
    рабочий part; результат от детали не зависит.
    """
    if machine in _SPINDLE_MEM:
        return _SPINDLE_MEM[machine]
    mdir = nx_sim.find_machine_dir(machine)
    cache = os.path.join(mdir, "spindle_axis.json") if mdir else None
    if cache and os.path.exists(cache):
        try:
            ax = json.load(open(cache, encoding="utf-8")).get("spindle_axis")
            if ax and len(ax) == 3:
                _SPINDLE_MEM[machine] = ax
                return ax
        except (OSError, ValueError):
            pass
    base = nx_export.find_nx_base()
    if not base or not sample_step or not os.path.exists(sample_step):
        return None
    from cam.freecad_cam import _ascii_safe
    tdir = tempfile.gettempdir()
    out_json = os.path.join(tdir, f"spindle_{abs(hash(machine)) % 100000}.json")
    if os.path.exists(out_json):
        os.unlink(out_json)
    params = {
        "stock_step": _ascii_safe(os.path.abspath(sample_step)),
        "machine": machine,
        "work_prt": os.path.join(tdir, "spindle_probe.prt"),
        "out_json": out_json,
        "log_path": os.path.join(tdir, "spindle_probe.log"),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        pp = tmp.name
    journal = os.path.join(tdir, "nx_spindle_probe_journal.py")
    shutil.copyfile(_SPINDLE_PROBE_JOURNAL, journal)
    rj = os.path.join(base, "NXBIN", "run_journal.exe")
    _log("определяю ось шпинделя станка (разовый зонд NX)...")
    try:
        subprocess.run([rj, journal], capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "NX_SPINDLE_PROBE": pp}, timeout=300)
    except Exception as e:
        _log(f"зонд оси шпинделя не удался: {e}")
    finally:
        try:
            os.unlink(pp)
        except OSError:
            pass
    if not os.path.exists(out_json):
        return None
    try:
        ax = json.load(open(out_json, encoding="utf-8")).get("spindle_axis")
    except (OSError, ValueError):
        return None
    if not ax or len(ax) != 3:
        return None
    _SPINDLE_MEM[machine] = ax
    if cache:
        try:
            shutil.copyfile(out_json, cache)   # нет прав на папку станка — ок,
        except OSError:                        # просто не кэшируем на диск
            pass
    return ax


def _rotation_to_spindle(spindle):
    """Поворот (axis, angle°), переводящий ось детали +Z на ось шпинделя.

    axis = нормаль (Z × spindle), angle = угол между Z и spindle. Для шпинделя
    вдоль X даёт (0,1,0)/90°. Коллинеарные случаи: 0° или 180°.
    """
    sx, sy, sz = (float(v) for v in spindle)
    n = math.sqrt(sx * sx + sy * sy + sz * sz) or 1.0
    sx, sy, sz = sx / n, sy / n, sz / n
    dot = max(-1.0, min(1.0, sz))               # (0,0,1)·spindle
    angle = math.degrees(math.acos(dot))
    ax = (-sy, sx, 0.0)                          # (0,0,1) × spindle
    axn = math.sqrt(ax[0] ** 2 + ax[1] ** 2 + ax[2] ** 2)
    if axn < 1e-9:                              # уже коллинеарны оси Z
        return (1.0, 0.0, 0.0), (0.0 if dot > 0 else 180.0)
    # + 0.0 нормализует -0.0 -> 0.0 (косметика лога)
    return (ax[0] / axn + 0.0, ax[1] / axn + 0.0, ax[2] / axn + 0.0), angle


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


def tools_from_gcode(path, nose_radius=0.4,
                     insert_position=INSERT_POSITION_OD_RIGHT):
    """Список инструментов, вычитанный ИЗ САМОЙ ПРОГРАММЫ.

    В таблице стойки обязан быть КАЖДЫЙ номер T, встречающийся в коде: смена
    инструмента виснет, если ToolChange.SPF читает $TC_TP1 несуществующего
    номера. Собирать таблицу из аргументов — значит каждый раз забывать про
    новый инструмент (так в неё не попали сверло, расточной и резьбовой, когда
    их добавили в генератор). Здесь она не может разойтись с программой по
    построению: номера берутся из строк `T<n>`, назначение — из комментария
    `(Tool T<n>: ...)`, который генератор пишет рядом.

    Типы инструментов Sinumerik: 200 спиральное сверло, 220 центровочное,
    500 черновой резец, 510 чистовой, 520 канавочный/отрезной, 540 резьбовой.
    """
    tools, cur = {}, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            l = raw.strip()
            m = re.match(r"^T(\d+)\s*$", l)
            if m:
                cur = int(m.group(1))
                tools.setdefault(cur, {"n": cur, "name": "TURN_OD", "type": 500,
                                       "pos": insert_position,
                                       "nose": nose_radius})
                continue
            m = re.match(r"\(Tool(?:\s+T\d+)?\s*:\s*(.+?)\)\s*$", l)
            if not m or cur is None:
                continue
            desc = m.group(1)
            t = tools[cur]
            d = re.search(r"\bD([\d.]+)", desc)
            w = re.search(r"\bW([\d.]+)", desc)
            r = re.search(r"nose R([\d.]+)", desc)
            low = desc.lower()
            if "center drill" in low:
                t.update(name="CENTER_DRILL", type=220, nose=0.0)
            elif "drill" in low:
                t.update(name="DRILL", type=200, nose=0.0)
            elif "grooving" in low or "parting" in low or "groove" in low:
                # «groove» — ради ЧУЖИХ программ: заводской резец назван
                # T05_OD_GROOVE_L_..., наши операции пишут «grooving/parting»
                t.update(name="GROOVE", type=520, nose=0.0)
            elif "threading" in low:
                t.update(name="THREAD", type=540, nose=0.0)
            elif "boring" in low:
                t.update(name="BORE", type=500, nose=0.0)
            elif "left-hand" in low:
                # у левого другая Schneidenlage — стойка иначе считает вершину
                t.update(name="TURN_OD_L", pos=4)
            elif "finishing" in low:
                t.update(name="TURN_OD_FIN", type=510)
            if r:
                t["nose"] = float(r.group(1))
            if d and t["type"] in (200, 220):
                t["nose"] = float(d.group(1))       # у сверла $TC_DP6 — диаметр
            if w:
                t["width"] = float(w.group(1))
    return [tools[k] for k in sorted(tools)]


def write_to_ini(machine_dir, nose_radius, tool_number=1,
                 insert_position=INSERT_POSITION_OD_RIGHT,
                 length_x=0.0, length_z=0.0,
                 groove_width=0.0, groove_tool_number=2,
                 left_tool_number=0, extra_tools=None, tools=None):
    """TO_INI.SPF для ТОКАРНЫХ резцов.

    Отличия от фрезы (см. nx_sim.write_to_ini): $TC_DP1 = 500 — тип «токарный
    инструмент» вместо 120 («концевая фреза»); $TC_DP2 — положение режущей
    кромки, для точения это не формальность, а то, с какой стороны стойка
    считает вершину; $TC_DP6 — радиус ПРИ ВЕРШИНЕ (у фрезы там радиус фрезы).

    Канавочный идёт типом 520 («отрезной/канавочный»), его ширина — $TC_DP7.

    **В таблице обязан быть КАЖДЫЙ номер T, который встречается в программе.**
    Без записи смена инструмента виснет: ToolChange.SPF читает $TC_TP1
    несуществующего номера. Поэтому таблица собирается СПИСКОМ, а не тремя
    фиксированными блоками — добавить четвёртый резец теперь значит добавить
    элемент списка, а не ещё одну ветку.

    `extra_tools` — [{"n": номер, "name": "...", "type": 500, "pos": 3,
                      "nose": 0.4, "width": 0.0}] — дописываются как есть.
    """
    sub = os.path.join(machine_dir, "cse_driver", "sinumerik", "subprog")
    nl = "\n"

    if tools:                       # готовый список (обычно из tools_from_gcode)
        tools = [dict(t) for t in tools]
        if length_x or length_z:
            # ВЫЛЕТ РЕЗЦА в таблице стойки — то же самое, что замер инструмента
            # на приборе у настоящего станка: он сообщает стойке, ГДЕ реально
            # лежит вершина относительно точки привязки резцедержателя. У нас
            # он по умолчанию нулевой, то есть мы утверждаем «вершина ровно в
            # точке привязки» — а модель пластины в NX имеет свою геометрию
            # относительно той же точки, и привязки расходятся.
            for t in tools:
                if int(t.get("type", 500)) in (500, 510):
                    t["lx"], t["lz"] = length_x, length_z
        for t in (extra_tools or []):
            if t and t.get("n"):
                tools.append(dict(t))
        return _write_to_ini_file(sub, tools, nl)

    tools = [{"n": int(tool_number), "name": "TURN_OD", "type": 500,
              "pos": insert_position, "nose": nose_radius,
              "lx": length_x, "lz": length_z}]
    if groove_width:
        tools.append({"n": int(groove_tool_number), "name": "GROOVE",
                      "type": 520, "pos": insert_position, "nose": 0.0,
                      "width": groove_width})
    if left_tool_number:
        # левый резец: другая Schneidenlage — стойка иначе считает вершину
        tools.append({"n": int(left_tool_number), "name": "TURN_OD_L",
                      "type": 500, "pos": 4, "nose": nose_radius})
    for t in (extra_tools or []):
        if t and t.get("n"):
            tools.append(dict(t))
    return _write_to_ini_file(sub, tools, nl)


def _write_to_ini_file(sub, tools, nl="\n"):
    """Пишет TO_INI.SPF по списку инструментов [{n, name, type, pos, nose, ...}]."""
    content = ""
    seen = set()
    for t in tools:
        n = int(t["n"])
        if n in seen:                      # один номер — одна запись
            continue
        seen.add(n)
        content += (
            f'$TC_TP1[{n}]={n}{nl}'
            f'$TC_TP2[{n}]="{t.get("name", "TURN_OD")}"{nl}'
            f'$TC_DP1[{n},1]={int(t.get("type", 500))}{nl}'
            f'$TC_DP2[{n},1]={int(t.get("pos", INSERT_POSITION_OD_RIGHT))}{nl}'
            f'$TC_DP3[{n},1]={t.get("lx", 0.0):g}{nl}'
            f'$TC_DP4[{n},1]={t.get("lz", 0.0):g}{nl}'
            f'$TC_DP6[{n},1]={t.get("nose", 0.0):g}{nl}'
        )
        if t.get("width"):
            content += f'$TC_DP7[{n},1]={t["width"]:g}{nl}'
    content += "M17" + nl
    path = os.path.join(sub, "TO_INI.SPF")
    try:
        if os.path.exists(path):
            with open(path, encoding="ascii", errors="replace") as f:
                if f.read() == content:
                    return path
        os.makedirs(sub, exist_ok=True)
        with open(path, "w", encoding="ascii", newline="\r\n") as f:
            f.write(content)
        _log("TO_INI.SPF записан: "
             + ", ".join(f"T{t['n']} {t.get('name', '')}"
                         + (f" {t['width']:g} мм" if t.get("width") else "")
                         for t in tools))
    except PermissionError:
        _log(f"warn: нет прав записи в {path} — таблица инструментов стойки "
             f"могла устареть. Дайте себе права на папку станка "
             f"(icacls ... /grant \"%USERNAME%:(OI)(CI)M\").")
    return path


def simulate(gcode_path, stock_step_path, out_stem=None, nose_radius=0.4,
             machine=None, nose_angle=None, insert_size=None,
             relief_angle=None, tool_params=None, groove_width=0.0,
             groove_tool_number=2, groove_params=None,
             left_tool_number=0, finish_tool_number=0, finish_nose_angle=35.0,
             finish_nose_radius=0.4, finish_insert_size=6.35,
             tool_length_x=0.0, tool_length_z=0.0, track_point=None):
    """Прогоняет токарный G-Code на виртуальном станке NX ISV.

    Результат возвращается в раме ДЕТАЛИ (ось Z): заготовку сажают на ось
    шпинделя станка (X) для съёма, затем фасетный результат поворачивают обратно
    на Z прямо в экспортном журнале NX — тогда out_nxsim.stp ложится на
    out_part.step и исходную модель без всяких промежуточных форматов.
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

    # Таблица стойки собирается ИЗ САМОЙ ПРОГРАММЫ: в ней обязан быть каждый
    # номер T, иначе смена инструмента виснет на несуществующем $TC_TP1.
    # Раньше список строился из аргументов, и при добавлении сверла, расточного
    # и резьбового они в таблицу просто не попали.
    tool_list = tools_from_gcode(gcode_path, nose_radius=nose_radius)
    _log("инструменты программы: "
         + ", ".join(f"T{t['n']}({t['name']})" for t in tool_list))
    write_to_ini(mdir, nose_radius, groove_width=groove_width,
                 length_x=tool_length_x, length_z=tool_length_z,
                 groove_tool_number=groove_tool_number,
                 left_tool_number=left_tool_number, tools=tool_list)

    # Свёрла и расточной — ИЗ ТОЙ ЖЕ разметки программы. Раньше журнал их не
    # создавал вовсе, и полная программа установа вставала на первой смене на
    # сверлильный инструмент: станция револьвера пуста. Типы Sinumerik:
    # 200 спиральное сверло, 220 центровочное; у сверла в списке диаметр лежит
    # в поле nose (так его пишет tools_from_gcode).
    # Длина рабочей части сверла: шаблонные 60 мм в ISV выметают деталь
    # целиком (IPW пустой), 3 мм дают нормальный результат. Причина не
    # найдена — тело сверла снимает материал там, где не должно.
    flute = float(getattr(config, "NX_LATHE_DRILL_FLUTE", 3.0))
    drills = [{"n": t["n"], "diameter": t.get("nose") or 0.0, "flute": flute}
              for t in tool_list if t.get("type") in (200, 220)]
    bore_tool_number = next((t["n"] for t in tool_list
                             if t.get("name") == "BORE"), 0)
    # Размер пластины расточного. Шаблонные 6.35 в отверстие Ø11.5 не лезут:
    # ISV режет им деталь пополам. Замерено на 14-31A (зарез к модели, мм³):
    #   6.35 → обточенный конец ОТРЕЗАН      1.15 → 424
    #   2.00 → отверстие r 7.03 вместо 5.75  1.00 → 223, отверстие r 5.82 ✔
    #   0.96 → ДЕТАЛЬ УНИЧТОЖЕНА (11005)
    # Зависимость НЕМОНОТОННАЯ — соседние значения дают то норму, то развал,
    # поэтому формулы по диаметру отверстия здесь нет: стоит единственное
    # проверенное 1.0, менять только замером (NX_LATHE_BORE_SIZE).
    bore_size = float(getattr(config, "NX_LATHE_BORE_SIZE", None) or 1.0)
    if drills:
        _log("свёрла программы: "
             + ", ".join(f"T{d['n']} Ø{d['diameter']:g}" for d in drills))
    if bore_tool_number:
        _log(f"расточной программы: T{bore_tool_number}")

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
    if groove_width:
        # ISV НЕ МОДЕЛИРУЕТ канавочный резец параметрически: подтип OD_GROOVE_L
        # исполняется стойкой (машинное время идёт, смена инструмента проходит),
        # но материал не снимает и IPW не сохраняется вовсе. Проверено в
        # POCKET_01 и POCKET_02, с NoseWidth 0 и 1.5, OrientAngle 180 и 90;
        # проходные подтипы в том же кармане работают. Для съёма канавочным
        # нужна СБОРКА инструмента (Mrshape=Assembly), а не параметры.
        _log("⚠  в программе есть канавочный резец T"
             f"{groove_tool_number}: ISV его параметрически не моделирует, "
             "съём по нему не пойдёт. Для станочной симуляции запускайте "
             "с --no-groove-tool.")

    from cam.freecad_cam import _ascii_safe

    # Посадить заготовку (и эталон) на ФИЗИЧЕСКУЮ ось шпинделя станка. Наш
    # пайплайн канонизирует деталь осью на Z (профиль R(z), G-код Z=ось), а у
    # sim11 шпиндель идёт вдоль мирового X — замерено зондом: MCS_MAIN_SPINDLE
    # Zaxis=(1,0,0), патрон в (−76.2,0,0). Без поворота шпиндель крутит тело
    # вокруг чужой оси → несимметричная стружка. Крутим тело Z→X (+90° вокруг
    # Y); G-код НЕ трогаем — его Z остаётся логической осью, стойка сама мапит
    # Z→ось станка. Только BREP-солиды (фасетный результат так крутить нельзя).
    do_rotate = getattr(config, "NX_LATHE_ROTATE_TO_SPINDLE", True)
    # поворот детали на ось шпинделя: по умолчанию АВТО — зонд читает ось
    # шпинделя станка (кэш на станок) и считает поворот +Z → ось шпинделя;
    # config может переопределить вручную (NX_LATHE_SPINDLE_ROT_AXIS/ANGLE)
    rot_axis = getattr(config, "NX_LATHE_SPINDLE_ROT_AXIS", None)
    rot_angle = getattr(config, "NX_LATHE_SPINDLE_ROT_ANGLE", None)
    if do_rotate and (rot_axis is None or rot_angle is None):
        spindle = spindle_axis(machine, stock_step_path)
        if spindle:
            rot_axis, rot_angle = _rotation_to_spindle(spindle)
            _log(f"ось шпинделя станка {tuple(round(v, 3) for v in spindle)} → "
                 f"поворот детали {rot_angle:.1f}° вокруг "
                 f"{tuple(round(v, 2) for v in rot_axis)}")
        else:
            rot_axis, rot_angle = (0.0, 1.0, 0.0), 90.0
            _log("ось шпинделя зондом не определена — беру дефолт (0,1,0)/90°")
    rot_axis = tuple(rot_axis) if rot_axis is not None else (0.0, 1.0, 0.0)
    rot_angle = float(rot_angle) if rot_angle is not None else 90.0
    if do_rotate:
        stock_for_nx = os.path.join(tdir, f"{stem}_stock_nx.stp")
        _rotate_step(stock_step_path, stock_for_nx, rot_axis, rot_angle)
        _log("заготовка повёрнута на ось шпинделя станка")
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
        # Резец. Подтип задаёт РУКУ (OrientAngle): наша программа точит справа
        # налево, к патрону, значит нужен ПРАВЫЙ (OD_80_R, OrientAngle 95°).
        # С левым (OD_80_L, 5°) кромка идёт впереди хода и режет конус ровно
        # в 5° — это была причина «зареза и конусности» на 4-13A.
        "tool_subtype": getattr(config, "NX_LATHE_TOOL_SUBTYPE", "OD_80_R"),
        "nose_angle": (nose_angle if nose_angle is not None
                       else getattr(config, "LATHE_NOSE_ANGLE", 55.0)),
        "insert_size": (insert_size if insert_size is not None
                        else getattr(config, "LATHE_INSERT_SIZE", 6.35)),
        "orient_angle": getattr(config, "NX_LATHE_ORIENT_ANGLE", None),
        # Точка отслеживания резца — какую точку пластины ISV ставит в
        # запрограммированную координату (см. set_track_point в журнале и
        # runs/80_track_point). Умолчание None = как в шаблоне NX.
        "track_point": (track_point if track_point is not None
                        else getattr(config, "NX_LATHE_TRACK_POINT", None)),
        # Канавочный резец T2. Проходной в канавку не лезет (волочит
        # вспомогательной кромкой по стенке позади), поэтому генератор отдаёт
        # канавки и отрезку отдельному инструменту — надо создать его и в ISV,
        # иначе съём по T2 пойдёт формой проходного.
        "groove_width": float(groove_width or 0.0),
        "groove_tool_number": int(groove_tool_number),
        "groove_subtype": getattr(config, "NX_LATHE_GROOVE_SUBTYPE",
                                  "OD_GROOVE_L"),
        "groove_orient_angle": getattr(config, "NX_LATHE_GROOVE_ORIENT_ANGLE",
                                       None),
        # свёрла (шаблон hole_making) и расточной (внутренний токарный)
        "drills": drills,
        "bore_tool_number": int(bore_tool_number or 0),
        "bore_subtype": getattr(config, "NX_LATHE_BORE_SUBTYPE", "ID_55_L"),
        "bore_insert_size": float(bore_size),
        # левый проходной T3 — зеркальный подтип того же класса
        "left_tool_number": int(left_tool_number or 0),
        "left_subtype": getattr(config, "NX_LATHE_LEFT_SUBTYPE", "OD_55_L"),
        # чистовой T8 — 35°-ромб, острее чернового и глубже заходит в уступы
        "finish_tool_number": int(finish_tool_number or 0),
        "finish_subtype": getattr(config, "NX_LATHE_FINISH_SUBTYPE", "OD_35_R"),
        "finish_nose_angle": float(finish_nose_angle),
        "finish_nose_radius": float(finish_nose_radius),
        "finish_insert_size": float(finish_insert_size),
        "groove_geometry": getattr(config, "NX_LATHE_GROOVE_GEOMETRY", True),
        "groove_params": dict(getattr(config, "NX_LATHE_GROOVE_PARAMS", None)
                              or {}, **(groove_params or {})),
        # Задний угол пластины — в параметрической модели ISV это ЗАДНЯЯ ГРАНЬ,
        # то есть та самая граница инструмента со стороны +Z. Она и определяет,
        # насколько резец волочит по уже обточенной поверхности, когда врезается
        # глубже. Дефолт шаблона 5° соответствует кромке почти вдоль оси; у
        # настоящего 55°-ромба в державке 95° вспомогательный угол в плане 40°.
        "relief_angle": (relief_angle if relief_angle is not None
                         else getattr(config, "LATHE_RELIEF_ANGLE", 40.0)),
        # произвольные свойства TurnToolBuilder для подбора геометрии резца
        "tool_params": dict(getattr(config, "NX_LATHE_TOOL_PARAMS", None) or {},
                            **(tool_params or {})),
        "sim_timeout": max(60, getattr(config, "NX_SIM_TIMEOUT", 1800) - 300),
        "startup_grace": getattr(config, "NX_LATHE_STARTUP_GRACE", 90),
        # сколько секунд без роста машинного времени считать концом программы;
        # смена инструмента с отъездом в точку смены занимает заметную паузу
        "settle": getattr(config, "NX_LATHE_SETTLE", 45),
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

    # IPW → STEP тем же батч-журналом, что у фрезеровки; для токарки просим
    # журнал повернуть фасетный результат ОБРАТНО в раму детали (X→Z) — поворот,
    # обратный посадке заготовки на шпиндель, чтобы STEP лёг на out_part.step
    exp_params = {"prt": ipw_prt, "out_step": tmp_step, "min_triangles": 50}
    if do_rotate:
        exp_params["rot_axis"] = list(rot_axis)
        exp_params["rot_angle"] = -rot_angle
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
            "triangles": triangles}
