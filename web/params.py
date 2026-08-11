#!/usr/bin/env python3
"""
web/params.py — какие параметры пайплайна показывать в форме и как их проверять.

Дефолты НЕ дублируются: они читаются из `config.py` в момент запроса, поэтому
форма всегда открывается с теми же значениями, с которыми работает CLI. Здесь
описано только то, ЧТО показывать пользователю, КАК подписать и в каких
границах принимать.

Границы совпадают с белым списком `auto_fix.PARAM_WHITELIST` там, где параметр
есть и в нём: агент в петле не должен иметь права уйти дальше человека.

Чего тут НАМЕРЕННО НЕТ:
  * `DIFF_OK_*` и прочие пороги приёмки — их двигает петля, и давать их
    пользователю в одной форме с параметрами резания значит путать «как резать»
    с «что считать годным»;
  * `DEAD_ZONES` / `EXTRA_ZONES` / `SKIP_OPS` — это координаты и имена операций,
    их заполняет агент по ходу петли, вручную задавать нечего;
  * пути (`FREECAD_CMD`, `NX_BASE_DIR`) — машинные настройки, им место в
    config.yaml, а не в веб-форме.
"""

import config

# группа → список полей. Порядок групп = порядок на странице.
SPEC = [
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
RUN_SPEC = [
    {"name": "iters", "label": "Максимум итераций петли",
     "type": "int", "min": 1, "max": 10, "step": 1, "default": 3,
     "hint": "Каждая итерация — генерация + симуляция NX + сверка + запрос "
             "к агенту. Считайте по 3–5 минут на итерацию."},
]

BY_NAME = {f["name"]: f for _, fields in SPEC for f in fields}


def current():
    """Спецификация с ПОДСТАВЛЕННЫМИ текущими значениями из config.py."""
    out = []
    for group, fields in SPEC:
        items = []
        for f in fields:
            val = getattr(config, f["name"], None)
            if f["type"] == "floats":
                val = ", ".join(f"{v:g}" for v in (val or []))
            items.append({**f, "value": val})
        out.append({"group": group, "fields": items})
    return {"groups": out,
            "run": [{**f, "value": f["default"]} for f in RUN_SPEC]}


def coerce(name, raw):
    """Значение из формы → значение для конфига, с проверкой границ.

    Кидает ValueError с человеческим текстом: он уйдёт прямо в форму.
    """
    f = BY_NAME.get(name)
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


def build(form):
    """Форма → (словарь для config.yaml, аргументы петли).

    Параметры, которых в форме нет, в YAML не попадают вовсе: тогда работают
    дефолты config.py, и мы не размножаем копии одних и тех же значений.
    """
    cfg, run = {}, {}
    for name, raw in (form or {}).items():
        if name in BY_NAME:
            cfg[name] = coerce(name, raw)
    for f in RUN_SPEC:
        raw = (form or {}).get(f["name"], f["default"])
        try:
            v = int(float(str(raw)))
        except (TypeError, ValueError):
            raise ValueError(f"{f['label']}: нужно целое число")
        if not f["min"] <= v <= f["max"]:
            raise ValueError(f"{f['label']}: {v} вне диапазона "
                             f"{f['min']}…{f['max']}")
        run[f["name"]] = v

    # Связность, которую конфиг сам не проверит: заглавная фреза обязана быть
    # в наборе, иначе воркер соберёт пул без неё и назначит операциям чужой Ø.
    d, s = cfg.get("TOOL_DIAMETER"), cfg.get("TOOL_SET")
    if d is not None and s is not None and not any(abs(d - x) < 1e-6 for x in s):
        cfg["TOOL_SET"] = sorted(set(s + [d]), reverse=True)
    return cfg, run
