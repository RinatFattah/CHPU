#!/usr/bin/env python3
"""
web/params.py — какие параметры пайплайна показывать в форме и как их проверять.

ДВА ВИДА ОБРАБОТКИ, у каждого свой набор параметров: `mill` (фрезеровка, петля
`auto_fix.py`) и `lathe` (точение, петля `auto_fix_lathe.py`). Разделы у них
намеренно ОДНОИМЁННЫЕ — инструмент, черновая, заготовка, режимы станка,
проверка, петля: технолог читает одну и ту же страницу, меняется только
содержимое. Общего между ними ничего нет, кроме этой формы: разная геометрия
задачи, разный инструмент, разный станок.

Дефолты НЕ дублируются: они читаются из `config.py` в момент запроса, поэтому
форма всегда открывается с теми же значениями, с которыми работает CLI. Здесь
описано только то, ЧТО показывать пользователю, КАК подписать и в каких
границах принимать.

Границы совпадают с белым списком `auto_fix.PARAM_WHITELIST` там, где параметр
есть и в нём: агент в петле не должен иметь права уйти дальше человека.

Чего тут НАМЕРЕННО НЕТ:
  * `DIFF_OK_*` и прочие пороги приёмки фрезеровки — их двигает петля, и давать
    их пользователю в одной форме с параметрами резания значит путать «как
    резать» с «что считать годным». У точения приёмка другая по природе — это
    ДОПУСК ПО РАДИУСУ на чертеже, его технолог задаёт сам (`ok_dr`);
  * `DEAD_ZONES` / `EXTRA_ZONES` / `SKIP_OPS` — это координаты и имена операций,
    их заполняет агент по ходу петли, вручную задавать нечего;
  * `LATHE_THREADS` — резьба из модели не выводится в принципе (CAD несёт
    гладкий цилиндр, шаг знает только чертёж), а объявлять её списком словарей
    в веб-форме — не форма, а YAML;
  * пути (`FREECAD_CMD`, `NX_BASE_DIR`) — машинные настройки, им место в
    config.yaml, а не в веб-форме.
"""

import config

# ── ФРЕЗЕРОВКА ──────────────────────────────────────────────────────────────
# группа → список полей. Порядок групп = порядок на странице.
MILL_SPEC = [
    ("Инструмент", [
        {"name": "TOOL_DIAMETER", "label": "Основная фреза, Ø мм",
         "type": "float", "min": 1.0, "max": 20.0, "step": 0.1,
         "hint": "Заглавная фреза — ей идёт выборка и контур. Обязана быть "
                 "в наборе ниже."},
        {"name": "TOOL_SET", "label": "Набор фрез, Ø мм",
         "type": "floats",
         "hint": "Через запятую, от крупной к мелкой. Воркер назначает каждой "
                 "операции самую крупную, что влезает в фичу."},
    ]),
    ("Черновая обработка", [
        {"name": "ROUGH_MODE", "label": "Режим черновой",
         "type": "choice", "choices": ["stages", "layers"],
         "hint": "stages — по фичам: вырезы → грани сверху вниз → контур. "
                 "layers — послойно (эксперимент)."},
        {"name": "ROUGH_ALLOWANCE_MODE", "label": "Где оставлять припуск",
         "type": "choice", "choices": ["none", "xy", "all"],
         "hint": "none — резать начисто; xy — только по стенкам; all — по "
                 "стенкам и полам."},
        {"name": "ROUGH_ALLOWANCE", "label": "Припуск, мм",
         "type": "float", "min": 0.0, "max": 1.0, "step": 0.1,
         "hint": "Величина припуска. Работает только если режим выше не none."},
        {"name": "ROUGH_STEPDOWN", "label": "Глубина слоя, мм",
         "type": "float", "min": 0.2, "max": 3.0, "step": 0.1},
        {"name": "ROUGH_STEPOVER", "label": "Шаг строчек, % Ø",
         "type": "float", "min": 10, "max": 95, "step": 1,
         "hint": "Плоскости и вырезы. 85 % — фреза берёт почти всю ширину."},
        {"name": "ROUGH_STEPOVER_SLOPE", "label": "Шаг на уклонах, % Ø",
         "type": "float", "min": 10, "max": 60, "step": 1,
         "hint": "Мельче основного: гребешки остаются прямо на поверхности "
                 "детали, а не в припуске."},
        {"name": "ROUGH_TOLERANCE", "label": "Точность траектории, мм",
         "type": "float", "min": 0.05, "max": 0.3, "step": 0.01},
        {"name": "SURFACE_KEEP_INSIDE", "label": "Запирать 3D-проход в границе грани",
         "type": "bool",
         "hint": "Убирает зарез соседней стенки, но вдоль края грани остаётся "
                 "полоска в радиус фрезы."},
    ]),
    ("Безопасность", [
        {"name": "FLOOR_CLEARANCE", "label": "Зазор от стола, мм",
         "type": "float", "min": 0.0, "max": 2.0, "step": 0.1,
         "hint": "Фреза не опускается ниже «дно детали + зазор»: деталь лежит "
                 "на столе. Плёнка этой толщины ломается при съёме."},
        {"name": "SAFE_HEIGHT", "label": "Высота холостых, мм",
         "type": "float", "min": 1.0, "max": 50.0, "step": 1.0},
    ]),
    ("Заготовка", [
        {"name": "STOCK_MARGIN", "label": "Поля вокруг детали, мм",
         "type": "float", "min": 0.0, "max": 50.0, "step": 1.0,
         "hint": "Используется, только если файл заготовки не выбран."},
        {"name": "STOCK_MARGIN_TOP", "label": "Запас сверху, мм",
         "type": "float", "min": 0.0, "max": 50.0, "step": 1.0},
        {"name": "STOCK_ALIGN", "label": "Выровнять заготовку по детали",
         "type": "bool",
         "hint": "Игнорировать координаты файла заготовки и положить её "
                 "«уголок в уголок». Нужно, когда один файл заготовки идёт "
                 "на серию деталей."},
    ]),
    ("Режимы станка", [
        {"name": "FEED_RATE", "label": "Подача, мм/мин",
         "type": "float", "min": 10, "max": 5000, "step": 10},
        {"name": "SPINDLE_SPEED", "label": "Обороты, об/мин",
         "type": "float", "min": 100, "max": 30000, "step": 100},
        {"name": "POSTPROCESSOR", "label": "Диалект G-Code",
         "type": "choice",
         "choices": ["grbl", "linuxcnc", "fanuc", "mach3_mach4", "centroid",
                     "refactored_grbl"]},
    ]),
    ("Система координат", [
        {"name": "ORIGIN", "label": "Ноль программы",
         "type": "choice", "choices": ["corner-top", "center-top", "model"],
         "hint": "corner-top — X0Y0 в углу габарита, Z0 сверху (стандарт ЧПУ). "
                 "model — не сдвигать, ноль как в CAD-файле."},
    ]),
    ("Проверка результата", [
        {"name": "DIFF_PITCH", "label": "Шаг воксельной сетки, мм",
         "type": "float", "min": 0.05, "max": 1.0, "step": 0.05,
         "hint": "Предел разрешения проверки: дефект тоньше шага не виден. "
                 "Мельче 0.1 считается минутами."},
        {"name": "DIFF_MIN_THICKNESS", "label": "Мин. толщина дефекта, мм",
         "type": "float", "min": 0.0, "max": 1.0, "step": 0.05,
         "hint": "Фактически допуск. Тоньше — считается плёнкой фасетизации."},
    ]),
]

# Поля, которые не лежат в config.py, а идут аргументами петли.
MILL_RUN = [
    {"name": "iters", "label": "Максимум итераций петли",
     "type": "int", "min": 1, "max": 10, "step": 1, "default": 3,
     "hint": "Каждая итерация — генерация + симуляция NX + сверка + запрос "
             "к агенту. Считайте по 3–5 минут на итерацию."},
]

# ── ТОЧЕНИЕ ─────────────────────────────────────────────────────────────────
# Главное отличие от фрезеровки — раздел «Инструмент». Там не числа, а НАБОР:
# отмечается, какие инструменты из заводского парка выданы генератору. Роли
# (черновой, чистовой, левый) он раскладывает сам из геометрии выданного, а
# агент в петле меняет ровно этот набор — см. lathe/lathe_tools.py.
LATHE_SPEC = [
    ("Инструмент", [
        {"name": "tools", "label": "Что выдаём генератору", "type": "tools",
         "as_run": True,
         "hint": "Весь список — парк цеха. Отмеченное уходит генератору: он сам "
                 "решит, кто ведёт черновую, кто чистовую, а работу, которую "
                 "делать нечем, не сделает вовсе и вынесет в «не обработано». "
                 "Именно этот набор перебирает агент в петле."},
    ]),
    ("Черновая обработка", [
        {"name": "LATHE_ROUGH_MODE", "label": "Форма черновых слоёв",
         "type": "choice", "choices": ["levels", "contour"],
         "hint": "levels — продольные проходы постоянного диаметра, как у "
                 "завода: каждый обрывается на контуре. contour — эквидистанта "
                 "чистового пути."},
        {"name": "LATHE_DEPTH_OF_CUT", "label": "Глубина резания, мм",
         "type": "float", "min": 0.05, "max": 5.0, "step": 0.005,
         "hint": "По радиусу за проход. 0.465 — снято с заводской программы."},
        {"name": "LATHE_ALLOWANCE", "label": "Припуск на чистовую, мм",
         "type": "float", "min": 0.0, "max": 2.0, "step": 0.05,
         "hint": "По радиусу. Его оставляет черновая, снимает чистовой проход."},
        {"name": "LATHE_SEMI_FINISH", "label": "Получистовой проход",
         "type": "bool",
         "hint": "Один контурный проход со смещением ровно на припуск — "
                 "срезает лестницу, которую уровни оставляют на уклонах."},
        {"name": "LATHE_PRE_FINISH", "label": "Выбирать уступы заранее",
         "type": "bool",
         "hint": "Чистовым резцом, до чистового прохода: иначе в уступах он "
                 "встретит не припуск, а всё, что не достал черновой."},
    ]),
    ("Два установа", [
        {"name": "LATHE_GRIP_LENGTH", "label": "Длина зажима патрона, мм",
         "type": "float", "min": 3.0, "max": 60.0, "step": 1.0,
         "hint": "Короче этого обточенный поясок не даёт перехватить деталь — "
                 "от него зависит, где ляжет граница установов."},
        {"name": "LATHE_SECOND_FACE_ALLOWANCE",
         "label": "Припуск на подрезку торца, мм",
         "type": "float", "min": 0.0, "max": 10.0, "step": 0.5,
         "hint": "Сколько первый установ оставляет за торцом детали: второй "
                 "подрежет его начисто."},
        {"name": "LATHE_SETUP_OVERLAP", "label": "Перекрытие установов, мм",
         "type": "float", "min": 0.0, "max": 10.0, "step": 0.5,
         "hint": "На столько каждый установ заходит за границу — чтобы на шве "
                 "не осталось необработанного пояска."},
    ]),
    ("Отверстие", [
        {"name": "LATHE_BORE_ALLOWANCE", "label": "Припуск под расточку, мм",
         "type": "float", "min": 0.0, "max": 3.0, "step": 0.05,
         "hint": "По радиусу: столько оставляет сверло борштанге."},
        {"name": "LATHE_DRILL_PECK", "label": "Шаг вывода стружки, мм",
         "type": "float", "min": 1.0, "max": 30.0, "step": 1.0},
    ]),
    ("Заготовка", [
        {"name": "LATHE_ALLOWANCE_PER_SIDE", "label": "Припуск на сторону, мм",
         "type": "float", "min": 0.5, "max": 15.0, "step": 0.05,
         "hint": "По нему из ряда ГОСТ 2590 подбирается ближайший больший "
                 "прокат. 3.15 — по заводскому эталону."},
        {"name": "LATHE_PREFER_HEX", "label": "Шестигранный прокат",
         "type": "bool",
         "hint": "Для деталей с гранями под ключ: тогда грани достаются от "
                 "проката. По умолчанию круг — как на заводе, грани отдельной "
                 "фрезерной операцией."},
    ]),
    ("Режимы станка", [
        {"name": "LATHE_SPINDLE_SPEED", "label": "Обороты, об/мин",
         "type": "float", "min": 100, "max": 6000, "step": 100},
        {"name": "LATHE_FEED_PER_REV", "label": "Черновая подача, мм/об",
         "type": "float", "min": 0.02, "max": 1.0, "step": 0.01,
         "hint": "Подача на оборот (G95) — как принято в точении."},
        {"name": "LATHE_FEED_PER_REV_FINISH", "label": "Чистовая подача, мм/об",
         "type": "float", "min": 0.01, "max": 0.5, "step": 0.01},
        {"name": "LATHE_PARTOFF_WIDTH", "label": "Ширина отрезного паза, мм",
         "type": "float", "min": 1.0, "max": 8.0, "step": 0.5},
    ]),
    ("Проверка результата", [
        {"name": "LATHE_DIFF_PITCH", "label": "Шаг воксельной сетки, мм",
         "type": "float", "min": 0.05, "max": 1.0, "step": 0.05,
         "hint": "Предел разрешения проверки: дефект тоньше шага не виден."},
        {"name": "LATHE_DIFF_Z_BIN", "label": "Ширина пояса по z, мм",
         "type": "float", "min": 0.2, "max": 10.0, "step": 0.2,
         "hint": "Отчёт разбивается по оси на пояса такой ширины, в каждом "
                 "считается своё отклонение по радиусу."},
        {"name": "LATHE_DIFF_FILM", "label": "Снимать плёнку симулятора",
         "type": "bool",
         "hint": "Модель резца в NX ISV срезает лишние 0.41·R по радиусу "
                 "равномерно по всей детали — это дефект симулятора, а не "
                 "программы. Выключать только при сверке с настоящим станком."},
    ]),
]

LATHE_RUN = [
    {"name": "iters", "label": "Максимум итераций петли",
     "type": "int", "min": 1, "max": 10, "step": 1, "default": 3,
     "hint": "Итерация — две программы, ДВА прогона в NX ISV, сборка итога, "
             "сверка и запрос к агенту. Считайте по 6 минут на итерацию."},
    {"name": "ok_dr", "label": "Допуск по радиусу, мм",
     "type": "float", "min": 0.01, "max": 1.0, "step": 0.01, "default": 0.12,
     "hint": "ПРИЁМКА: петля останавливается, когда худшее отклонение по "
             "радиусу уложилось в этот допуск. У токаря приёмка — допуск, а не "
             "объём; объём идёт в отчёт как контекст."},
]

# ── ВИДЫ ОБРАБОТКИ ──────────────────────────────────────────────────────────
KINDS = {
    "mill": {
        "title": "Фрезеровка",
        "sub": "3 оси, черновая по фичам",
        "about": "Модель → вырезы → грани сверху вниз → контур. Заготовка — "
                 "файлом или боксом от габарита детали.",
        "spec": MILL_SPEC, "run": MILL_RUN,
        "stock": True,
        "stock_note": "не выбрана: бокс от габарита детали",
    },
    "lathe": {
        "title": "Точение",
        "sub": "два установа, перехват",
        "about": "Тело вращения → профиль r(z) → две программы (перехват) → "
                 "оба установа в NX ISV → итоговая деталь. Заготовка "
                 "подбирается сама: круглый прокат по ГОСТ 2590.",
        "spec": LATHE_SPEC, "run": LATHE_RUN,
        "stock": False,
        "stock_note": "",
    },
}
DEFAULT_KIND = "mill"


def _spec(kind):
    k = KINDS.get(kind)
    if not k:
        raise ValueError(f"неизвестный вид обработки {kind!r}; есть: "
                         f"{', '.join(KINDS)}")
    return k


def _by_name(kind):
    k = _spec(kind)
    return {f["name"]: f for _, fields in k["spec"] for f in fields}


def tool_pool():
    """Парк токарного инструмента для чекбоксов — то, что есть в цехе."""
    from lathe import lathe_tools
    extra = getattr(config, "LATHE_TOOLS", None)
    out = []
    for t in lathe_tools.catalog(extra).values():
        row = {"id": t["id"], "kind": t["type"], "desc": t["desc"]}
        if t["type"] == "turning":
            row.update(hand=t.get("hand"), nose_angle=t.get("nose_angle"),
                       phi1=round(lathe_tools.phi1(t), 1))
        if "width" in t:
            row["width"] = t["width"]
        out.append(row)
    return out


def _value(kind, f):
    """Текущее значение поля: из config.py, а для набора инструмента — весь
    парк, если ничего не задано (генератору по умолчанию доступно всё)."""
    if f["type"] == "tools":
        from lathe import lathe_tools
        extra = getattr(config, "LATHE_TOOLS", None)
        return (getattr(config, "LATHE_AVAILABLE_TOOLS", None)
                or lathe_tools.all_ids(extra))
    val = getattr(config, f["name"], None)
    if f["type"] == "floats":
        val = ", ".join(f"{v:g}" for v in (val or []))
    return val


def current(kind=DEFAULT_KIND):
    """Спецификация вида обработки с ПОДСТАВЛЕННЫМИ значениями из config.py."""
    k = _spec(kind)
    groups = []
    for group, fields in k["spec"]:
        items = [{**f, "value": _value(kind, f)} for f in fields]
        groups.append({"group": group, "fields": items})
    out = {"kind": kind, "title": k["title"], "sub": k["sub"],
           "about": k["about"], "stock": k["stock"],
           "stock_note": k["stock_note"], "groups": groups,
           "run": [{**f, "value": f["default"]} for f in k["run"]]}
    if any(f["type"] == "tools" for _, fs in k["spec"] for f in fs):
        out["tool_pool"] = tool_pool()
    return out


def all_kinds():
    """Все виды обработки разом — форма переключается без похода на сервер."""
    return {name: current(name) for name in KINDS}


def coerce(kind, name, raw):
    """Значение из формы → значение для конфига, с проверкой границ.

    Кидает ValueError с человеческим текстом: он уйдёт прямо в форму.
    """
    f = _by_name(kind).get(name)
    if not f:
        raise ValueError(f"неизвестный параметр {name!r}")
    t = f["type"]
    if t == "bool":
        return bool(raw)
    if t == "choice":
        if str(raw) not in f["choices"]:
            raise ValueError(f"{f['label']}: {raw!r} — допустимо "
                             f"{', '.join(f['choices'])}")
        return str(raw)
    if t == "tools":
        from lathe import lathe_tools
        extra = getattr(config, "LATHE_TOOLS", None)
        ids = [str(s).strip() for s in (raw or []) if str(s).strip()]
        if not ids:
            raise ValueError(f"{f['label']}: не отмечен ни один инструмент")
        pool = lathe_tools.catalog(extra)
        bad = [i for i in ids if i not in pool]
        if bad:
            raise ValueError(f"{f['label']}: нет в парке {', '.join(bad)}")
        # Набор должен быть работоспособен ДО запуска: без правого проходного
        # программы не будет вовсе, и узнать об этом через минуту работы
        # FreeCAD — худший из возможных способов.
        try:
            lathe_tools.plan(ids, extra=extra)
        except ValueError as e:
            raise ValueError(f"{f['label']}: {e}")
        return ids
    if t == "floats":
        try:
            vals = [float(s.strip().replace(",", "."))
                    for s in str(raw).replace(";", " ").replace(",", " ").split()
                    if s.strip()]
        except ValueError:
            raise ValueError(f"{f['label']}: не разобрать список чисел")
        if not vals:
            raise ValueError(f"{f['label']}: нужен хотя бы один диаметр")
        if any(v <= 0 for v in vals):
            raise ValueError(f"{f['label']}: диаметр должен быть больше нуля")
        return sorted(set(vals), reverse=True)
    try:
        v = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"{f['label']}: нужно число")
    lo, hi = f.get("min"), f.get("max")
    if lo is not None and v < lo or hi is not None and v > hi:
        raise ValueError(f"{f['label']}: {v:g} вне диапазона {lo:g}…{hi:g}")
    return int(v) if t == "int" else v


def build(kind, form):
    """Форма → (словарь для config.yaml, аргументы петли).

    Параметры, которых в форме нет, в YAML не попадают вовсе: тогда работают
    дефолты config.py, и мы не размножаем копии одних и тех же значений.
    Поля с `as_run` (набор инструмента) в конфиг тоже не идут — они уходят
    аргументом петле, чтобы у набора был ровно один источник истины.
    """
    k = _spec(kind)
    by_name = _by_name(kind)
    cfg, run = {}, {}
    for name, raw in (form or {}).items():
        f = by_name.get(name)
        if not f:
            continue
        val = coerce(kind, name, raw)
        (run if f.get("as_run") else cfg)[name] = val
    for f in k["run"]:
        raw = (form or {}).get(f["name"], f["default"])
        try:
            v = float(str(raw).replace(",", "."))
        except (TypeError, ValueError):
            raise ValueError(f"{f['label']}: нужно число")
        if not f["min"] <= v <= f["max"]:
            raise ValueError(f"{f['label']}: {v:g} вне диапазона "
                             f"{f['min']:g}…{f['max']:g}")
        run[f["name"]] = int(v) if f["type"] == "int" else v

    if kind == "mill":
        # Связность, которую конфиг сам не проверит: заглавная фреза обязана
        # быть в наборе, иначе воркер соберёт пул без неё и назначит операциям
        # чужой Ø.
        d, s = cfg.get("TOOL_DIAMETER"), cfg.get("TOOL_SET")
        if d is not None and s is not None and not any(abs(d - x) < 1e-6
                                                       for x in s):
            cfg["TOOL_SET"] = sorted(set(s + [d]), reverse=True)
    return cfg, run
