#!/usr/bin/env python3
"""
freecad_worker.py — выполняется ВНУТРИ Python FreeCAD (freecadcmd), не в обычном Python.

Параметры приходят JSON-файлом, путь к которому — в переменной окружения
FREECAD_WORKER_PARAMS (не аргументом: freecadcmd пытается выполнить каждый файловый
аргумент как скрипт).

Стратегия — 3D-обработка по поверхности (Path Surface): фреза следует за фактической
геометрией модели (наклоны, конусы, купола, рельеф).

Форматы модели:
  .step/.stp/.iges/.igs/.brep — точное тело (BREP), единицы из файла. Рекомендуемый вход.
  .stl/.obj                   — меш; масштабируется (scale_to_mm) и сшивается в тело.
  .prt (Siemens NX)           — не читается FreeCAD; нужен экспорт STEP из NX.

Запуск (обычно через freecad_cam.py):
  QT_QPA_PLATFORM=offscreen FREECAD_WORKER_PARAMS=params.json freecadcmd freecad_worker.py
"""

import json
import math
import os
import sys

import FreeCAD
import Part
import Mesh

# FreeCAD форсирует stdout в кодировку консоли (на Windows-RU это cp1251), игнорируя
# PYTHONUTF8. Символ Ø и прочие не-cp1251 знаки в log() иначе роняют worker с
# UnicodeEncodeError. Переключаем на UTF-8 ТОЛЬКО когда stdout не UTF-8; где он уже
# UTF-8 (Linux), ничего не трогаем — старое поведение сохраняется.
for _stream in (sys.stdout, sys.stderr):
    if (getattr(_stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

SOLID_EXTS = {".step", ".stp", ".iges", ".igs", ".brep", ".brp"}


def log(msg):
    # stdout worker'а парсится хостом; префикс отделяет наши строки от шума FreeCAD
    print(f"[worker] {msg}", flush=True)


def load_model(path, scale_to_mm):
    """Файл модели → твёрдое тело."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".prt":
        raise RuntimeError(
            ".prt (Siemens NX) — закрытый формат, FreeCAD его не читает. "
            "Экспортируйте деталь из NX в STEP (File → Export → STEP AP214/AP242) "
            "и подайте .step файл."
        )

    if ext in SOLID_EXTS:
        shape = Part.Shape()
        shape.read(path)
        if shape.Solids:
            solid = max(shape.Solids, key=lambda s: s.Volume)
            if len(shape.Solids) > 1:
                log(f"warn: в файле {len(shape.Solids)} тел, взято самое крупное")
        else:
            solid = Part.makeSolid(shape)  # поверхности без объёма — пробуем собрать
        log(f"model loaded as exact BREP ({ext})")
        return solid

    # меш (.stl/.obj/…)
    mesh = Mesh.Mesh(path)
    if scale_to_mm and scale_to_mm != 1.0:
        m = FreeCAD.Matrix()
        m.scale(scale_to_mm, scale_to_mm, scale_to_mm)
        mesh.transform(m)
    shape = Part.Shape()
    shape.makeShapeFromMesh(mesh.Topology, 0.05)  # tolerance сшивки, мм
    solid = Part.makeSolid(shape.removeSplitter())
    log(f"model loaded as faceted mesh ({ext}, scale={scale_to_mm})")
    return solid


def apply_transforms(solid, journal):
    """Повторяет на другом теле трансформации из журнала (повороты/сдвиги детали).
    Нужно для заготовки из файла: она задана в той же системе координат, что и
    деталь, и должна двигаться синхронно с ней."""
    solid = solid.copy()
    for kind, args in journal:
        if kind == "rotate":
            axis, angle = args
            solid.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(*axis), angle)
        else:
            solid.translate(FreeCAD.Vector(*args))
    return solid


def auto_orient(solid, journal=None):
    """Кладёт деталь самой большой плоской гранью «на стол» (нормаль грани → вниз).
    Детали из NX часто экспортированы в координатах сборки/станка НАКЛОНЁННЫМИ —
    3-осевая обработка при этом невозможна: ось фрезы должна совпадать с Z детали."""
    best = None
    for f in solid.Faces:
        if type(f.Surface).__name__ == "Plane" and (best is None or f.Area > best.Area):
            best = f
    if best is None:
        log("auto-orient: плоских граней нет — ориентация не менялась")
        return solid
    n = best.normalAt(0, 0)
    target = FreeCAD.Vector(0, 0, -1)  # большая грань станет дном
    angle = math.degrees(n.getAngle(target))
    if angle < 0.05:
        return solid
    axis = n.cross(target)
    if axis.Length < 1e-9:      # нормаль уже вдоль Z (вверх) — переворот вокруг X
        axis = FreeCAD.Vector(1, 0, 0)
    # rotate() — жёсткий поворот, сохраняет аналитические поверхности (плоскости,
    # цилиндры). transformGeometry здесь НЕЛЬЗЯ: он конвертирует их в BSpline,
    # после чего Adaptive строит пустые зоны, а Surface падает на «Null shape».
    solid = solid.copy()
    solid.rotate(FreeCAD.Vector(0, 0, 0), axis, angle)
    if journal is not None:
        journal.append(("rotate", ((axis.x, axis.y, axis.z), angle)))
    log(f"auto-orient: деталь повёрнута на {angle:.1f}° (большая плоская грань — вниз)")
    return solid


def orient_features_up(solid, journal=None):
    """После укладки на большую грань проверяет, куда ОТКРЫТЫ отверстия.
    Крупнейшие плоскости двух сторон детали часто почти равны, и укладка может
    положить деталь «лицом вниз» — тогда фактура (глухие отверстия, карманы)
    недоступна 3-осевой обработке. За переворот голосует только отверстие,
    закрытое сверху и открытое снизу (глухое, смотрит вниз): сквозной вырез
    доступен фрезе сверху в любом положении — судить по «цилиндр у дна детали»
    нельзя (сквозное окно в нижней полке перевернуло бы деталь зря)."""
    up = down = 0
    for f in solid.Faces:
        s = f.Surface
        if type(s).__name__ != "Cylinder" or abs(s.Axis.z) < 0.999:
            continue
        u0, u1, v0, v1 = f.ParameterRange
        pnt = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
        nrm = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        radial = FreeCAD.Vector(pnt.x - s.Center.x, pnt.y - s.Center.y, 0)
        if nrm.dot(radial) > 0:
            continue  # выпуклая стенка (бобышка), не отверстие
        fb = f.BoundBox
        probe_up = FreeCAD.Vector(s.Center.x, s.Center.y, fb.ZMax + 0.2)
        probe_dn = FreeCAD.Vector(s.Center.x, s.Center.y, fb.ZMin - 0.2)
        if not solid.isInside(probe_up, 1e-6, True):
            up += 1        # открыто сверху (сквозное или глухое вверх) — ок
        elif not solid.isInside(probe_dn, 1e-6, True):
            down += 1      # глухое, открыто только вниз
    if down > 0 and up == 0:
        solid = solid.copy()
        solid.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(1, 0, 0), 180)
        if journal is not None:
            journal.append(("rotate", ((1.0, 0.0, 0.0), 180.0)))
        log(f"orient: отверстия ({down} шт.) открыты только вниз — деталь перевёрнута на 180°")
    return solid


def normalize_origin(solid, mode, journal=None):
    """Сдвигает модель к нулю программы. Детали из NX часто экспортированы в координатах
    сборки/станка (геометрия за метры от нуля) — без сдвига первый же G0 уводит фрезу туда.
      corner-top — X0 Y0 = мин. угол габарита, Z0 = верхняя плоскость (стандарт ЧПУ);
      center-top — X0 Y0 = центр детали, Z0 = верх;
      model      — не сдвигать (ноль = ноль CAD-файла)."""
    if mode == "model":
        return solid
    bb = solid.BoundBox
    if mode == "center-top":
        dx, dy = -(bb.XMin + bb.XMax) / 2.0, -(bb.YMin + bb.YMax) / 2.0
    else:  # corner-top
        dx, dy = -bb.XMin, -bb.YMin
    dz = -bb.ZMax
    if max(abs(dx), abs(dy), abs(dz)) > 1e-9:
        solid.translate(FreeCAD.Vector(dx, dy, dz))
        if journal is not None:
            journal.append(("translate", (dx, dy, dz)))
        log(f"origin {mode}: модель сдвинута на ({dx:.2f}, {dy:.2f}, {dz:.2f})")
    return solid


def set_prop(obj, prop, value):
    """Ставит свойство операции, логируя (а не роняя) несовместимость версии API."""
    try:
        setattr(obj, prop, value)
    except Exception as e:
        log(f"warn: {prop}={value!r} не применилось: {e}")


def export_gcode(job, ops, postname):
    """Постпроцессирует текущий набор операций в текст G-Code."""
    job.Operations.Group = ops
    job.Document.recompute()
    from Path.Post.Processor import PostProcessorFactory
    post = PostProcessorFactory.get_post_processor(job, postname)
    sections = post.export()
    return "\n".join(sec[1] for sec in sections if sec and sec[1])


def write_partial(job, ops, p, note):
    """Пишет промежуточный G-Code уже посчитанных операций — файл можно смотреть,
    не дожидаясь конца расчёта. Финальная запись в main() перезаписывает его."""
    try:
        gcode = export_gcode(job, ops, p["postprocessor"])
        with open(p["gcode_path"], "w", encoding="utf-8") as f:
            f.write(f"(ПРОМЕЖУТОЧНЫЙ ФАЙЛ: {note} — расчёт продолжается)\n"
                    f"{p.get('_gcode_header', '')}{gcode}")
        log(f"промежуточный G-Code записан: {note}")
    except Exception as e:
        log(f"warn: промежуточная запись не удалась: {e}")


# ── Мёртвые зоны (keep-out): объёмы, куда инструменту нельзя ─────────────────────
# Примитивы (в СК ПРОГРАММЫ — как у G-кода): запретные боксы (не входить),
# рабочие боксы (не выходить; работаем в их пересечении), полупространства по
# осям. Модель инструмента — вертикальный цилиндр Ø фрезы от кончика ВВЕРХ
# (хвостовик/патрон не тоньше и не короче), поэтому:
#   - запретный бокс закрывает и всё ПОД собой (сверху туда не подъехать);
#     проезд НАД боксом (кончик выше его верха + зазор) разрешён;
#   - потолки (Z gt, верх рабочего бокса) проверяются по КОНЧИКУ инструмента;
#   - в XY зоны раздуваются на радиус фрезы + зазор m (KEEPOUT_MARGIN).
# Ограничение АПРИОРНОЕ: зоны операций режутся ДО расчёта траекторий; гейт по
# готовому G-коду (check_gcode_zones) — только страховка от дыр в этой логике.

_ZONE_EPS = 1e-9


def parse_zones(p):
    """params → структура зон или None (зон нет). Рабочие боксы сразу
    пересекаются в один; пустое пересечение — ошибка."""
    boxes = [[float(v) for v in b] for b in (p.get("keepout_boxes") or [])]
    works = [[float(v) for v in b] for b in (p.get("work_boxes") or [])]
    half = [[str(a).upper(), str(c).lower(), float(v)]
            for a, c, v in (p.get("keepout_halfspaces") or [])]
    if not (boxes or works or half):
        return None
    work = None
    for w in works:
        if work is None:
            work = list(w)
        else:
            work = [max(work[0], w[0]), max(work[1], w[1]), max(work[2], w[2]),
                    min(work[3], w[3]), min(work[4], w[4]), min(work[5], w[5])]
    if work is not None and (work[0] >= work[3] or work[1] >= work[4]
                             or work[2] >= work[5]):
        raise RuntimeError("пересечение рабочих боксов пусто — работать негде")
    m = float(p.get("keepout_margin", 0.5))
    return {"boxes": boxes, "work": work, "half": half, "m": m,
            "r": float(p["tool_diameter"]) / 2.0,
            # раздув для ПОСТРОЕНИЯ зон операций: сверх зазора m закладываем
            # допуск расчёта траектории (Adaptive кладёт путь с точностью
            # rough_tolerance) и округление G-кода — иначе слоп траектории
            # съедает зазор и строгий гейт ловит касания на сотки
            "grow": m + float(p.get("rough_tolerance", 0.1)) + 0.01}


def zone_z_floor(z):
    """Пол для кончика инструмента (ниже нельзя): Z-lt полупространства и низ
    рабочего бокса. -inf, если пола нет."""
    floor = float("-inf")
    if z["work"] is not None:
        floor = max(floor, z["work"][2] + z["m"])
    for a, c, v in z["half"]:
        if a == "Z" and c == "lt":
            floor = max(floor, v + z["m"])
    return floor


def zone_z_ceiling(z):
    """Потолок для кончика (Z-gt полупространства, верх рабочего бокса).
    None, если потолка нет. Хвостовик НЕ моделируется: потолок — по кончику."""
    ceil_ = None
    if z["work"] is not None:
        ceil_ = z["work"][5] - z["m"]
    for a, c, v in z["half"]:
        if a == "Z" and c == "gt":
            hv = v - z["m"]
            ceil_ = hv if ceil_ is None else min(ceil_, hv)
    return ceil_


def zone_required_clearance(z):
    """Минимальная высота холостых перемещений: выше верха всех запретных
    боксов (проезд над боксом разрешён). None, если боксов нет."""
    if not z["boxes"]:
        return None
    return max(b[5] for b in z["boxes"]) + z["m"]


def op_clearance(p, start_z):
    """ClearanceHeight операции: обычная формула, но не ниже проезда над
    запретными боксами и не выше потолка зон (совместимость высот заранее
    проверена check_zone_heights)."""
    clr = start_z + p["safe_height"]
    z = p.get("_zones")
    if z:
        rq = zone_required_clearance(z)
        if rq is not None:
            clr = max(clr, rq)
        ceil_ = zone_z_ceiling(z)
        if ceil_ is not None:
            clr = min(clr, ceil_)
    return clr


def check_zone_heights(z, sb):
    """Совместимость высот: потолок зон обязан оставлять место подходам
    (верх заготовки + 3 мм на SafeHeight) и проезду над запретными боксами;
    пол — быть ниже верха заготовки. Иначе — понятная ошибка сразу."""
    ceil_ = zone_z_ceiling(z)
    if ceil_ is not None:
        need = sb.ZMax + 3.0
        rq = zone_required_clearance(z)
        if rq is not None and rq > need:
            need = rq
        if ceil_ < need - _ZONE_EPS:
            raise RuntimeError(
                f"потолок зон Z={ceil_:.2f} ниже необходимых {need:.2f} мм "
                f"(подходы над заготовкой / проезд над запретными боксами) — "
                f"обработка сверху невозможна, поправьте зоны")
    floor = zone_z_floor(z)
    if floor > sb.ZMax - _ZONE_EPS:
        raise RuntimeError(f"пол зон Z={floor:.2f} не ниже верха заготовки "
                           f"Z={sb.ZMax:.2f} — резать нечего")


def warn_zone_sanity(z, sb):
    """Ранняя ловля типовой ошибки «зоны заданы не в той СК»: зона, не
    задевающая заготовку, скорее всего промахнулась. Зона, съевшая всю
    заготовку, — ошибка сразу."""
    g = z["r"] + z["m"]
    for b in z["boxes"]:
        if (b[0] - g > sb.XMax or b[3] + g < sb.XMin or
                b[1] - g > sb.YMax or b[4] + g < sb.YMin or
                b[5] + z["m"] < sb.ZMin):
            log(f"warn: запретный бокс X {b[0]:g}..{b[3]:g} Y {b[1]:g}..{b[4]:g} "
                f"Z {b[2]:g}..{b[5]:g} не задевает заготовку — проверьте, что "
                f"его координаты в СК ПРОГРАММЫ (см. шапку G-кода)")
    if z["work"] is not None:
        w = z["work"]
        if (w[0] + g > sb.XMax or w[3] - g < sb.XMin or
                w[1] + g > sb.YMax or w[4] - g < sb.YMin):
            raise RuntimeError("рабочий бокс не пересекает заготовку — работать "
                               "негде (проверьте СК зон)")
        if (w[0] + g > sb.XMin + 1e-6 or w[3] - g < sb.XMax - 1e-6 or
                w[1] + g > sb.YMin + 1e-6 or w[4] - g < sb.YMax - 1e-6):
            log("warn: рабочий бокс тесней заготовки — материал за его "
                "пределами останется несрезанным")
    for a, c, v in z["half"]:
        if a == "Z":
            continue
        lo, hi = (sb.XMin, sb.XMax) if a == "X" else (sb.YMin, sb.YMax)
        if (c == "lt" and v + g > hi) or (c == "gt" and v - g < lo):
            raise RuntimeError(f"запрет {a} {'<' if c == 'lt' else '>'} {v:g} "
                               f"накрывает всю заготовку — работать негде")


def plug_stock_for_zones(doc, job, sb, z, p):
    """Запретные КОЛОННЫ в заготовке — для планировщика Adaptive. Он сводит
    заготовку к внешнему 2D-контуру (TechDraw.findShapeOutline) и считает всё
    вне контура расчищенным воздухом: там он свободно ставит винтовой вход и
    тянет линки НА ГЛУБИНЕ РЕЗА, игнорируя вырез из региона операции. Колонна
    (бокс ⊕ m, на высоту заготовки — габарит не меняется) расширяет контур,
    воздух в футпринте бокса становится «материалом», и Adaptive его объезжает.
    Нужна только боксам, задевающим кромку/окрестность заготовки: внутри
    силуэта и так материал. Заготовка при этом материализуется в статичную
    Part::Feature (у параметрической присвоение Shape откатилось бы)."""
    reach = float(p["tool_diameter"]) + float(p["rough_allowance"]) + z["m"] + 1.0
    prisms = []
    for b in z["boxes"]:
        if (b[0] > sb.XMax + reach or b[3] < sb.XMin - reach or
                b[1] > sb.YMax + reach or b[4] < sb.YMin - reach):
            continue    # бокс далеко от заготовки — планировщику не мешает
        g = z["grow"]
        prisms.append(Part.makeBox(b[3] - b[0] + 2 * g, b[4] - b[1] + 2 * g,
                                   sb.ZLength,
                                   FreeCAD.Vector(b[0] - g, b[1] - g, sb.ZMin)))
    if not prisms:
        return
    try:
        fused = job.Stock.Shape.fuse(prisms)
    except Exception as e:
        log(f"warn: запретные колонны не вплавились в заготовку ({e}) — "
            f"вход/линки Adaptive у края прикроет только гейт")
        return
    stock_feat = doc.addObject("Part::Feature", "StockZoned")
    stock_feat.Shape = fused
    old = job.Stock
    job.Stock = stock_feat
    try:
        doc.removeObject(old.Name)
    except Exception:
        pass
    doc.recompute()
    log(f"зоны: в заготовку вплавлено запретных колонн: {len(prisms)} — "
        f"планировщик Adaptive не поведёт туда вход и линки (габарит прежний)")


def _rect_face(x0, y0, x1, y1):
    """Прямоугольная грань на плоскости Z=0 (там живут все 2D-зоны операций)."""
    return Part.Face(Part.makePolygon([
        FreeCAD.Vector(x0, y0, 0), FreeCAD.Vector(x1, y0, 0),
        FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x0, y1, 0),
        FreeCAD.Vector(x0, y0, 0)]))


def restrict_region(z, region, grow, final_z, name):
    """АПРИОРНОЕ ограничение 2D-зоны операции (грани на Z=0): вычесть запретные
    футпринты, раздутые на grow, и пересечь с рабочим боксом, сжатым на grow.
    grow зависит от типа операции: Adaptive держит ДИСК фрезы внутри зоны —
    достаточно зазора m; Profile ведёт ЦЕНТР снаружи контура на R+припуск —
    нужно 2R+припуск+m. Запретный бокс участвует, только если операция
    опускается ниже его верха (final_z < z1+m): выше бокса резать можно.
    Возвращает (регион|None, менялся_ли: bool); None — зона съедена целиком."""
    if z is None or region is None:
        return region, False
    changed = False
    shape = region
    if z["work"] is not None:
        w = z["work"]
        clipped = shape.common(_rect_face(w[0] + grow, w[1] + grow,
                                          w[3] - grow, w[4] - grow))
        if abs(clipped.Area - shape.Area) > 1e-6:
            changed = True
        shape = clipped
    if shape.Area > 1e-9:
        bb = shape.BoundBox
        ex0, ey0, ex1, ey1 = bb.XMin - 1.0, bb.YMin - 1.0, bb.XMax + 1.0, bb.YMax + 1.0
        rects = []
        for b in z["boxes"]:
            if final_z < b[5] + z["m"] - _ZONE_EPS:
                rects.append((b[0] - grow, b[1] - grow, b[3] + grow, b[4] + grow))
        for a, c, v in z["half"]:
            if a == "Z":
                continue
            if a == "X":
                rects.append((ex0, ey0, v + grow, ey1) if c == "lt"
                             else (v - grow, ey0, ex1, ey1))
            else:
                rects.append((ex0, ey0, ex1, v + grow) if c == "lt"
                             else (ex0, v - grow, ex1, ey1))
        for r in rects:
            if r[2] - r[0] < 1e-6 or r[3] - r[1] < 1e-6:
                continue
            cut = shape.cut(_rect_face(*r))
            if abs(cut.Area - shape.Area) > 1e-6:
                changed = True
            shape = cut
            if shape.Area < 1e-9:
                break
    faces = [f for f in shape.Faces if f.Area > 0.5]
    if not faces:
        log(f"{name}: зона операции целиком в мёртвой зоне — пропущено")
        return None, True
    if changed:
        log(f"{name}: зона операции урезана мёртвыми зонами")
        shape = faces[0] if len(faces) == 1 else Part.makeCompound(faces)
    return shape, changed


def surface_zone_block(z, rbb, final_z):
    """Пускать ли Surface-черновую (террасы по грани: RoughSlope / узкий
    RoughFace)? Операция расширяет границу зоны на BoundaryAdjustment=R
    (это путь ЦЕНТРА, до 2R за контур грани при BoundaryEnforcement=False),
    диск добавляет ещё R — рабочее пятно = bbox грани + ~3R. Внутри пятна
    Surface объезжать зоны не умеет, поэтому при любом конфликте операция
    пропускается ЦЕЛИКОМ (v1, консервативно — материал остаётся).
    Возвращает строку-причину или None (можно работать)."""
    if z is None:
        return None
    e = 3.0 * z["r"] + z["m"] + 0.2
    x0, y0, x1, y1 = rbb.XMin - e, rbb.YMin - e, rbb.XMax + e, rbb.YMax + e
    for b in z["boxes"]:
        if final_z < b[5] + z["m"] - _ZONE_EPS and \
                x1 > b[0] and b[3] > x0 and y1 > b[1] and b[4] > y0:
            return (f"рабочее пятно задевает запретный бокс "
                    f"X {b[0]:g}..{b[3]:g} Y {b[1]:g}..{b[4]:g}")
    for a, c, v in z["half"]:
        if a == "Z":
            continue
        lo, hi = (x0, x1) if a == "X" else (y0, y1)
        if (c == "lt" and lo < v) or (c == "gt" and hi > v):
            return (f"рабочее пятно пересекает запрет "
                    f"{a} {'<' if c == 'lt' else '>'} {v:g}")
    if z["work"] is not None:
        w = z["work"]
        if x0 < w[0] or y0 < w[1] or x1 > w[3] or y1 > w[4]:
            return "рабочее пятно выходит за рабочий бокс"
    return None


def zone_header_lines(z):
    """Строки шапки G-кода про зоны (латиницей — кириллицу в комментариях
    понимает не каждая стойка)."""
    if z is None:
        return ""
    out = [f"(Keepout: tool R{z['r']:g} mm + margin {z['m']:g} mm applied)"]
    for b in z["boxes"]:
        out.append(f"(Keepout box: X {b[0]:g}..{b[3]:g}  Y {b[1]:g}..{b[4]:g}  "
                   f"Z {b[2]:g}..{b[5]:g}, no entry below Z {b[5]:g})")
    if z["work"] is not None:
        w = z["work"]
        out.append(f"(Work box: X {w[0]:g}..{w[3]:g}  Y {w[1]:g}..{w[4]:g}  "
                   f"Z {w[2]:g}..{w[5]:g}, stay inside)")
    for a, c, v in z["half"]:
        out.append(f"(Keepout: {a} {'<' if c == 'lt' else '>'} {v:g})")
    return "\n".join(out) + "\n"


def _arc_segments(p0, p1, arc, cw):
    """Дуга G2/G3 (плоскость G17; I/J — инкрементальные от старта, как в
    grbl-посте; поддержан и R-формат) → цепочка отрезков с хордой ≤ 0.2 мм.
    Z интерполируется линейно (винтовые заходы Adaptive)."""
    x0, y0, z0 = p0
    x1, y1, z1 = p1
    if "I" in arc or "J" in arc:
        cx, cy = x0 + arc.get("I", 0.0), y0 + arc.get("J", 0.0)
    else:
        r = arc["R"]
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy)
        if d < 1e-9:
            return []
        h = math.sqrt(max(r * r - (d / 2.0) ** 2, 0.0))
        px, py = -dy / d, dx / d
        mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0

        def sweep(c):
            s = (math.atan2(y1 - c[1], x1 - c[0])
                 - math.atan2(y0 - c[1], x0 - c[0]))
            if cw:
                while s > 1e-12:
                    s -= 2 * math.pi
            else:
                while s < -1e-12:
                    s += 2 * math.pi
            return s
        cands = sorted([(mx + px * h, my + py * h), (mx - px * h, my - py * h)],
                       key=lambda c: abs(sweep(c)))
        cx, cy = cands[0] if r >= 0 else cands[-1]  # R>0 — малая дуга, R<0 — большая
    rad = math.hypot(x0 - cx, y0 - cy)
    a0 = math.atan2(y0 - cy, x0 - cx)
    s = math.atan2(y1 - cy, x1 - cx) - a0
    if cw:
        while s > -1e-9:     # нулевой свип по часовой = полный круг
            s -= 2 * math.pi
    else:
        while s < 1e-9:
            s += 2 * math.pi
    n = max(2, int(math.ceil(abs(s) * max(rad, 1e-6) / 0.2)))
    pts = [(cx + rad * math.cos(a0 + s * i / n),
            cy + rad * math.sin(a0 + s * i / n),
            z0 + (z1 - z0) * i / n) for i in range(n + 1)]
    return list(zip(pts[:-1], pts[1:]))


def _pt_seg_dist2d(p, a, b):
    """Расстояние точка-отрезок в XY."""
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    if ll < 1e-18:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / ll))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _seg_seg_dist2d(a0, a1, b0, b1):
    """Расстояние отрезок-отрезок в XY (0 — пересекаются)."""
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    d1, d2 = orient(b0, b1, a0), orient(b0, b1, a1)
    d3, d4 = orient(a0, a1, b0), orient(a0, a1, b1)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(_pt_seg_dist2d(a0, b0, b1), _pt_seg_dist2d(a1, b0, b1),
               _pt_seg_dist2d(b0, a0, a1), _pt_seg_dist2d(b1, a0, a1))


def _seg_rect_dist2d(p0, p1, x0, y0, x1, y1):
    """Расстояние в XY от отрезка до прямоугольника (0 — задевает)."""
    for px, py in (p0, p1):
        if x0 <= px <= x1 and y0 <= py <= y1:
            return 0.0
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    return min(_seg_seg_dist2d(p0, p1, corners[i], corners[(i + 1) % 4])
               for i in range(4))


def check_gcode_zones(z, gcode):
    """ГЕЙТ-страховка: проход по ГОТОВОМУ G-коду против зон в пространстве
    кончика инструмента. Запретный бокс = прямоугольник, раздутый ДИСКОМ
    R+m (углы скруглены — раздув квадратом ложно срабатывал бы на честном
    обходе угла): точный тест «расстояние XY-отрезка до прямоугольника» на
    части отрезка ниже потолка бокса. Дуги — сэмплинг; выпуклые ограничения
    (рабочий бокс — эрозия диском даёт точный прямоугольник, полупространства)
    — по концам отрезков (прямая между точками выпуклой области не выходит из
    неё). Возвращает [(строка, (x,y,z), глубина_захода, описание)].
    Априорное ограничение обязано давать пустой список — непустой означает
    дыру в нём, а не «нормальную» ситуацию."""
    import re
    word_re = re.compile(r"([A-Za-z])\s*([+-]?(?:\d+\.?\d*|\.\d+))")
    tol = 1e-3
    g = z["r"] + z["m"]

    # запретные боксы: (описание, x0, y0, x1, y1, потолок для кончика)
    boxes = [(f"запретный бокс X {b[0]:g}..{b[3]:g} Y {b[1]:g}..{b[4]:g} "
              f"(кончику нельзя ниже Z {b[5] + z['m']:g})",
              b[0], b[1], b[3], b[4], b[5] + z["m"])
             for b in z["boxes"]]
    half = []
    for a, c, v in z["half"]:
        shift = g if a != "Z" else z["m"]
        half.append((a, c, v, v + shift if c == "lt" else v - shift))
    work = None
    if z["work"] is not None:
        w = z["work"]
        work = (w[0] + g - tol, w[1] + g - tol, w[2] + z["m"] - tol,
                w[3] - g + tol, w[4] - g + tol, w[5] - z["m"] + tol)

    viol = []

    def check_point(pt, ln):
        if work is not None:
            d = min(pt[0] - work[0], work[3] - pt[0], pt[1] - work[1],
                    work[4] - pt[1], pt[2] - work[2], work[5] - pt[2])
            if d < 0:
                viol.append((ln, pt, -d, "выход за рабочий бокс"))
        for a, c, v, lim in half:
            i = {"X": 0, "Y": 1, "Z": 2}[a]
            if c == "lt" and pt[i] < lim - tol:
                viol.append((ln, pt, lim - pt[i], f"запрет {a} < {v:g}"))
            elif c == "gt" and pt[i] > lim + tol:
                viol.append((ln, pt, pt[i] - lim, f"запрет {a} > {v:g}"))

    def check_seg(p0, p1, ln):
        check_point(p1, ln)
        for desc, bx0, by0, bx1, by1, zc in boxes:
            # часть отрезка ниже потолка бокса (снизу бокс открыт)
            zc_eff = zc - tol
            z0, z1v = p0[2], p1[2]
            if z0 >= zc_eff and z1v >= zc_eff:
                continue
            t0, t1 = 0.0, 1.0
            dzt = z1v - z0
            if abs(dzt) > 1e-12:
                tc = (zc_eff - z0) / dzt
                if z0 >= zc_eff:      # входит под потолок в точке tc
                    t0 = max(t0, tc)
                elif z1v >= zc_eff:   # выходит из-под потолка в точке tc
                    t1 = min(t1, tc)
            q0 = (p0[0] + (p1[0] - p0[0]) * t0, p0[1] + (p1[1] - p0[1]) * t0)
            q1 = (p0[0] + (p1[0] - p0[0]) * t1, p0[1] + (p1[1] - p0[1]) * t1)
            d = _seg_rect_dist2d(q0, q1, bx0, by0, bx1, by1)
            if d < g - tol:
                tm = (t0 + t1) / 2.0
                pt = tuple(p0[i] + (p1[i] - p0[i]) * tm for i in range(3))
                viol.append((ln, pt, g - d, desc))

    pos = {"X": None, "Y": None, "Z": None}
    mode = None
    for ln, raw in enumerate(gcode.splitlines(), 1):
        line = re.sub(r"\([^)]*\)", "", raw).split(";", 1)[0].strip()
        if not line:
            continue
        target = dict(pos)
        arc = {}
        motion = None
        moved = False
        for letter, val in word_re.findall(line):
            letter = letter.upper()
            num = float(val)
            if letter == "G":
                gi = int(round(num))
                if gi in (0, 1, 2, 3):
                    mode = motion = gi
                elif gi == 91:
                    raise RuntimeError("гейт зон: G91 (инкрементальные "
                                       "координаты) не поддерживается")
                elif gi == 20:
                    raise RuntimeError("гейт зон: G20 (дюймы) не поддерживается")
            elif letter in ("X", "Y", "Z"):
                target[letter] = num
                moved = True
            elif letter in ("I", "J", "K", "R"):
                arc[letter] = num
        if not moved:
            continue
        if motion is None:
            motion = mode      # координаты без G-слова — модальный режим
        p1 = (target["X"], target["Y"], target["Z"])
        if motion is None or any(c is None for c in p1):
            pos = target       # координаты ещё не определились — судить нечего
            continue
        if any(pos[k] is None for k in "XYZ"):
            check_point(p1, ln)   # старт станка неизвестен — судим только точку
        elif motion in (2, 3) and ("I" in arc or "J" in arc or "R" in arc):
            p0 = (pos["X"], pos["Y"], pos["Z"])
            for q0, q1 in _arc_segments(p0, p1, arc, motion == 2):
                check_seg(q0, q1, ln)
        else:
            check_seg((pos["X"], pos["Y"], pos["Z"]), p1, ln)
        pos = target
    return viol


def enforce_zone_gate(z, gcode, gcode_path):
    """Прогоняет гейт; при нарушениях НЕ даёт программе выйти: целевой файл
    удаляется (там мог остаться промежуточный G-код), забракованный код
    сохраняется рядом как *.REJECTED.gcode, наверх уходит RuntimeError."""
    viol = check_gcode_zones(z, gcode)
    if not viol:
        log("гейт мёртвых зон: нарушений нет")
        return
    viol.sort(key=lambda v: -v[2])
    for ln, pt, depth, desc in viol[:5]:
        log(f"ГЕЙТ: строка {ln}: X{pt[0]:.2f} Y{pt[1]:.2f} Z{pt[2]:.2f} — "
            f"заход {depth:.2f} мм, {desc}")
    rej = os.path.splitext(gcode_path)[0] + ".REJECTED.gcode"
    with open(rej, "w", encoding="utf-8") as f:
        f.write(gcode)
    try:
        os.unlink(gcode_path)
    except OSError:
        pass
    raise RuntimeError(
        f"гейт мёртвых зон: {len(viol)} наруш. — программа НЕ записана, "
        f"забракованный код: {os.path.basename(rej)}. Это дыра в априорном "
        f"ограничении, сообщите о ней")


def _slice_faces(solid, z):
    """Сечение тела на высоте z как грани НА ПЛОСКОСТИ Z=0, с сохранением отверстий.
    Контуры собираются в грань все вместе (FaceMakerBullseye понимает вложенность):
    делать Face из каждого контура по отдельности нельзя — контур отверстия
    превращается в залитую фигуру, и union «замуровывает» вырезы."""
    try:
        wires = [w for w in solid.slice(FreeCAD.Vector(0, 0, 1), z) if w.isClosed()]
    except Exception:
        return []
    if not wires:
        return []
    moved = []
    for w in wires:
        w = w.copy()
        w.translate(FreeCAD.Vector(0, 0, -z))
        moved.append(w)
    try:
        made = Part.makeFace(moved, "Part::FaceMakerBullseye")
        return list(made.Faces)
    except Exception:
        # fallback: хотя бы без отверстий
        out = []
        for w in moved:
            try:
                out.append(Part.Face(w))
            except Exception:
                pass
        return out


def build_silhouette(solid, bb, step):
    """2D-силуэт детали: объединение горизонтальных сечений по слоям.
    Сквозные отверстия остаются отверстиями силуэта; глухие закрываются
    сечениями ниже дна отверстия."""
    eps = min(0.05, step / 10.0)
    zs = [bb.ZMax - eps]
    z = bb.ZMax - step
    while z > bb.ZMin + eps:
        zs.append(z)
        z -= step
    zs.append(bb.ZMin + eps)
    faces = []
    for z in zs:
        faces += _slice_faces(solid, z)
    if not faces:
        return None
    sil = faces[0] if len(faces) == 1 else faces[0].fuse(faces[1:])
    try:
        sil = sil.removeSplitter()
    except Exception:
        pass
    return sil


def _nearest_order(items, xy):
    """Порядок «как человек»: начиная от нуля детали, дальше ближайший следующий."""
    rest = list(items)
    ordered, cur = [], (0.0, 0.0)
    while rest:
        nxt = min(rest, key=lambda it: (xy(it)[0] - cur[0]) ** 2 + (xy(it)[1] - cur[1]) ** 2)
        rest.remove(nxt)
        ordered.append(nxt)
        cur = xy(nxt)
    return ordered


def find_through_cuts(sil):
    """Сквозные вырезы ПРОИЗВОЛЬНОЙ формы: внутренние замкнутые контуры силуэта.
    Вырез = «фигура, выдавленная вертикально насквозь» — круглый, овальный,
    из пересекающихся окружностей — не важно: каждый внутренний контур силуэта
    становится одной зоной. Острова внутри выреза вычитаются из зоны."""
    cuts = []
    for f in sil.Faces:
        outer_area = Part.Face(f.OuterWire).Area
        for w in f.Wires:
            try:
                region = Part.Face(w)
            except Exception:
                continue
            if abs(region.Area - outer_area) < 1e-6:
                continue  # это внешний контур, не вырез
            try:
                region = region.cut(sil)  # вычесть острова внутри выреза
            except Exception:
                pass
            if region.Area > 0.5:
                cuts.append(region)
    return _nearest_order(cuts, lambda r: (r.CenterOfMass.x, r.CenterOfMass.y))


def find_up_faces(shape, bb, include_top=False):
    """Плоские горизонтальные грани, смотрящие вверх, — «контуры» в терминах NX:
    полки, донья карманов, уступы. Каждая станет отдельной операцией: выбрать
    материал над гранью до её высоты + припуск. Верхние грани детали включаются
    только при include_top (заготовка выше детали — например, уголок или бокс
    с верхним полем: над верхом детали есть материал). Порядок: сверху вниз,
    на одном уровне — от ближней к дальней."""
    faces = []
    for idx, f in enumerate(shape.Faces, 1):
        if type(f.Surface).__name__ != "Plane" or f.normalAt(0, 0).z < 0.999:
            continue
        zf = f.BoundBox.ZMax  # грань горизонтальна: ZMin == ZMax
        if zf > bb.ZMax - 0.01 and not include_top:
            continue  # верх детали заподлицо с заготовкой — материала над ним нет
        region = f.copy()
        region.translate(FreeCAD.Vector(0, 0, -zf))
        c = f.CenterOfMass
        faces.append({"region": region, "z": zf, "area": f.Area,
                      "cx": c.x, "cy": c.y, "idx": idx})
    levels = sorted({round(fc["z"], 3) for fc in faces}, reverse=True)
    ordered = []
    for lv in levels:
        ordered += _nearest_order([fc for fc in faces if round(fc["z"], 3) == lv],
                                  lambda fc: (fc["cx"], fc["cy"]))
    return ordered


def make_adaptive(doc, job, tc, name, region_shape, p, start_z, final_z, allowance):
    """Одна черновая операция Adaptive по явной 2D-зоне (Side=Inside).
    Возвращает операцию или None, если траектория пуста.
    Зона задаётся явной плоской областью: проекции граней модели на сложной
    детали дают незамкнутый контур (Path.Area: «ccurve not closed»), и операция
    молча выдаёт пустую траекторию."""
    region = doc.addObject("Part::Feature", f"Region{name}")
    region.Shape = region_shape
    doc.recompute()

    import Path.Op.Adaptive as Adaptive
    op = Adaptive.Create(name, parentJob=job)
    op.ToolController = tc
    op.Base = [(region, f"Face{i + 1}") for i in range(len(region.Shape.Faces))]
    set_prop(op, "OperationType", "Clearing")
    set_prop(op, "Side", "Inside")
    set_prop(op, "StockToLeave", FreeCAD.Units.Quantity(f"{allowance} mm"))
    set_prop(op, "StepOver", int(p["rough_stepover"]))
    set_prop(op, "Tolerance", float(p["rough_tolerance"]))
    # setExpression(None) снимает привязку к SetupSheet — иначе recompute вернёт дефолт
    op.setExpression("StepDown", None)
    set_prop(op, "StepDown", p["rough_stepdown"])
    op.setExpression("StartDepth", None)
    op.StartDepth = start_z
    op.setExpression("FinalDepth", None)
    op.FinalDepth = final_z
    # ClearanceHeight/SafeHeight тоже привязаны экспрешеном к SetupSheet
    # (сток-топ + 5/3): без setExpression(None) присвоение .Value молча
    # откатывается на recompute — и подъём клиренса над боксами не доедет
    op.setExpression("ClearanceHeight", None)
    op.ClearanceHeight.Value = op_clearance(p, start_z)  # не ниже верха запретных боксов
    op.setExpression("SafeHeight", None)
    op.SafeHeight.Value = start_z + 3.0
    doc.recompute()  # здесь Adaptive считает траекторию — самый долгий шаг

    n = len(op.Path.Commands) if op.Path else 0
    log(f"{name}: {n} команд")
    if n > 2:
        return op
    try:  # пустую операцию убираем из документа — иначе имя останется занятым
        doc.removeObject(op.Name)
        doc.recompute()
    except Exception:
        pass
    return None  # 2 команды = пустой путь (один подъём Z)


def make_profile(doc, job, tc, name, region_shape, p, start_z, final_z, allowance):
    """Контурный проход (Path Profile): фреза обходит внешний контур зоны
    с отступом радиус + припуск, слоями StepDown до дна. Применяется для
    периметра при УЗКИХ полях (меньше ~2 диаметров): адаптивной выборке там
    негде сделать винтовой заход, а контурному проходу заход не нужен —
    он идёт по воздуху вокруг заготовки и срезает выступающий материал."""
    region = doc.addObject("Part::Feature", f"Region{name}")
    region.Shape = region_shape
    doc.recompute()

    import Path.Op.Profile as Profile
    op = Profile.Create(name, parentJob=job)
    op.ToolController = tc
    op.Base = [(region, f"Face{i + 1}") for i in range(len(region.Shape.Faces))]
    set_prop(op, "Side", "Outside")
    set_prop(op, "UseComp", True)  # смещение на радиус фрезы считается в софте
    set_prop(op, "OffsetExtra", FreeCAD.Units.Quantity(f"{allowance} mm"))
    op.setExpression("StepDown", None)
    set_prop(op, "StepDown", p["rough_stepdown"])
    op.setExpression("StartDepth", None)
    op.StartDepth = start_z
    op.setExpression("FinalDepth", None)
    op.FinalDepth = final_z
    # ClearanceHeight/SafeHeight тоже привязаны экспрешеном к SetupSheet
    # (сток-топ + 5/3): без setExpression(None) присвоение .Value молча
    # откатывается на recompute — и подъём клиренса над боксами не доедет
    op.setExpression("ClearanceHeight", None)
    op.ClearanceHeight.Value = op_clearance(p, start_z)  # не ниже верха запретных боксов
    op.setExpression("SafeHeight", None)
    op.SafeHeight.Value = start_z + 3.0
    doc.recompute()

    n = len(op.Path.Commands) if op.Path else 0
    log(f"{name}: {n} команд (контурный проход)")
    return op if n > 2 else None


def make_surface_rough(doc, job, tc, name, model_obj, face_idx, p,
                       start_z, final_z, allowance):
    """Черновая наклонной/криволинейной грани «террасами»: Path Surface
    (drop cutter — фреза опускается сверху до поверхности) в режиме Multi-pass:
    слоями StepDown, с вертикальным смещением DepthOffset = припуск, зона —
    только эта грань модели (BoundBox=BaseBoundBox). Плоская фреза оставляет
    на наклоне ступеньки высотой до StepDown — их снимает чистовой проход."""
    import Path.Op.Surface as Surface
    op = Surface.Create(name, parentJob=job)
    op.ToolController = tc
    op.Base = [(model_obj, f"Face{face_idx}")]
    set_prop(op, "BoundBox", "BaseBoundBox")
    set_prop(op, "ScanType", "Planar")
    set_prop(op, "LayerMode", "Multi-pass")
    set_prop(op, "CutMode", "Climb")
    # грань обычно УЖЕ фрезы (радиус гиба, скос): рисунок Offset отступил бы
    # от границы на радиус и не оставил ничего. ZigZag + расширение границы
    # на радиус фрезы дают полное покрытие; по высоте фрезу всё равно ведёт
    # поверхность модели (+DepthOffset), зарезаться в соседей она не может.
    set_prop(op, "CutPattern", "ZigZag")
    set_prop(op, "BoundaryAdjustment",
             FreeCAD.Units.Quantity(f"{float(p['tool_diameter']) / 2.0} mm"))
    set_prop(op, "BoundaryEnforcement", False)
    set_prop(op, "StepOver", int(p["rough_stepover"]))
    set_prop(op, "SampleInterval",
             FreeCAD.Units.Quantity(f"{max(float(p['rough_tolerance']), 0.2)} mm"))
    set_prop(op, "DepthOffset", FreeCAD.Units.Quantity(f"{allowance} mm"))
    op.setExpression("StepDown", None)
    set_prop(op, "StepDown", p["rough_stepdown"])
    op.setExpression("StartDepth", None)
    op.StartDepth = start_z
    op.setExpression("FinalDepth", None)
    op.FinalDepth = final_z
    # ClearanceHeight/SafeHeight тоже привязаны экспрешеном к SetupSheet
    # (сток-топ + 5/3): без setExpression(None) присвоение .Value молча
    # откатывается на recompute — и подъём клиренса над боксами не доедет
    op.setExpression("ClearanceHeight", None)
    op.ClearanceHeight.Value = op_clearance(p, start_z)  # не ниже верха запретных боксов
    op.setExpression("SafeHeight", None)
    op.SafeHeight.Value = start_z + 3.0
    doc.recompute()

    n = len(op.Path.Commands) if op.Path else 0
    log(f"{name}: {n} команд (террасы по поверхности)")
    return op if n > 2 else None


def make_roughing_ops(doc, job, tc, shape, p, region_stock=None):
    """Черновая «по-человечески» — раздельными операциями в порядке техпроцесса:
      1. RoughContour — контур: материал между силуэтом детали и краем заготовки
         (существует только при STOCK_MARGIN > 0);
      2. RoughHole<N> — сквозные вырезы ПРОИЗВОЛЬНОЙ формы, по очереди;
      3. RoughFace<N> — каждая плоская грань («контур» в терминах NX) отдельной
         операцией: полки, донья карманов, уступы — сверху вниз.
    Зоны не пересекаются. Недоступное сверху (нависания — 2-й установ) и наклонные
    поверхности черновая НЕ трогает: наклонные — задача чистовой (FINISH).
    Припуск: по стенкам StockToLeave, по дну — глубиной FinalDepth."""
    allowance = round(p["rough_allowance"], 1)  # шаг 0.1 мм
    bb = shape.BoundBox
    sb = job.Stock.Shape.BoundBox
    start_z = sb.ZMax                      # верх заготовки
    floor_z = bb.ZMin + allowance          # дно + припуск
    zones = p.get("_zones")
    if zones:
        zfl = zone_z_floor(zones)
        if zfl > floor_z:
            log(f"зоны: дно черновой поднято с {floor_z:.2f} до {zfl:.2f} — "
                f"ниже пола зон материал останется")
            floor_z = zfl

    sil = build_silhouette(shape, bb, p["rough_stepdown"])
    ops = []
    # регионы строим от НЕпроплюженной заготовки (region_stock): запретные
    # колонны нужны только планировщику Adaptive (job.Stock), а в геометрии
    # регионов они дали бы «ореол» — кольцо ⊕ Ø вокруг колонн, т.е. холостые
    # заезды далеко за реальную заготовку
    stock_shape = region_stock if region_stock is not None else job.Stock.Shape

    def local_start(region_shape):
        """Локальный верх материала над зоной: колонна над зоной ∩ заготовка.
        None = материала над зоной нет вообще (зону пропускаем). Заготовка —
        не обязательно бокс с ровным верхом (уголок, отливка): стартовать все
        операции с верха ГАБАРИТА — значит резать воздух десятками слоёв."""
        try:
            base = region_shape.copy()
            base.translate(FreeCAD.Vector(0, 0, sb.ZMin - 1.0))
            prisms = [f.extrude(FreeCAD.Vector(0, 0, sb.ZLength + 2.0))
                      for f in base.Faces]
            m = stock_shape.common(Part.makeCompound(prisms))
            if m.Volume < 0.5:
                return None
            return min(m.BoundBox.ZMax, sb.ZMax)
        except Exception as e:
            log(f"warn: локальный верх зоны не посчитался ({e}) — беру верх заготовки")
            return sb.ZMax

    # ── 1) контур: материал между силуэтом детали и краем заготовки.
    #      Существует и при полях 0: заготовка — всегда БОКС, а деталь в плане
    #      обычно не прямоугольник (скругления углов, выступы дают материал
    #      по периметру). Широкие поля (≥ 2 диаметров) — адаптивная выборка
    #      кольца; узкие — контурный проход (Profile): адаптивной негде сделать
    #      винтовой заход, а проходу по контуру заход не нужен. ──
    if sil is None:
        log("warn: силуэт не построился — контур и вырезы пропущены")
    else:
        try:
            # вычитается ЗАЛИТЫЙ силуэт (только внешние контуры): если вычесть
            # силуэт с отверстиями, сквозные вырезы «провалятся» в зону контура
            # и будут фрезероваться дважды (они — этап 2, RoughHole)
            filled = Part.makeFace([f.OuterWire for f in sil.Faces],
                                   "Part::FaceMakerBullseye")
            # силуэт ЗАГОТОВКИ: для бокса это прямоугольник, для произвольной
            # заготовки из файла — её реальный контур в плане
            stock_sil = build_silhouette(stock_shape, sb, p["rough_stepdown"])
            stock_filled = Part.makeFace([f.OuterWire for f in stock_sil.Faces],
                                         "Part::FaceMakerBullseye")
            material = stock_filled.cut(filled)  # реальный материал по периметру
        except Exception as e:
            material = filled = None
            log(f"warn: зона контура не построилась: {e}")
        # мёртвые зоны: из материала периметра вычитается запретный футпринт
        # (Adaptive держит диск фрезы внутри зоны — хватает зазора m)
        if material is not None:
            material, _ = restrict_region(zones, material,
                                          zones["grow"] if zones else 0.0,
                                          floor_z, "контур")
        if material is not None and material.Area > 1.0:
            tool_d = float(p["tool_diameter"])
            margin_xy = min(sb.XLength - bb.XLength, sb.YLength - bb.YLength) / 2.0
            # При зонах контур ВСЕГДА адаптивным кольцом, даже при узких полях:
            # Profile-петля обводит ВНЕШНИЙ КОНТУР своей грани, и если из грани
            # выгрызена зона — контур идёт по стенкам выгрыза, а те проходят
            # ВНУТРИ детали (ловлено на реальном уголке: траншеи поперёк тела
            # детали при чистом гейте). Adaptive же в урезанный регион не
            # заходит вовсе; его воздушные входы/линки закрыты запретными
            # колоннами в заготовке, а винтовой заход в узком поле не нужен —
            # кольцо выпущено за край, вход сбоку из воздуха (как в layers).
            if zones is not None or margin_xy >= 2.0 * tool_d:
                # адаптивная выборка кольца. Зона выпускается за край заготовки
                # (снаружи воздух) — иначе StockToLeave оставит кожуру 0.5 мм
                # у самого края
                try:
                    ring = stock_filled.makeOffset2D(tool_d + allowance).cut(filled)
                except Exception:
                    ring = material
                ring, _ = restrict_region(zones, ring,
                                          zones["grow"] if zones else 0.0,
                                          floor_z, "контур (кольцо)")
                ring_top = local_start(material) if ring is not None else None
                if ring_top is None:
                    log("контур: материала по периметру нет — пропущено")
                else:
                    op = make_adaptive(doc, job, tc, "RoughContour", ring, p,
                                       ring_top, floor_z, allowance)
                    if op:
                        ops.append(op)
                        write_partial(job, ops, p, "готов контур")
                    else:
                        log("контур: пустая траектория — фреза не прошла по периметру")
            else:
                # узкие/неравномерные поля БЕЗ ЗОН: контурные петли СНАРУЖИ
                # ВНУТРЬ, каждая петля — отдельная операция (Profile с
                # несколькими гранями обводит только общий внешний контур).
                # Одна петля снимает полосу шириной в фрезу; материал дальше
                # (наплывы произвольной заготовки) добирают внешние петли.
                step = tool_d * float(p["rough_stepover"]) / 100.0
                mb = material.BoundBox
                pts = [v.Point for v in material.Vertexes]
                pts += [FreeCAD.Vector(x, y, 0)
                        for x in (mb.XMin, mb.XMax) for y in (mb.YMin, mb.YMax)]
                dmax = max(filled.distToShape(Part.Vertex(pt))[0] for pt in pts)
                n_loops = max(1, min(50, int(math.ceil(
                    (dmax - tool_d - allowance) / step)) + 1))
                loop_faces = []
                for k in range(n_loops - 1, 0, -1):
                    try:
                        loop_faces.append((k, Part.makeCompound(
                            filled.makeOffset2D(k * step).Faces)))
                    except Exception as e:
                        log(f"warn: петля контура на отступе {k * step:.1f} мм "
                            f"не построилась: {e}")
                loop_faces.append((0, Part.makeCompound(filled.Faces)))  # вдоль детали
                if n_loops > 1:
                    log(f"контур: {n_loops} петель — материал заготовки до "
                        f"{dmax:.1f} мм от детали")
                made = 0
                for i, (k, lf) in enumerate(loop_faces, 1):
                    # верх материала в полосе, которую метёт эта петля:
                    # петля выше него — чистый воздух, начинаем оттуда
                    loop_top = start_z
                    try:
                        band = filled.makeOffset2D(
                            k * step + tool_d + allowance).cut(lf)
                        loop_top = local_start(band)
                    except Exception:
                        pass
                    if loop_top is None:
                        log(f"контур: петля {i} — материала нет, пропущена")
                        continue
                    name = f"RoughContour{i}" if len(loop_faces) > 1 else "RoughContour"
                    op = make_profile(doc, job, tc, name, lf, p,
                                      loop_top, floor_z, allowance)
                    if op:
                        ops.append(op)
                        made += 1
                if made:
                    write_partial(job, ops, p, f"готов контур ({made} петель)")
                else:
                    log("контур: пустая траектория — фреза не прошла по периметру")

    # ── 2) сквозные вырезы любой формы, по очереди ──
    for i, region in enumerate(find_through_cuts(sil) if sil is not None else [], 1):
        rb = region.BoundBox
        region, _ = restrict_region(zones, region, zones["grow"] if zones else 0.0,
                                    floor_z, f"RoughHole{i}")
        if region is None:
            continue
        hole_top = local_start(region)
        if hole_top is None:
            log(f"RoughHole{i}: над вырезом нет материала заготовки — пропущено")
            continue
        op = make_adaptive(doc, job, tc, f"RoughHole{i}", region, p,
                           hole_top, floor_z, allowance)
        if op:
            ops.append(op)
            write_partial(job, ops, p, f"готов вырез {i} "
                                       f"(~{rb.XLength:.0f}x{rb.YLength:.0f} мм)")
        else:
            log(f"RoughHole{i}: фреза Ø{p['tool_diameter']} с припуском не влезает "
                f"в вырез ~{rb.XLength:.0f}x{rb.YLength:.0f} мм — пропущено")

    # ── 3) плоские грани («контуры» из NX), каждая отдельной операцией.
    #      Верхние грани детали включаются, если заготовка выше детали
    #      (уголок, верхнее поле) — над ними тогда есть материал. ──
    jm = job.Model.Group[0]  # клон модели внутри Job — его грани идут в Base операций
    include_top = sb.ZMax > bb.ZMax + 0.01
    for j, fc in enumerate(find_up_faces(shape, bb, include_top), 1):
        final = fc["z"] + allowance
        if zones:
            zfl = zone_z_floor(zones)
            if zfl > final:
                final = zfl  # дно грани ниже пола зон — останавливаемся выше
        region = fc["region"]
        region, _ = restrict_region(zones, region, zones["grow"] if zones else 0.0,
                                    final, f"RoughFace{j}")
        if region is None:
            continue
        face_top = local_start(region)
        if face_top is None or final >= face_top - 1e-6:
            if face_top is None:
                log(f"RoughFace{j}: (Z={fc['z']:.1f}) материала над гранью нет — "
                    f"пропущено")
            continue  # грань вровень с верхом материала — снимать нечего
        op = make_adaptive(doc, job, tc, f"RoughFace{j}", region, p,
                           face_top, final, allowance)
        if not op:
            # грань уже фрезы (узкая полка) — адаптивной выборке негде
            # развернуться; снимаем материал над ней террасами по поверхности,
            # тем же приёмом, что и криволинейные грани
            blk = surface_zone_block(zones, fc["region"].BoundBox, final)
            if blk:
                log(f"RoughFace{j}: узкая грань у мёртвой зоны ({blk}) — "
                    f"пропущено, материал остаётся")
                continue
            log(f"RoughFace{j}: узкая грань — перехожу на террасы по поверхности")
            op = make_surface_rough(doc, job, tc, f"RoughFace{j}", jm, fc["idx"], p,
                                    face_top, final, allowance)
        if op:
            ops.append(op)
            write_partial(job, ops, p, f"готова грань {j} "
                                       f"(Z={fc['z']:.1f}, {fc['area']:.0f} мм²)")
        else:
            log(f"RoughFace{j}: (Z={fc['z']:.1f}) пустая траектория — материал "
                f"недоступен, пропущено")

    # ── 4) наклонные/криволинейные грани — террасами, каждая отдельной операцией.
    #      Берём грани, у которых есть площадь в проекции на основание (нормаль
    #      хоть немного смотрит вверх) и над которыми нет материала. Смотрящие
    #      вниз/вбок или накрытые — недоступны сверху, это второй установ. ──
    def is_handled(f):
        s = type(f.Surface).__name__
        nz = f.normalAt(0, 0).z if s == "Plane" else None
        if s == "Plane" and (abs(nz) > 0.999 or abs(nz) < 0.001):
            return True    # горизонтальные (этап 3) и вертикальные плоскости (стенки)
        if s == "Cylinder" and abs(f.Surface.Axis.z) > 0.999:
            return True    # вертикальные цилиндрические стенки/скругления
        return False

    slopes, skipped = [], 0.0
    for idx, f in enumerate(jm.Shape.Faces, 1):
        if is_handled(f) or f.Area < 1.0:
            continue
        try:
            u0, u1, v0, v1 = f.ParameterRange
            mid = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
            nz = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2).z
        except Exception:
            skipped += f.Area
            continue
        if nz < 0.01:      # смотрит вниз или строго вбок — проекции на основание нет
            skipped += f.Area
            continue
        probe = FreeCAD.Vector(mid.x, mid.y, f.BoundBox.ZMax + 0.3)
        if shape.isInside(probe, 1e-6, True):
            skipped += f.Area   # накрыта материалом — сверху не добраться
            continue
        fbb = f.BoundBox
        rect = Part.Face(Part.makePolygon([
            FreeCAD.Vector(fbb.XMin, fbb.YMin, 0), FreeCAD.Vector(fbb.XMax, fbb.YMin, 0),
            FreeCAD.Vector(fbb.XMax, fbb.YMax, 0), FreeCAD.Vector(fbb.XMin, fbb.YMax, 0),
            FreeCAD.Vector(fbb.XMin, fbb.YMin, 0)]))
        slopes.append({"idx": idx, "z": fbb.ZMax, "zmin": fbb.ZMin,
                       "area": f.Area, "cx": mid.x, "cy": mid.y, "rect": rect})
    levels = sorted({round(s["z"], 3) for s in slopes}, reverse=True)
    ordered = []
    for lv in levels:
        ordered += _nearest_order([s for s in slopes if round(s["z"], 3) == lv],
                                  lambda s: (s["cx"], s["cy"]))
    for k, s in enumerate(ordered, 1):
        slope_final = s["zmin"] + allowance
        if zones:
            slope_final = max(slope_final, zone_z_floor(zones))
            blk = surface_zone_block(zones, s["rect"].BoundBox, slope_final)
            if blk:
                log(f"RoughSlope{k}: {blk} — пропущено, материал остаётся "
                    f"(Surface не умеет объезжать зоны)")
                continue
        slope_top = local_start(s["rect"])  # верх материала над зоной грани
        if slope_top is None:
            log(f"RoughSlope{k}: материала над гранью нет — пропущено")
            continue
        op = make_surface_rough(doc, job, tc, f"RoughSlope{k}", jm, s["idx"], p,
                                slope_top, slope_final, allowance)
        if op:
            ops.append(op)
            write_partial(job, ops, p, f"готова криволинейная грань {k} "
                                       f"({s['area']:.0f} мм²)")
        else:
            log(f"RoughSlope{k}: пустая траектория — пропущено")
    if skipped > 1.0:
        log(f"warn: {skipped:.0f} мм² поверхностей смотрят вниз/вбок или накрыты "
            f"материалом — сверху не достать, это второй установ")

    if ops:
        log(f"черновая: припуск {allowance} мм, слой {p['rough_stepdown']} мм, "
            f"этапов: {len(ops)} ({', '.join(o.Label for o in ops)})")
    else:
        log("warn: черновая не дала ни одной операции")
    return ops


def make_layered_ops(doc, job, tc, shape, p, region_stock=None):
    """ЭКСПЕРИМЕНТ (--rough-mode layers): послойная черновая «как технолог в NX
    делает Cavity Mill». Ничего не угадывает про типы фич — на каждой высоте
    ответ точный:  материал(Z) = проекция заготовки − тень детали выше Z.
    Высоты режутся диапазонами между характерными уровнями детали (верхи полок,
    дно), наклонные/криволинейные участки дробятся по ROUGH_STEPDOWN. Каждая
    связная область диапазона — отдельная операция Adaptive (Contour — область
    у края заготовки, Pocket — замкнутая внутри); Adaptive знает заготовку и
    пропускает воздух. Припуск: по стенкам StockToLeave, по полкам — границы
    диапазонов сдвинуты на припуск выше граней."""
    allowance = round(p["rough_allowance"], 1)
    step = float(p["rough_stepdown"])
    bb = shape.BoundBox
    sb = job.Stock.Shape.BoundBox
    ops = []
    zones = p.get("_zones")

    region_stock = region_stock if region_stock is not None else job.Stock.Shape
    ssil = build_silhouette(region_stock, sb, step)
    if ssil is None:
        log("warn: силуэт заготовки не построился — послойная черновая невозможна")
        return ops
    stock_filled = Part.makeFace([f.OuterWire for f in ssil.Faces],
                                 "Part::FaceMakerBullseye")

    # ── 1) характерные уровни детали ──
    bottom = bb.ZMin + allowance
    if zones:
        zfl = zone_z_floor(zones)
        if zfl > bottom:
            log(f"зоны: дно послойной черновой поднято с {bottom:.2f} до "
                f"{zfl:.2f} — ниже пола зон материал останется")
            bottom = zfl
    levels = {sb.ZMax, bottom}
    slant_spans = []
    for f in shape.Faces:
        s = type(f.Surface).__name__
        fz1, fz2 = f.BoundBox.ZMin, f.BoundBox.ZMax
        if s == "Plane":
            nz = f.normalAt(0, 0).z
            if nz > 0.999:
                levels.add(fz2 + allowance)  # полка: дно диапазона = грань + припуск
                continue
            if nz < -0.999:
                levels.add(fz1)
                continue
            if abs(nz) < 0.001:
                continue                     # вертикальная стенка уровней не даёт
        if s == "Cylinder" and abs(f.Surface.Axis.z) > 0.999:
            continue                         # вертикальная цилиндрическая стенка
        if fz2 - fz1 > 0.01:
            slant_spans.append((fz1, fz2))   # скос/криволинейная грань
        else:
            levels.add(fz2)
    for z1, z2 in slant_spans:               # дробление наклонных участков по слою
        z = z2
        while z > z1:
            levels.add(z)
            z -= step
        levels.add(z1)
    raw = sorted({round(z, 3) for z in levels if bottom <= z <= sb.ZMax},
                 reverse=True)
    levels = []                              # слить уровни, слипшиеся в пределах 0.05
    for z in raw:
        if not levels or levels[-1] - z > 0.05:
            levels.append(z)
    levels[-1] = bottom

    # ── 2) сверху вниз: тень детали накапливается, зона диапазона =
    #      сечение ЗАГОТОВКИ в диапазоне − тень (Adaptive пропускает слои без
    #      материала, но внутри слоя метёт всю зону — глобальная проекция
    #      заготовки дала бы часы воздуха) ──
    shadow = None
    eps = 0.02
    bands = []   # [верх, низ, зона]
    for bi in range(len(levels) - 1):
        top_z, bot_z = levels[bi], levels[bi + 1]
        if top_z - bot_z < 0.005:
            continue
        # тень пополняется сечениями на границах диапазона (фреза, идущая до
        # bot_z, должна обходить весь материал детали выше bot_z)
        for z in (top_z - eps, bot_z + eps):
            for fc in _slice_faces(shape, z):
                shadow = fc if shadow is None else shadow.fuse(fc)
        try:
            if shadow is not None:
                shadow = shadow.removeSplitter()
        except Exception:
            pass
        stock_sec = None
        for z in (top_z - eps, bot_z + eps):
            for fc in _slice_faces(region_stock, z):
                stock_sec = fc if stock_sec is None else stock_sec.fuse(fc)
        if stock_sec is None:
            continue  # заготовки на этих высотах нет
        try:
            stock_sec = stock_sec.removeSplitter()
        except Exception:
            pass
        try:  # зона выпускается за край заготовки (там воздух). Это решает сразу
              # два случая: StockToLeave не оставляет кожуру 0.5 мм у края, и
              # тонкие рёбра заготовки (полка уголка уже фрезы) становятся
              # обрабатываемыми — Side=Inside в узкую зону не помещается.
              # makeOffset2D зовётся по-фасетно: на сшитом сечении он падает.
            grow = float(p["tool_diameter"]) + allowance
            parts = [f.makeOffset2D(grow) for f in stock_sec.Faces]
            stock_sec = parts[0] if len(parts) == 1 else parts[0].fuse(parts[1:])
        except Exception as e:
            log(f"warn: расширение зоны {top_z:.1f}..{bot_z:.1f} не удалось ({e}) — "
                f"узкие рёбра заготовки могут остаться необработанными")
        try:
            region = stock_sec if shadow is None else stock_sec.cut(shadow)
        except Exception as e:
            log(f"warn: зона диапазона {top_z:.1f}..{bot_z:.1f} не построилась: {e}")
            continue
        if region.Area < 1.0:
            continue
        bands.append([top_z, bot_z, region])

    def _same(a, b):     # зоны совпадают? (площадь и габарит в допуске)
        if len(a.Faces) != len(b.Faces):
            return False
        if abs(a.Area - b.Area) > max(0.5, 0.002 * a.Area):
            return False
        ab, bbx = a.BoundBox, b.BoundBox
        return all(abs(x - y) < 0.05 for x, y in (
            (ab.XMin, bbx.XMin), (ab.XMax, bbx.XMax),
            (ab.YMin, bbx.YMin), (ab.YMax, bbx.YMax)))

    merged = []          # склейка соседних диапазонов с одинаковой зоной
    for b in bands:
        if merged and abs(merged[-1][1] - b[0]) < 0.01 and _same(merged[-1][2], b[2]):
            merged[-1][1] = b[1]
        else:
            merged.append(b)
    log(f"послойная черновая: {len(merged)} диапазонов высот "
        f"({levels[0]:.1f}..{levels[-1]:.1f}), до склейки {len(bands)}")

    for bi, (top_z, bot_z, region) in enumerate(merged, 1):
        # мёртвые зоны: запретный футпринт по низу ИМЕННО этого диапазона —
        # боксы, чей верх ниже диапазона, рез выше себя не ограничивают
        region, _ = restrict_region(zones, region, zones["grow"] if zones else 0.0,
                                    bot_z, f"диапазон {bi}")
        if region is None:
            continue
        faces = _nearest_order([f for f in region.Faces if f.Area > 1.0],
                               lambda f: (f.CenterOfMass.x, f.CenterOfMass.y))
        for ri, rf in enumerate(faces, 1):
            try:  # у края заготовки — контур, замкнутая внутри — карман
                kind = ("Contour" if rf.distToShape(stock_filled.OuterWire)[0] < 0.01
                        else "Pocket")
            except Exception:
                kind = "Region"
            op = make_adaptive(doc, job, tc, f"B{bi}{kind}{ri}", rf, p,
                               top_z, bot_z, allowance)
            if op:
                ops.append(op)
                write_partial(job, ops, p,
                              f"диапазон {bi} ({top_z:.1f}..{bot_z:.1f}), "
                              f"область {ri}")
    if ops:
        log(f"послойная черновая: {len(ops)} операций")
    else:
        log("warn: послойная черновая не дала ни одной операции")
    return ops


def mill(doc, feat, p, stock_solid=None):
    """Последовательная обработка: черновая по этапам (контур → отверстия →
    остальное) и, если включена, чистовая по поверхности. → текст G-Code.
    stock_solid — произвольная заготовка из файла (уже в координатах детали);
    None — заготовка = габаритный бокс детали + поля."""
    bb = feat.Shape.BoundBox

    import Path.Main.Job as Job
    job = Job.Create("Job", [feat])
    doc.recompute()
    if stock_solid is not None:
        # произвольная заготовка: CreateFromExisting в этой версии API нет,
        # но Job принимает любой объект с Shape — операции читают job.Stock.Shape
        default_stock = job.Stock
        stock_feat = doc.addObject("Part::Feature", "StockSolid")
        stock_feat.Shape = stock_solid
        job.Stock = stock_feat
        try:
            doc.removeObject(default_stock.Name)
        except Exception:
            pass
        doc.recompute()
        sb = job.Stock.Shape.BoundBox
        stock_note = f"из файла {os.path.basename(p['stock_file'])}"
        log(f"заготовка: {sb.XLength:.1f} x {sb.YLength:.1f} x {sb.ZLength:.1f} мм "
            f"({stock_note})")
        for axis, d in (("X", bb.XMin - sb.XMin), ("X", sb.XMax - bb.XMax),
                        ("Y", bb.YMin - sb.YMin), ("Y", sb.YMax - bb.YMax),
                        ("Z", sb.ZMax - bb.ZMax), ("Z", bb.ZMin - sb.ZMin)):
            if d < -0.1:   # допуск на округления экспорта STEP
                log(f"warn: деталь выступает из заготовки по {axis} на {-d:.2f} мм — "
                    f"проверьте, что файлы в одной системе координат")
                break
    else:
        # заготовка = габарит детали + поля STOCK_MARGIN (XY) / STOCK_MARGIN_TOP (Z+)
        margin = float(p.get("stock_margin", 1.0))
        for prop in ("ExtXneg", "ExtXpos", "ExtYneg", "ExtYpos"):
            set_prop(job.Stock, prop, FreeCAD.Units.Quantity(f"{margin} mm"))
        set_prop(job.Stock, "ExtZpos",
                 FreeCAD.Units.Quantity(f"{float(p.get('stock_margin_top', 0.0))} mm"))
        set_prop(job.Stock, "ExtZneg", FreeCAD.Units.Quantity("0 mm"))
        doc.recompute()
        sb = job.Stock.Shape.BoundBox
        stock_note = f"деталь + поля {margin}/{p.get('stock_margin_top', 0.0)} мм"
        log(f"заготовка: {sb.XLength:.1f} x {sb.YLength:.1f} x {sb.ZLength:.1f} мм "
            f"({stock_note})")
    zones = p.get("_zones")
    region_stock = None
    if zones:
        if p.get("finish"):
            # дублирует гард хоста: worker могут запустить и в обход run_cam
            raise RuntimeError("мёртвые зоны поддерживаются только для черновой "
                               "(v1) — отключите чистовую (--no-finish)")
        check_zone_heights(zones, sb)   # потолок/пол зон против высот заготовки
        warn_zone_sanity(zones, sb)     # зоны мимо заготовки = вероятно, не та СК
        region_stock = job.Stock.Shape  # геометрия регионов — БЕЗ колонн
        plug_stock_for_zones(doc, job, sb, zones, p)  # колонны от воздушных
        # шорткатов Adaptive за краем заготовки (вход/линки на глубине реза)

    tc = job.Tools.Group[0]
    tool_d = float(p["tool_diameter"])
    set_prop(tc.Tool, "Diameter", FreeCAD.Units.Quantity(f"{tool_d} mm"))
    tc.HorizFeed = FreeCAD.Units.Quantity(f"{p['feed_rate']} mm/min")
    tc.VertFeed = FreeCAD.Units.Quantity(f"{p['feed_rate'] / 4.0} mm/min")
    tc.SpindleSpeed = float(p["spindle_speed"])
    # дефолтный инструмент Job зовётся «5mm Endmill» независимо от диаметра —
    # переименовываем, чтобы комментарий (TC: ...) в G-Code не врал
    tc.Label = f"TC: Endmill D{tool_d:g}mm"
    tc.Tool.Label = f"Endmill D{tool_d:g}mm"
    doc.recompute()

    # в описание идёт только диаметр — единственный размер, который мы задаём;
    # остальные размеры (длина, хвостовик) у дефолтного инструмента FreeCAD —
    # библиотечные заглушки, печатать их в программу опасно
    tool_desc = f"endmill (flat), D{tool_d:g} mm"
    log(f"фреза: концевая плоская (endmill) Ø{tool_d:g} мм | "
        f"подача {p['feed_rate']:g} мм/мин | шпиндель {p['spindle_speed']:g} об/мин")

    # шапка G-Code: заготовка/деталь/инструмент комментарием (латиницей — кириллицу
    # в комментариях понимает не каждая стойка). Координаты — в нуле программы.
    stock_src = (f", from file {os.path.basename(p['stock_file'])}"
                 if stock_solid is not None else "")
    p["_gcode_header"] = (
        f"(Stock: {sb.XLength:.1f} x {sb.YLength:.1f} x {sb.ZLength:.1f} mm{stock_src})\n"
        f"(Stock box: X {sb.XMin:.1f}..{sb.XMax:.1f}  Y {sb.YMin:.1f}..{sb.YMax:.1f}"
        f"  Z {sb.ZMin:.1f}..{sb.ZMax:.1f})\n"
        f"(Part: {bb.XLength:.1f} x {bb.YLength:.1f} x {bb.ZLength:.1f} mm, "
        f"X0 Y0 Z0 = {p.get('origin', 'corner-top')})\n"
        f"(Tool: {tool_desc}, feed {p['feed_rate']:g} mm/min, "
        f"spindle {p['spindle_speed']:g} rpm)\n"
        + zone_header_lines(zones)
    )

    ops = []
    if p.get("rough_enabled", True):        # припуск (в т.ч. 0 = до номинала) — отдельно
        if p.get("rough_mode", "stages") == "layers":
            ops.extend(make_layered_ops(doc, job, tc, feat.Shape, p, region_stock))
        else:
            ops.extend(make_roughing_ops(doc, job, tc, feat.Shape, p, region_stock))
    else:
        log("черновая отключена (--no-rough)")

    if p.get("finish", False):
        import Path.Op.Surface as Surface
        surf = Surface.Create("Finish", parentJob=job)
        surf.ToolController = tc
        set_prop(surf, "CutPattern", p["cut_pattern"])
        set_prop(surf, "CutMode", "Climb")
        set_prop(surf, "LayerMode", "Single-pass")
        set_prop(surf, "ScanType", "Planar")
        set_prop(surf, "BoundBox", "BaseBoundBox")  # границы обработки = модель
        set_prop(surf, "StepOver", int(p["stepover"]))
        set_prop(surf, "SampleInterval",
                 FreeCAD.Units.Quantity(f"{p['sample_interval']} mm"))
        # setExpression(None) снимает привязку к SetupSheet — иначе recompute вернёт дефолт
        surf.setExpression("StartDepth", None)
        surf.StartDepth = bb.ZMax
        surf.setExpression("FinalDepth", None)
        surf.FinalDepth = bb.ZMin
        surf.ClearanceHeight.Value = bb.ZMax + p["safe_height"]
        surf.SafeHeight.Value = bb.ZMax + 3.0
        doc.recompute()
        n = len(surf.Path.Commands) if surf.Path else 0
        log(f"Finish (surface): {n} команд")
        if n == 0:
            raise RuntimeError("Surface не дала траекторию (проверьте модель и параметры)")
        ops.append(surf)
    else:
        log("чистовая отключена (FINISH=false)")

    if not ops:
        raise RuntimeError("ни одной операции с траекторией — проверьте параметры")
    # порядок операций = порядок выполнения
    return p["_gcode_header"] + export_gcode(job, ops, p["postprocessor"])


# ── Экспорт эталона и масок достижимости для verify.py (флаг --verify-export) ─────
def _write_faces_step(solid, idxs, path):
    """Пишет подмножество граней тела в STEP (точное BREP). False, если граней нет.
    Через Shape.exportStep — Part.export ждёт документные объекты, не сырой Shape."""
    if not idxs:
        return False
    Part.makeCompound([solid.Faces[i] for i in idxs]).exportStep(path)
    return True


def classify_reachable_faces(solid):
    """Делит грани детали на достижимые сверху (3-осевой установ, ось +Z) и
    недостижимые («второй установ»). Достижимо: нормаль смотрит вверх ИЛИ грань —
    вертикальная стенка, и сверху не накрыта материалом. Недостижимо: нормаль вниз
    (низ детали, подрезы, низ нависаний) или грань накрыта телом при взгляде сверху.
    Внешняя нормаль определяется геометрически (проба по обе стороны грани), а не по
    флагу Orientation — устойчиво к тому, как ориентированы грани в импортированном
    STEP. Возвращает (reachable_idx, unreachable_idx) — индексы в solid.Faces."""
    UP = 0.01
    reachable, unreachable = [], []
    for i, f in enumerate(solid.Faces):
        if f.Area < 1e-3:
            continue
        try:
            u0, u1, v0, v1 = f.ParameterRange
            mid = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
            n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
            n.normalize()
        except Exception:
            unreachable.append(i)          # не смогли оценить — в «второй установ»
            continue
        eps = 1e-3
        p_out = FreeCAD.Vector(mid.x + n.x * eps, mid.y + n.y * eps, mid.z + n.z * eps)
        p_in = FreeCAD.Vector(mid.x - n.x * eps, mid.y - n.y * eps, mid.z - n.z * eps)
        nz = n.z
        # если «наружу» по нормали оказался материал, а «внутрь» — воздух, нормаль
        # смотрит внутрь тела → берём её со знаком минус (истинная внешняя нормаль)
        if solid.isInside(p_out, 1e-6, True) and not solid.isInside(p_in, 1e-6, True):
            nz = -nz
        probe = FreeCAD.Vector(mid.x, mid.y, f.BoundBox.ZMax + 0.3)
        covered = solid.isInside(probe, 1e-6, True)
        if covered or nz < -UP:
            unreachable.append(i)
        else:
            reachable.append(i)
    return reachable, unreachable


def export_verify(solid, base):
    """Пишет эталон и маски достижимых/недостижимых граней в STEP (точное BREP)
    рядом с G-кодом — вход для verify.py. Всё в текущей (ориентированной, сдвинутой)
    СК, ровно как у G-кода и у результата симуляции."""
    solid.exportStep(base + "_part.step")
    log(f"verify-export: эталон → {os.path.basename(base)}_part.step")
    reach, unreach = classify_reachable_faces(solid)
    if _write_faces_step(solid, reach, base + "_reachable.step"):
        log(f"verify-export: достижимые грани ({len(reach)}) → "
            f"{os.path.basename(base)}_reachable.step")
    if _write_faces_step(solid, unreach, base + "_unreachable.step"):
        log(f"verify-export: недостижимые грани, второй установ ({len(unreach)}) → "
            f"{os.path.basename(base)}_unreachable.step")


def main():
    with open(os.environ["FREECAD_WORKER_PARAMS"]) as f:
        p = json.load(f)

    p["_zones"] = parse_zones(p)   # мёртвые зоны (или None) — в СК программы
    if p["_zones"]:
        z = p["_zones"]
        log(f"мёртвые зоны: запретных боксов {len(z['boxes'])}, "
            f"рабочий бокс: {'да' if z['work'] is not None else 'нет'}, "
            f"полупространств {len(z['half'])}; отступ R{z['r']:g}+{z['m']:g} мм")

    doc = FreeCAD.newDocument("CAM")
    solid = load_model(p["model_path"], p.get("scale_to_mm", 1.0))
    if not solid.isValid():
        log("warn: solid is not valid (mesh may be non-watertight)")
    journal = []   # трансформации детали — повторяются на заготовке из файла
    if p.get("auto_orient", True):
        solid = auto_orient(solid, journal)
        solid = orient_features_up(solid, journal)
    solid = normalize_origin(solid, p.get("origin", "corner-top"), journal)
    bb = solid.BoundBox
    log(f"solid mm: {bb.XLength:.2f} x {bb.YLength:.2f} x {bb.ZLength:.2f}")

    stock_solid = None
    if p.get("stock_file"):
        stock_solid = load_model(p["stock_file"], p.get("scale_to_mm", 1.0))
        if not stock_solid.isValid():
            # невалидное тело ломает Adaptive (пустые траектории) и булевы
            # операции зон — пробуем сшить и собрать заново
            log("warn: тело заготовки невалидно — пробую починить (sew + makeSolid)")
            try:
                sh = stock_solid.copy()
                sh.sewShape()
                sh = Part.makeSolid(sh)
                if sh.isValid() and sh.Volume > 0:
                    stock_solid = sh
                    log("заготовка починена")
                else:
                    log("warn: починить не удалось — операции могут выйти пустыми, "
                        "проверьте файл заготовки")
            except Exception as e:
                log(f"warn: починка заготовки не удалась ({e}) — "
                    f"операции могут выйти пустыми")
        stock_solid = apply_transforms(stock_solid, journal)
        log("заготовка из файла повёрнута/сдвинута вместе с деталью")

    feat = doc.addObject("Part::Feature", "Model")
    feat.Shape = solid
    doc.recompute()

    # Опционально: экспорт детали и заготовки в STEP в ТЕКУЩЕЙ (ориентированной,
    # сдвинутой) системе координат — ровно в той, что у G-кода. Для симуляции в NX:
    # импортируешь эти STEP, MCS в нуле — и всё встаёт под траекторию.
    if p.get("nx_export"):
        base = os.path.splitext(p["gcode_path"])[0]
        Part.export([feat], base + "_part.step")
        log(f"NX-export: деталь → {os.path.basename(base)}_part.step")
        if stock_solid is not None:
            sfeat = doc.addObject("Part::Feature", "Stock")
            sfeat.Shape = stock_solid
            doc.recompute()
            Part.export([sfeat], base + "_stock.step")
            log(f"NX-export: заготовка → {os.path.basename(base)}_stock.step")
        else:
            log("NX-export: заготовка = бокс, STEP не пишу — создай блок в NX по шапке (Stock box)")

    if p.get("verify_export"):
        try:
            export_verify(solid, os.path.splitext(p["gcode_path"])[0])
        except Exception as e:
            log(f"verify-export: не удалось ({e})")

    gcode = mill(doc, feat, p, stock_solid)

    if p["_zones"]:
        enforce_zone_gate(p["_zones"], gcode, p["gcode_path"])

    with open(p["gcode_path"], "w", encoding="utf-8") as f:
        f.write(gcode)
    log(f"OK gcode_lines={gcode.count(chr(10)) + 1} path={p['gcode_path']}")


# freecadcmd исполняет этот файл как скрипт (не как __main__), поэтому вызываем напрямую
if os.environ.get("FREECAD_WORKER_PARAMS"):
    main()
