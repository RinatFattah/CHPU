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


def surf_name(f):
    """Имя типа поверхности грани ('Plane'/'Cylinder'/...); '' если OCCT его не
    определяет. На невалидном теле f.Surface может бросать 'undefined surface
    type' — такие грани пропускаем, а не роняем весь расчёт."""
    try:
        return type(f.Surface).__name__
    except Exception:
        return ""


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
        if surf_name(f) == "Plane" and (best is None or f.Area > best.Area):
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


def orient_hole_axis_up(solid, journal=None):
    """Уголок может лечь auto_orient'ом на СТЕНКУ (её грань бывает больше полки) —
    тогда отверстия полки смотрят вбок и деталь необрабатываема. Доворачивает
    деталь так, чтобы доминирующая ось ОТВЕРСТИЙ стала вертикальной.
    Отверстие = вогнутый цилиндр с охватом >= 180° (радиусы гиба — четверть
    цилиндра — не в счёт, иначе ось гиба перепутается с осью отверстия)."""
    groups = []   # [ось, суммарная площадь]
    for f in solid.Faces:
        if surf_name(f) != "Cylinder":
            continue
        try:
            u0, u1, v0, v1 = f.ParameterRange
            if (u1 - u0) < math.pi - 0.01:
                continue        # дуга < 180° — скругление/гиб, не отверстие
            pnt = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
            nrm = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
            s = f.Surface
            a = FreeCAD.Vector(s.Axis.x, s.Axis.y, s.Axis.z)
            a.normalize()
            v = pnt - s.Center
            radial = v - a * v.dot(a)
        except Exception:
            continue
        if radial.Length < 1e-9 or nrm.dot(radial) > 0:
            continue            # выпуклая стенка (бобышка), не отверстие
        for g in groups:
            if abs(g[0].dot(a)) > 0.99:
                g[1] += f.Area
                break
        else:
            groups.append([a, f.Area])
    if not groups:
        return solid
    dom = max(groups, key=lambda g: g[1])[0]
    if abs(dom.z) > 0.99:
        return solid            # ось отверстий уже вертикальна
    if dom.z < 0:
        dom = dom * -1.0
    target = FreeCAD.Vector(0, 0, 1)
    axis = dom.cross(target)
    if axis.Length < 1e-9:
        axis = FreeCAD.Vector(1, 0, 0)
    angle = math.degrees(dom.getAngle(target))
    solid = solid.copy()
    solid.rotate(FreeCAD.Vector(0, 0, 0), axis, angle)
    if journal is not None:
        journal.append(("rotate", ((axis.x, axis.y, axis.z), angle)))
    log(f"orient: ось отверстий смотрела вбок — деталь довёрнута на {angle:.1f}° "
        f"(отверстия вертикально)")
    return solid


def orient_flange_down(solid, journal=None):
    """Полка (самая большая горизонтальная грань) должна быть ВНИЗУ, стенка —
    торчать вверх. Если полка оказалась в верхней половине габарита — стенка
    свисает вниз нависанием (сверху не достать) — переворот на 180°."""
    bb = solid.BoundBox
    best = None
    for f in solid.Faces:
        if surf_name(f) == "Plane" and abs(f.normalAt(0, 0).z) > 0.999:
            if best is None or f.Area > best.Area:
                best = f
    if best is None or best.BoundBox.ZMax <= (bb.ZMin + bb.ZMax) / 2:
        return solid
    solid = solid.copy()
    solid.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(1, 0, 0), 180)
    if journal is not None:
        journal.append(("rotate", ((1.0, 0.0, 0.0), 180.0)))
    log("orient: полка была сверху (стенка нависала) — переворот на 180°")
    return solid


def _largest_vertical_face(solid):
    best_f, best_n = None, None
    for f in solid.Faces:
        if surf_name(f) != "Plane":
            continue
        n = f.normalAt(0, 0)
        if abs(n.z) > 0.001:
            continue
        if best_f is None or f.Area > best_f.Area:
            best_f, best_n = f, n
    return best_f, best_n


def orient_wall_to_yz(solid, journal=None):
    """Ставит вертикальную стенку уголка в плоскость YZ у края XMin — так
    деталь-уголок вкладывается в заготовку-уголок, стенка которой стоит по
    XMin (см. align_stock). Два шага: стенка смотрит вдоль Y (лежит в XZ) —
    поворот 90° вокруг Z; стенка у дальнего края (XMax) — доворот 180°."""
    f, n = _largest_vertical_face(solid)
    if f is None:
        return solid
    if abs(n.x) < abs(n.y):
        solid = solid.copy()
        solid.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), 90)
        if journal is not None:
            journal.append(("rotate", ((0.0, 0.0, 1.0), 90.0)))
        log("orient: стенка развёрнута в плоскость YZ (90° вокруг Z)")
        f, n = _largest_vertical_face(solid)
        if f is None:
            return solid
    bb = solid.BoundBox
    fx = (f.BoundBox.XMin + f.BoundBox.XMax) / 2
    if fx > (bb.XMin + bb.XMax) / 2:
        solid = solid.copy()
        solid.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), 180)
        if journal is not None:
            journal.append(("rotate", ((0.0, 0.0, 1.0), 180.0)))
        log("orient: стенка была у края XMax — доворот 180° (стенка к XMin, "
            "как у заготовки-уголка)")
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
        if surf_name(f) != "Cylinder":
            continue
        s = f.Surface
        if abs(s.Axis.z) < 0.999:
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


def align_stock(stock, part_bb):
    """Выравнивает заготовку ПО ДЕТАЛИ, игнорируя координаты файла (файл может
    быть привязан к другой детали сборки): кладёт заготовку плашмя, рёбра — по
    осям, затем «уголок в уголке» — как у исходной пары деталь/заготовка серии:
    X — край в край (XMin в XMin, запас уходит в +X), Y — центр в центр,
    Z — ДНО В ДНО (запас материала оказывается сверху, где фреза его снимет;
    снизу его не достать)."""
    s = auto_orient(stock.copy())          # наибольшая грань вниз
    # доворот вокруг Z: длинное прямое ребро нижней грани → вдоль оси
    best = None
    for f in s.Faces:
        if surf_name(f) == "Plane" and abs(f.normalAt(0, 0).z) > 0.999:
            if best is None or f.Area > best.Area:
                best = f
    if best is not None:
        edge_dir, elen = None, 0.0
        for e in best.Edges:
            if type(e.Curve).__name__ == "Line" and e.Length > elen:
                edge_dir, elen = e.Curve.Direction, e.Length
        if edge_dir is not None:
            ang = math.degrees(math.atan2(edge_dir.y, edge_dir.x)) % 90.0
            if ang > 45.0:
                ang -= 90.0
            if abs(ang) > 0.05:
                s.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), -ang)
    sb = s.BoundBox
    s.translate(FreeCAD.Vector(
        part_bb.XMin - sb.XMin,                                   # X: край в край
        (part_bb.YMin + part_bb.YMax) / 2 - (sb.YMin + sb.YMax) / 2,  # Y: центр
        part_bb.ZMin - sb.ZMin))                                  # Z: дно в дно
    return s


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


def export_stock(job, p):
    """Выгружает заготовку в координатах ПРОГРАММЫ (уже повёрнутую и сдвинутую
    вместе с деталью) в STEP рядом с G-Code — для симулятора и наладки.
    Работает и для бокса, и для произвольной заготовки из файла."""
    out = p.get("stock_out")
    if not out:
        return
    try:
        import shutil
        import tempfile
        # OCCT на Windows не пишет по путям с не-ASCII символами (кириллица в
        # C:\Users\<имя>) — экспорт во временную ASCII-папку, затем перенос
        tmp = os.path.join(tempfile.gettempdir(), "cam_stock_export.stp")
        job.Stock.Shape.exportStep(tmp)
        shutil.move(tmp, out)
        log(f"заготовка в координатах программы → {out}")
    except Exception as e:
        log(f"warn: экспорт заготовки не удался: {e}")


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
        if surf_name(f) != "Plane" or f.normalAt(0, 0).z < 0.999:
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
    final_z снизу ограничен зазором от стола (_floor_limit).
    Зона задаётся явной плоской областью: проекции граней модели на сложной
    детали дают незамкнутый контур (Path.Area: «ccurve not closed»), и операция
    молча выдаёт пустую траекторию."""
    final_z = max(final_z, p.get("_floor_limit", final_z))  # не ниже стола
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
    op.ClearanceHeight.Value = start_z + p["safe_height"]
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


def make_profile(doc, job, tc, name, region_shape, p, start_z, final_z, allowance,
                 side="Outside"):
    """Контурный проход (Path Profile): фреза обходит внешний контур зоны
    с отступом радиус + припуск, слоями StepDown до дна. Применяется для
    периметра при УЗКИХ полях (меньше ~2 диаметров): адаптивной выборке там
    негде сделать винтовой заход, а контурному проходу заход не нужен —
    он идёт по воздуху вокруг заготовки и срезает выступающий материал."""
    final_z = max(final_z, p.get("_floor_limit", final_z))  # не ниже стола
    region = doc.addObject("Part::Feature", f"Region{name}")
    region.Shape = region_shape
    doc.recompute()

    import Path.Op.Profile as Profile
    op = Profile.Create(name, parentJob=job)
    op.ToolController = tc
    op.Base = [(region, f"Face{i + 1}") for i in range(len(region.Shape.Faces))]
    set_prop(op, "Side", side)
    set_prop(op, "UseComp", True)  # смещение на радиус фрезы считается в софте
    set_prop(op, "OffsetExtra", FreeCAD.Units.Quantity(f"{allowance} mm"))
    op.setExpression("StepDown", None)
    set_prop(op, "StepDown", p["rough_stepdown"])
    op.setExpression("StartDepth", None)
    op.StartDepth = start_z
    op.setExpression("FinalDepth", None)
    op.FinalDepth = final_z
    op.ClearanceHeight.Value = start_z + p["safe_height"]
    op.SafeHeight.Value = start_z + 3.0
    doc.recompute()

    n = len(op.Path.Commands) if op.Path else 0
    log(f"{name}: {n} команд (контурный проход)")
    return op if n > 2 else None


def make_surface_rough(doc, job, tc, name, model_obj, face_idx, p,
                       start_z, final_z, allowance, single_pass=False,
                       stepover=None):
    """Проход по грани: Path Surface (drop cutter — фреза опускается сверху до
    поверхности), зона — только эта грань модели (BoundBox=BaseBoundBox).

    single_pass=False — ЧЕРНОВОЙ: Multi-pass, слоями StepDown, с вертикальным
    смещением DepthOffset = припуск. Плоская фреза оставляет на наклоне
    ступеньки высотой до StepDown.
    single_pass=True — ЧИСТОВОЙ: один проход прямо по поверхности (DepthOffset
    задаётся нулевым вызывающим), ступеньки от черновой снимаются. Так устроена
    заводская программа: у каждого элемента пара «черновая с припуском →
    чистовая» (`RADIYS_1_PR0.5` → `RADIYS_1_CHIST`)."""
    final_z = max(final_z, p.get("_floor_limit", final_z))  # не ниже стола
    import Path.Op.Surface as Surface
    op = Surface.Create(name, parentJob=job)
    op.ToolController = tc
    op.Base = [(model_obj, f"Face{face_idx}")]
    set_prop(op, "BoundBox", "BaseBoundBox")
    set_prop(op, "ScanType", "Planar")
    set_prop(op, "LayerMode", "Single-pass" if single_pass else "Multi-pass")
    set_prop(op, "CutMode", "Climb")
    # Грань бывает УЖЕ фрезы (радиус гиба, скос), поэтому рисунок ZigZag, а не
    # Offset (тот отступает от границы на радиус и оставляет пусто).
    set_prop(op, "CutPattern", "ZigZag")
    # строчки вдоль ДЛИННОЙ стороны грани (меньше проходов и врезаний):
    # CutPatternAngle 0° = строчки вдоль X, 90° = вдоль Y
    try:
        fbb = model_obj.Shape.Faces[face_idx - 1].BoundBox
        cut_angle = 0.0 if fbb.XLength >= fbb.YLength else 90.0
    except Exception:
        cut_angle = 0.0
    set_prop(op, "CutPatternAngle", cut_angle)
    try:   # радиус границы — от фактической фрезы ЭТОЙ операции, не глобальной
        tool_r = float(tc.Tool.Diameter.Value) / 2.0
    except Exception:
        tool_r = float(p["tool_diameter"]) / 2.0
    # Граница обработки. Расширение на радиус (keep_in=False) даёт полное
    # покрытие узкой грани, НО 3D-проход видит только свою грань — выйдя за её
    # край, фреза опускается и боком срезает соседнюю вертикальную стенку
    # (замеряли зарез 0.2 мм на торцах). keep_in=True запирает фрезу внутри
    # грани: зареза нет, зато вдоль края остаётся полоска в радиус фрезы —
    # поэтому вызывающий перебирает фрезы от крупной к мелкой.
    keep_in = bool(p.get("surface_keep_inside", False))
    set_prop(op, "BoundaryAdjustment",
             FreeCAD.Units.Quantity(f"{0.0 if keep_in else tool_r} mm"))
    set_prop(op, "BoundaryEnforcement", keep_in)
    # шаг строчек на наклоне — свой, мельче: гребешки между строчками остаются
    # на самой поверхности детали (чистовой обработки нет)
    set_prop(op, "StepOver", int(stepover if stepover
                                 else p.get("rough_stepover_slope",
                                            p["rough_stepover"])))
    set_prop(op, "SampleInterval",
             FreeCAD.Units.Quantity(f"{max(float(p['rough_tolerance']), 0.2)} mm"))
    set_prop(op, "DepthOffset", FreeCAD.Units.Quantity(f"{allowance} mm"))
    op.setExpression("StepDown", None)
    set_prop(op, "StepDown", p["rough_stepdown"])
    op.setExpression("StartDepth", None)
    op.StartDepth = start_z
    op.setExpression("FinalDepth", None)
    op.FinalDepth = final_z
    op.ClearanceHeight.Value = start_z + p["safe_height"]
    op.SafeHeight.Value = start_z + 3.0
    doc.recompute()

    n = len(op.Path.Commands) if op.Path else 0
    log(f"{name}: {n} команд "
        f"({'чистовой проход' if single_pass else 'террасы по поверхности'})")
    return op if n > 2 else None


def median_line(face, tool_d):
    """Средняя линия узкой зоны — отрезок вдоль её длинной стороны, или None.

    Нужна, чтобы снимать тонкую стоячую полку ОДНИМ проходом на уровень, как
    заводская `NK_1_01`: фреза шире полки, поэтому вести её надо по середине, а
    не обводить зону по контуру (обвод даёт два прохода вдоль плюс торцы).

    Считается только для зоны, которая и правда полоса: площадь должна занимать
    почти весь габаритный прямоугольник. У Г-образного сечения (гнутый лист с
    двумя полками) отношение мало, и вызывающий останется на обводе — там
    средняя линия отрезком не описывается.
    """
    bbf = face.BoundBox
    lx, ly = bbf.XLength, bbf.YLength
    if min(lx, ly) > 2.0 * tool_d:
        return None                       # не полоса, зону надо выбирать
    box = lx * ly
    if box < 1e-9 or face.Area / box < 0.75:
        return None                       # не прямоугольник: L, дуга, звезда
    c = face.CenterOfMass
    z = bbf.ZMin
    if lx >= ly:
        a = FreeCAD.Vector(bbf.XMin, c.y, z)
        b = FreeCAD.Vector(bbf.XMax, c.y, z)
    else:
        a = FreeCAD.Vector(c.x, bbf.YMin, z)
        b = FreeCAD.Vector(c.x, bbf.YMax, z)
    if a.distanceToPoint(b) < tool_d / 2.0:
        return None
    return Part.Wire([Part.makeLine(a, b)])


def make_engrave_sweep(doc, job, tc, name, wire, p, start_z, final_z,
                       note="проход по средней линии"):
    """Проход ПО ЛИНИИ, слоями StepDown: ось фрезы идёт по самой линии.

    Path Engrave — единственная операция FreeCAD, которая не смещает фрезу от
    заданной геометрии. Для тонкой полки это и нужно: фреза шире полки и
    накрывает её, идя серединой.
    """
    final_z = max(final_z, p.get("_floor_limit", final_z))
    feat = doc.addObject("Part::Feature", f"Line{name}")
    feat.Shape = wire
    doc.recompute()

    import Path.Op.Engrave as Engrave
    op = Engrave.Create(name, parentJob=job)
    op.ToolController = tc
    op.BaseShapes = [feat]
    op.setExpression("StepDown", None)
    set_prop(op, "StepDown", p["rough_stepdown"])
    op.setExpression("StartDepth", None)
    op.StartDepth = start_z
    op.setExpression("FinalDepth", None)
    op.FinalDepth = final_z
    # Высоты отвода привязаны ВЫРАЖЕНИЕМ к SetupSheet (верх ЗАГОТОВКИ), и без
    # сброса выражения присваивание молча не действует. Для стадии съёма объёма
    # это незаметно — она и стартует с верха заготовки; а очистка полосы под
    # обвод работает внизу, и отвод уносил фрезу на 47 мм вверх и обратно на
    # КАЖДОМ уровне: 1 064 мм холостого хода на операцию. Обе стадии стартуют с
    # верха ОСТАВШЕГОСЯ материала, выше него ничего нет — отводиться туда и надо.
    for prop, val in (("ClearanceHeight", start_z + p["safe_height"]),
                      ("SafeHeight", start_z + 3.0)):
        try:
            op.setExpression(prop, None)
        except Exception:
            pass
        set_prop(op, prop, val)
    doc.recompute()

    n = len(op.Path.Commands) if op.Path else 0
    log(f"{name}: {n} команд ({note})")
    if n > 2:
        return op
    try:
        doc.removeObject(op.Name)
        doc.recompute()
    except Exception:
        pass
    return None


# Запас, на который отрезок очистки выводится за металл, чтобы спуск попал в
# воздух и врезание не потребовалось. Должен превышать шаг сетки StockMap
# (0.5 мм) — иначе модель снятого материала «увидит» металл под фрезой.
CLEAR_AIR_MARGIN = 1.0


def contour_band(filled, dia, alw_xy):
    """След обводной фрезы вокруг детали: (пути центра фрезы, полоса под ней).

    Обвод (`make_profile`, Side=Outside) ведёт центр фрезы по силуэту детали,
    отжатому на радиус + припуск; чистовой обвод — по тому же силуэту без
    припуска. Вместе они заметают кольцо от нуля до 2R + припуск — это и есть
    полоса, в которой к моменту обвода не должно стоять ничего.

    Полоса ШИРЕ фрезы ровно на припуск, поэтому одним проходом её не закрыть:
    пройди по чистовой линии — останется припуск снаружи, по черновой —
    останется он же изнутри, вплотную к детали. Отсюда ДВА пути, как у завода:
    `NK_1_02` и `NK_1_02_COPY` идут в 0.5 мм друг от друга и вместе прорезают
    полосу шириной ровно 2R + припуск.
    """
    r = dia / 2.0
    offs = [r] + ([r + alw_xy] if alw_xy > 1e-6 else [])
    band = filled.makeOffset2D(2.0 * r + alw_xy).cut(filled)
    return offs, band


def make_clearance_ops(doc, job, shape, p, filled, peri_top, alw_xy, choose):
    """Очистка полосы, по которой ПОТОМ пойдёт обвод контура.

    Обвод режет на уровне полки, а тело фрезы поднимается на всю длину режущей
    части. Стоит в её следе заготовка — фреза снимет её БОКОМ, на всю высоту, за
    один проход и на подаче, выбранной под съём слоя. Сверка этого не покажет:
    она меряет тело детали, а срезается остаток заготовки.

    Завод снимает эту полосу ЗАРАНЕЕ, отдельными операциями (`NK_1_02/03` у
    003 — прямые ходы поперёк стенки по уровням), и обводит контур уже по
    пустому. Здесь то же самое, но зона не задаётся руками:

      * след обводной фрезы считается из её же геометрии (`contour_band`);
      * пересекается с сечением ЗАГОТОВКИ выше старта обвода — что там стоит,
        то и снимаем; где не стоит ничего, операция не создаётся вовсе;
      * снимается уровнями по ROUGH_STEPDOWN.

    Путь — САМ путь обводной фрезы, обрезанный участками, где материал стоит.
    Вести узкую площадку по её средней линии (как делает RoughBulk) здесь
    НЕЛЬЗЯ: у торца стенки 003 средняя линия проходит в 1.2 мм от детали, и
    фреза Ø12 вошла бы в неё на пять миллиметров. Путь обвода по построению
    держит от детали радиус + припуск.

    Ставится сразу после съёма объёма: там верх материала известен точно (пол
    той стадии), и ни один уровень не режет воздух.
    """
    z_top = p.get("_bulk_floor")
    if z_top is None:
        z_top = job.Stock.Shape.BoundBox.ZMax
    height = z_top - peri_top

    # Шаг слоя. Завод разводит его по стадиям: на съёме объёма над деталью
    # 1.0 мм (`NK_1_01`, 24 уровня), на боковых полосках 1.5 (`NK_1_02/03`,
    # 9–10 уровней) — при одном и том же инструменте и одних режимах (F2000,
    # 12000 об/мин). Разница в характере контакта: там фреза 80 мм подряд
    # снимает верх стенки ТОРЦОМ, здесь пересекает 2.5 мм стенки БОКОМ и сразу
    # выходит в воздух. Чем короче контакт, тем больше осевой глубины можно себе
    # позволить при той же нагрузке.
    # Отсюда правило по высоте полосы: высокая (глубокий суммарный съём) —
    # осторожный шаг чернового слоя, невысокая — крупный.
    step = float(p.get("clear_stepdown") or 0.0)
    if step > 0:
        how = "задан"
    elif height > float(p.get("clear_tall_band", 10.0)):
        step, how = float(p["rough_stepdown"]), "полоса высокая"
    else:
        step, how = float(p.get("clear_stepdown_shallow", 1.5)), "полоса невысокая"
    if height < step * 0.5:
        return []

    tcx, dia = choose("RoughClear1", None)
    try:
        offs, band = contour_band(filled, dia, alw_xy)
    except Exception as e:
        log(f"warn: полоса обвода не построена ({e}) — очистка пропущена")
        return []

    # Сечения заготовки по высоте диапазона — объединением, как в make_bulk_ops:
    # заготовка не обязана быть призмой, а объединение ошибается в сторону
    # лишнего холостого хода, а не пропущенного материала.
    n = max(2, min(8, int((z_top - peri_top) / step) + 1))
    sec = None
    for i in range(n):
        z = peri_top + 0.05 + (z_top - 0.1 - peri_top) * i / (n - 1)
        for fc in _slice_faces(job.Stock.Shape, z):
            sec = fc if sec is None else sec.fuse(fc)
    if sec is None:
        return []
    try:
        stand = sec.common(band).removeSplitter()
    except Exception as e:
        log(f"warn: остаток в полосе обвода не посчитан ({e})")
        return []
    faces = [f for f in stand.Faces if f.Area > 1.0]
    if not faces:
        log("полоса под обвод контура чиста — очистка не нужна")
        return []

    # Куда должна встать фреза, чтобы это снять, ПЛЮС запас на выход в воздух.
    # Без запаса отрезок обрывается ровно там, где фреза касается материала, —
    # то есть каждый уровень начинается врезанием, и пасс подвода ставит рампу.
    # На коротком отрезке рампа не помещается в один ход и складывается
    # зигзагом: четыре хода по одному месту вместо одного.
    # Завод входит иначе (`NK_1_02`): отвесный БЫСТРЫЙ спуск в семи миллиметрах
    # перед стенкой, в чистом воздухе, и один прямой рез насквозь. Запас даёт
    # ровно это — концы отрезка отодвигаются от металла, спуск становится
    # холостым, рампа не нужна. Запас обязан превышать шаг сетки модели снятого
    # материала (0.5 мм), иначе она «увидит» металл под фрезой и всё равно
    # потребует врезания.
    r = dia / 2.0
    reach = None
    for f in faces:
        try:
            g = f.makeOffset2D(r + CLEAR_AIR_MARGIN)
        except Exception:
            g = f
        reach = g if reach is None else reach.fuse(g)

    ops, total, n = [], 0.0, 0
    for off in offs:
        try:
            path = filled.makeOffset2D(off).OuterWire
            # Фильтровать по длине РЕБРА нельзя: у замкнутого контура на шве
            # остаётся крошечное ребро, и без него цепочка рвётся пополам —
            # вместо одного прохода из воздуха в воздух получаются два, каждый
            # со своим врезанием посреди металла. Отсеиваем по длине ЦЕПОЧКИ.
            edges = [e for e in path.common(reach).Edges if e.Length > 1e-6]
        except Exception as e:
            log(f"warn: участки пути не выделены ({e}) — очистка неполная")
            break
        for chain in Part.sortEdges(edges):
            if sum(e.Length for e in chain) < 1.0:
                continue
            # С какого конца цепочки пойдёт проход, ВЫБИРАЕТ САМ Engrave —
            # проверено: разворот списка рёбер и разворот самих рёбер выхлопа
            # не меняют. Поэтому вход в воздух обеспечивается только длиной
            # цепочки (CLEAR_AIR_MARGIN выводит концы за металл); где Engrave
            # всё же начал с конца, идущего по касательной к стенке САМОЙ
            # ДЕТАЛИ, пасс подвода поставит там врезание рампой — на длинной
            # цепочке это один наклонный ход, а не зигзаг.
            try:
                wire = Part.Wire(chain)
            except Exception:
                continue
            n += 1
            name = f"RoughClear{n}"
            op = make_engrave_sweep(doc, job, tcx, name, wire,
                                    dict(p, rough_stepdown=step),
                                    z_top, peri_top,
                                    note="очистка полосы под обвод контура")
            if op:
                ops.append(op)
                total += wire.Length
            else:
                log(f"{name}: пустая траектория — участок не очищен")
    if ops:
        log(f"очистка полосы под обвод: {len(faces)} участков "
            f"({sum(f.Area for f in faces):.0f} мм² в плане), {len(offs)} линии "
            f"обвода, путь {total:.0f} мм на уровень, "
            f"Z {z_top:.1f}..{peri_top:.1f} ({height:.1f} мм), "
            f"слой {step:g} мм ({how})")
    return ops


def make_bulk_ops(doc, job, tc, shape, p, alw_xy, alw_z, choose):
    """Съём объёма НАД деталью уровнями — первая стадия заводской программы.

    Разбор `PR_1_01…06` (деталь 003): половину всего рабочего хода завода —
    2 400 мм из 4 891 — занимает операция `NK_1_01`, и устроена она предельно
    просто: 75 прямых ходов вдоль Y на 25 уровнях Z. Так сносится лишняя часть
    стоячей полки уголка. Всё остальное — радиус гиба (568 мм), выборка выреза
    (402) и контур (894) — работает уже по тому, что осталось.

    У нас этой стадии не было, и объём над деталью снимали операции ПО ГРАНЯМ:
    `RoughSlope1` тащила на себе весь столб материала над гранью площадью
    19 мм² и стоила 5 697 мм, `RoughFace1` — 7 896, `RoughSlope2` — 5 465. Три
    операции по граням давали 19 058 мм из 22 427.

    Здесь снимается ровно то, что лежит ВЫШЕ детали: зона = сечение заготовки в
    диапазоне, тень детали вычитать не нужно — весь диапазон над ней. Adaptive
    сам делит его на слои по ROUGH_STEPDOWN. Возвращает (операции, низ стадии);
    низ отдаётся вызывающему, чтобы операции по граням стартовали от него, а не
    от верха габарита заготовки.
    """
    bb = shape.BoundBox
    sb = job.Stock.Shape.BoundBox
    z_top = sb.ZMax
    z_floor = bb.ZMax + alw_z
    step = float(p["rough_stepdown"])
    if z_top - z_floor < step:
        return [], None                  # над деталью материала нет

    # Сечения заготовки по высоте диапазона — объединением: заготовка не обязана
    # быть призмой (уголок, отливка), а объединение ошибается в сторону лишнего
    # холостого хода, а не пропущенного материала.
    n = max(2, min(8, int((z_top - z_floor) / step) + 1))
    sec = None
    for i in range(n):
        z = z_floor + 0.02 + (z_top - 0.02 - z_floor - 0.02) * i / (n - 1)
        for fc in _slice_faces(job.Stock.Shape, z):
            sec = fc if sec is None else sec.fuse(fc)
    if sec is None:
        return [], None
    try:
        sec = sec.removeSplitter()
    except Exception:
        pass

    dia = float(p["tool_diameter"])
    ops = []
    faces = _nearest_order([f for f in sec.Faces if f.Area > 1.0],
                           lambda f: (f.CenterOfMass.x, f.CenterOfMass.y))
    for i, rf in enumerate(faces, 1):
        name = f"RoughBulk{i}"
        tcx, _dx = choose(name, None)
        # ШИРОКАЯ зона или УЗКАЯ полоса — это разные операции, и завод их тоже
        # разводит. Признак — средняя ширина 2·площадь/периметр: у стоячей полки
        # 003 это 1.95 мм, у нормального бруска заготовки — десятки.
        try:
            w_eff = 2.0 * rf.Area / max(rf.OuterWire.Length, 1e-6)
        except Exception:
            w_eff = dia * 3
        narrow = w_eff < 2.0 * dia

        op, how = None, ""
        # Полоса-прямоугольник — один проход по средней линии на уровень, как
        # `NK_1_01` у завода. Обвод по контуру той же полосы стоит вдвое: он
        # идёт вдоль неё дважды плюс торцы.
        if narrow:
            line = median_line(rf, dia)
            if line is not None:
                op = make_engrave_sweep(doc, job, tcx, name, line, p,
                                        z_top, z_floor)
                how = "полоса, средняя линия"

        if op is None:
            # Зона расширяется, иначе фреза в неё не встанет: сечение полки уже
            # фрезы. Узкой хватает радиуса, широкой нужен диаметр — Adaptive
            # требует около двух диаметров на винтовой заход, и заодно у края
            # заготовки не остаётся кожуры припуска.
            grow = (dia / 2.0 if narrow else dia) + alw_xy
            try:
                zone = rf.makeOffset2D(grow)
            except Exception as e:
                log(f"warn: {name}: зона не расширена ({e})")
                zone = rf
            if not narrow:
                op = make_adaptive(doc, job, tcx, name, zone, p,
                                   z_top, z_floor, alw_xy)
                how = "выборка"
            if not op:
                if not narrow:
                    log(f"{name}: Adaptive пуст — перехожу на контурный проход")
                op = make_profile(doc, job, tcx, name, zone, p, z_top, z_floor,
                                  alw_xy, side="Inside")
                how = "контурный обход"
        if op:
            ops.append(op)
            log(f"{name}: ширина зоны {w_eff:.1f} мм ({how})")
        else:
            log(f"{name}: пустая траектория — объём над деталью не снят")
    if ops:
        log(f"съём объёма над деталью: Z {z_top:.1f}..{z_floor:.1f}, "
            f"{len(ops)} зон, слой {step:g} мм")
    return ops, (z_floor if ops else None)


def make_roughing_ops(doc, job, tc, shape, p):
    """Черновая «по граням» (ROUGH_MODE=stages), порядок техпроцесса:
      1. RoughHole<N>   — сквозные вырезы ЛЮБОЙ формы, по очереди (Adaptive) —
         ПЕРВЫМИ, пока деталь жёстко держится в заготовке;
      2. грани сверху вниз (только достижимые сверху: над гранью есть заготовка,
         грань смотрит вверх — есть проекция на XY, над ней нет тела детали):
         плоские — Adaptive (RoughFace<N>), наклонные/криволинейные — террасы
         по поверхности (RoughSlope<N>), ВПЕРЕМЕШКУ по высоте;
      3. RoughPerimeter — внешний контур детали по силуэту, ПОСЛЕДНИМ: один обвод
         Profile снаружи (материал в углах заготовки, не касающийся детали,
         остаётся).
    Зоны не пересекаются. Недоступное сверху (грани вниз/вбок, нависания, накрытые
    материалом) не режется — второй установ. Припуск: по стенкам StockToLeave /
    OffsetExtra, по дну — глубиной FinalDepth."""
    # припуск разведён на стенки (XY) и полы/поверхности (Z). Режим:
    # none = начисто без припуска (дефолт), xy = только стенки, all = стенки+полы.
    # Величина — ROUGH_ALLOWANCE. Сквозные вырезы и внешний контур режутся до дна
    # ВСЕГДА (ниже), припуск по дну на них не влияет.
    mag = round(p.get("rough_allowance", 0.5), 1)     # шаг 0.1 мм
    mode = p.get("rough_allowance_mode", "none")
    alw_xy = mag if mode in ("xy", "all") else 0.0    # StockToLeave / OffsetExtra
    alw_z = mag if mode == "all" else 0.0             # полы карманов, поверхности
    # Чистовой проход: у каждого элемента пара «черновая с припуском → чистовая»,
    # как в заводской программе (`RADIYS_1_PR0.5` → `RADIYS_1_CHIST`). Без
    # припуска чистовая была бы повтором черновой — предупреждаем и не молчим.
    fin = bool(p.get("finish", False))
    fin_stepover = int(p.get("finish_stepover", 25))
    # Шаг строчек ЧЕРНОВОГО прохода по граням. Мелкий шаг (ROUGH_STEPOVER_SLOPE)
    # нужен потому, что гребешки между строчками остаются на самой детали —
    # чистовой обработки нет. Когда чистовой ЕСТЬ и есть припуск, гребешки
    # ложатся в припуск и снимаются им; тогда черновой идёт крупным шагом, как
    # по плоскостям. Замер на 003: шаг 40 → 85 % даёт 3 225 → 1 667 мм на трёх
    # операциях по граням, сверка не меняется (проверено и с порогом толщины
    # 0.05 мм: остаток тот же и лежит в тех же местах — это плёнка фасетизации
    # тела ISV, а не гребешки).
    rough_slope_so = (int(p["rough_stepover"]) if fin and alw_z > 0 else None)
    if fin and mode == "none":
        log("warn: чистовой проход включён при ROUGH_ALLOWANCE_MODE=none — "
            "черновая режет начисто, чистовому нечего снимать")
    bb = shape.BoundBox
    sb = job.Stock.Shape.BoundBox
    start_z = sb.ZMax                      # верх заготовки

    sil = build_silhouette(shape, bb, p["rough_stepdown"])
    ops = []
    stock_shape = job.Stock.Shape

    # ── мёртвые зоны: XY-боксы (координаты программы), где фреза работать НЕ должна
    #    (прижимы, указания auto_fix/ЛЛМ). Вычитаются из 2D-зон операций;
    #    наклонная грань, чей центр в зоне, пропускается целиком. ──
    dz_faces = []
    for z in (p.get("dead_zones") or []):
        try:
            x0, x1 = float(z["x"][0]), float(z["x"][1])
            y0, y1 = float(z["y"][0]), float(z["y"][1])
            dz_faces.append(Part.Face(Part.makePolygon([
                FreeCAD.Vector(x0, y0, 0), FreeCAD.Vector(x1, y0, 0),
                FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x0, y1, 0),
                FreeCAD.Vector(x0, y0, 0)])))
        except Exception as e:
            log(f"warn: мёртвая зона {z} не разобрана: {e}")
    dead = Part.makeCompound(dz_faces) if dz_faces else None
    if dead is not None:
        log(f"мёртвые зоны: {len(dz_faces)} шт. — исключены из обработки")

    def cut_dead(region):
        """Вычитает мёртвые зоны из плоской зоны; None = зона исчезла целиком."""
        if dead is None or region is None:
            return region
        try:
            r = region.cut(dead)
            return r if r.Area > 0.5 else None
        except Exception as e:
            log(f"warn: вычитание мёртвой зоны не удалось ({e}) — зона без выреза")
            return region

    def in_dead(cx, cy):
        for z in (p.get("dead_zones") or []):
            try:
                if z["x"][0] <= cx <= z["x"][1] and z["y"][0] <= cy <= z["y"][1]:
                    return True
            except Exception:
                pass
        return False

    skip_ops = set(p.get("skip_ops") or [])

    def skip(name):
        """Операция в чёрном списке (указание оператора/ЛЛМ) — не создавать."""
        if name in skip_ops:
            log(f"{name}: в списке skip_ops — пропущено по указанию")
            return True
        return False

    # пул фрез: {диаметр: TC}, диаметры по убыванию. Неглавные TC пока ВНЕ
    # job.Tools (иначе FreeCAD роняет создание операций) — добавятся в конце.
    pool = p.get("_tool_pool") or {round(float(p["tool_diameter"]), 3): tc}
    diams = p.get("_tool_diams") or sorted(pool, reverse=True)
    overrides = p.get("set_op_tools") or {}

    def choose_tc(name, width):
        """(TC, диаметр) для операции: пофрезное переопределение (SET_OP_TOOLS) →
        иначе крупнейшая фреза набора, влезающая в фичу шириной width
        (width=None — выборка/контур, берём крупнейшую)."""
        if name in overrides:
            d = min(diams, key=lambda t: abs(t - float(overrides[name])))
        elif width is None:
            d = diams[0]
        else:
            fit = [t for t in diams if t <= width + 1e-6]  # diams убыв. → [0] крупнейшая
            d = fit[0] if fit else diams[-1]
        return pool.get(d, tc), d

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
            top = min(m.BoundBox.ZMax, sb.ZMax)
            # Если объём над деталью уже снят отдельной стадией (RoughBulk),
            # стартовать от верха заготовки — значит резать воздух все её слои
            # заново. Это и была главная статья расхода: операции по граням
            # тащили на себе весь столб материала над собой.
            floor = p.get("_bulk_floor")
            return top if floor is None else min(top, floor)
        except Exception as e:
            log(f"warn: локальный верх зоны не посчитался ({e}) — беру верх заготовки")
            return sb.ZMax

    def formed_allowance(f, tol):
        """Максимальный припуск над ПОВЕРХНОСТЬЮ грани, но не больше tol.

        Возвращает None, если припуска нигде нет (грань уже готова в
        заготовке), иначе величину, на которой проверка остановилась.

        Зачем отдельно от local_start. Тот меряет «верх материала над зоной»
        против НИЗА габарита грани. У плоской грани это точно: обе величины —
        горизонтали. У наклонной и кривой её собственная высота габарита
        каждый раз засчитывается как припуск, даже когда поверхность заготовки
        совпадает с поверхностью детали. Гнутый лист, отливка, поковка несут
        часть поверхностей готовыми, и на них это даёт целую пару операций
        впустую: на 003 радиус гиба согнут в заготовке (0.00 мм припуска во
        всех 64 точках грани), а габаритная проверка видела 2.5 мм и заводила
        RoughSlope+FinishSlope на 1 370 мм рабочего хода.

        Меряется точечно по самой грани. Точка идёт в припуск, только если она
        ВНЕ детали (иначе это тело самой детали, а не то, что надо снять) и
        ВНУТРИ заготовки. Материал выше `_bulk_floor` не в счёт — его сняла
        стадия съёма объёма; та же оговорка, что в local_start.
        """
        floor = p.get("_bulk_floor")
        try:
            verts, tris = f.tessellate(max(tol / 2.0, 0.02))
            pts = [(verts[a] + verts[b] + verts[c]) * (1.0 / 3.0)
                   for a, b, c in tris]
        except Exception as e:
            log(f"warn: грань не тесселируется ({e}) — считаю, что припуск есть")
            return tol
        if not pts:
            return tol
        if len(pts) > 800:                      # плотность важнее полноты
            pts = pts[::len(pts) // 800 + 1]
        for pt in pts:
            q = FreeCAD.Vector(pt.x, pt.y, pt.z + tol)
            if floor is not None and q.z >= floor:
                continue                        # выше снято стадией съёма объёма
            if shape.isInside(q, 1e-6, True):
                continue                        # тело детали, а не припуск
            if stock_shape.isInside(q, 1e-6, True):
                return tol                      # припуск есть — грань нужна
        return None

    def concave_radius(f):
        """Радиус ВОГНУТОГО скругления грани (гиб, галтель у стенки) или None.
        Вогнутое = тело снаружи цилиндра: пробуем точку на 0.1 мм в сторону оси —
        если там пусто, материал с другой стороны, значит угол вогнутый и фреза
        радиусом больше R в него не войдёт."""
        if surf_name(f) != "Cylinder":
            return None
        try:
            u0, u1, v0, v1 = f.ParameterRange
            mid = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
            ax, base = f.Surface.Axis, f.Surface.Center
            axis_pt = base + ax * (mid - base).dot(ax)   # проекция точки на ось
            d = axis_pt - mid
            if d.Length < 1e-9:
                return None
            d.normalize()
            if shape.isInside(mid + d * 0.1, 1e-6, True):
                return None            # материал со стороны оси = выпуклый угол
            return float(f.Surface.Radius)
        except Exception:
            return None

    def surface_ladder(name, face_idx, top, final_z, width, alw,
                       single_pass=False, stepover=None):
        """3D-проход по грани с перебором фрез от крупной к мелкой. Нужен из-за
        запрета выхода за границу грани: фреза шире грани даёт пустой путь,
        мелкая — нормальный. Возврат: (операция или None, диаметр)."""
        first, dx0 = choose_tc(name, width)
        tries = [(first, dx0)] + [(pool[d], d) for d in diams
                                  if d < dx0 and name not in overrides]
        for tcx, dx in tries:
            op = make_surface_rough(doc, job, tcx, name, jm, face_idx, p,
                                    top, final_z, alw, single_pass, stepover)
            if op:
                return op, dx
            if dx != tries[-1][1]:
                log(f"{name}: фреза Ø{dx:g} не дала траектории — пробую мельче")
        return None, dx0

    def finish_surface(rough_name, face_idx, top, final_z, width):
        """Чистовой проход по той же грани — пара к черновой, как у завода.

        Черновая оставляет припуск (по стенкам StockToLeave, по поверхности
        DepthOffset) и ступеньки высотой до StepDown; чистовая идёт одним
        проходом прямо по поверхности с более мелким шагом строчек."""
        if not fin:
            return
        name = rough_name.replace("Rough", "Finish", 1)
        if skip(name):
            return
        op2, _ = surface_ladder(name, face_idx, top, final_z, width, 0.0,
                                single_pass=True, stepover=fin_stepover)
        if op2:
            ops.append(op2)
        else:
            log(f"{name}: пустая траектория — чистового прохода не будет")

    # Внешний контур считается ЗАРАНЕЕ: по нему же строится полоса, которую надо
    # очистить ДО обвода (иначе фреза снимет стоящее в ней боком — см.
    # make_clearance_ops). Сам обвод создаётся в конце, как и раньше.
    filled, peri_top = None, None
    if sil is not None:
        try:
            filled = Part.makeFace([f.OuterWire for f in sil.Faces],
                                   "Part::FaceMakerBullseye")
        except Exception as e:
            log(f"warn: внешний контур не построился: {e}")
    if filled is not None:
        # Верх обвода — по ВЕРХУ НИЖНЕЙ ПОЛКИ детали (самой большой
        # горизонтальной грани, смотрящей вверх). Выше полки силуэт сжимается до
        # стенки, и обвод по силуэту шёл бы там по воздуху.
        peri_top = min(sb.ZMax, bb.ZMax)
        shelf_a, shelf_z = 0.0, None
        for f in shape.Faces:
            if surf_name(f) != "Plane" or f.normalAt(0, 0).z < 0.999:
                continue
            if f.Area > shelf_a:
                shelf_a, shelf_z = f.Area, f.BoundBox.ZMax
        if shelf_z is not None:
            peri_top = min(peri_top, max(shelf_z, bb.ZMin + 0.1))
            log(f"RoughPerimeter: верх обвода по полке Z={peri_top:.2f} "
                f"(верх детали {bb.ZMax:.2f})")

    # ── 0) съём объёма НАД деталью уровнями, ПЕРВЫМ — как `NK_1_01` у завода.
    #      Без этой стадии её работу делают операции по граням, каждая заново
    #      проходя весь столб материала над своей гранью. ──
    p.pop("_bulk_floor", None)
    if p.get("bulk_rough", True):
        bulk, bulk_floor = make_bulk_ops(doc, job, tc, shape, p,
                                         alw_xy, alw_z, choose_tc)
        if bulk:
            ops.extend(bulk)
            p["_bulk_floor"] = bulk_floor
            write_partial(job, ops, p, "снят объём над деталью")

    # ── 0a) очистка полосы, по которой пойдёт обвод контура — ЗАРАНЕЕ, чтобы к
    #       обводу там уже ничего не стояло и бокового реза не возникало. У
    #       завода на это отдельные операции (`NK_1_02/03`), и контур они
    #       обводят по пустому. ──
    if (p.get("clear_contour_band", True) and filled is not None
            and peri_top is not None and not skip("RoughClear1")):
        clr = make_clearance_ops(doc, job, shape, p, filled, peri_top,
                                 alw_xy, choose_tc)
        if clr:
            ops.extend(clr)
            write_partial(job, ops, p, "очищена полоса под обвод контура")

    # ── 1) сквозные вырезы любой формы, ПЕРВЫМИ (деталь ещё жёстко в заготовке) ──
    if sil is None:
        log("warn: силуэт не построился — вырезы и внешний контур пропущены")
    for i, region in enumerate(find_through_cuts(sil) if sil is not None else [], 1):
        if skip(f"RoughHole{i}"):
            continue
        region = cut_dead(region)
        if region is None:
            log(f"RoughHole{i}: целиком в мёртвой зоне — пропущено")
            continue
        rb = region.BoundBox
        hole_top = local_start(region)
        if hole_top is None:
            log(f"RoughHole{i}: над вырезом нет материала заготовки — пропущено")
            continue
        # сквозной вырез режем до дна ДЕТАЛИ (bb.ZMin), а НЕ до floor_z
        # (дно + припуск): у сквозного отверстия нет дна, чтобы оставлять там
        # припуск — иначе на дне стоит кожура.
        # Припуск по стенкам (StockToLeave) при этом сохраняется.
        # Перебор фрез по убыванию: «самая крупная, что влезает по ширине» —
        # только первая попытка. Фреза ровно в размер выреза (Ø1 в вырез 1×1)
        # даёт пустую траекторию: винтовому заходу нужно ~2 Ø, контурному —
        # запас на радиус. Раньше на этом сдавались и вырез оставался целым;
        # теперь берём следующую по убыванию, пока путь не появится.
        width = min(rb.XLength, rb.YLength)
        name = f"RoughHole{i}"
        first, dx0 = choose_tc(name, width)
        tries = [(first, dx0)] + [(pool[d], d) for d in diams
                                  if d < dx0 and name not in overrides]
        op = None
        for tcx, dx in tries:
            op = make_adaptive(doc, job, tcx, name, region, p,
                               hole_top, bb.ZMin, alw_xy)
            if not op and width > dx + 0.2:
                # узкий паз: винтового захода нет, но фреза в паз проходит —
                # контурный обход ИЗНУТРИ (вход вертикальным врезанием)
                log(f"{name}: узкий вырез — перехожу на контурный проход изнутри")
                op = make_profile(doc, job, tcx, name, region, p,
                                  hole_top, bb.ZMin, alw_xy, side="Inside")
            if op:
                break
            if dx != tries[-1][1]:
                log(f"{name}: фреза Ø{dx:g} не дала траектории — пробую мельче")
        if op:
            ops.append(op)
            # чистовая стенок выреза — контурный обход изнутри без припуска
            if fin and not skip(f"FinishHole{i}"):
                fop = make_profile(doc, job, tcx, f"FinishHole{i}", region, p,
                                   hole_top, bb.ZMin, 0.0, side="Inside")
                if fop:
                    ops.append(fop)
                else:
                    log(f"FinishHole{i}: пустая траектория — чистового нет")
            write_partial(job, ops, p, f"готов вырез {i} (Ø{dx:g}, "
                                       f"~{rb.XLength:.0f}x{rb.YLength:.0f} мм)")
        else:
            log(f"RoughHole{i}: фреза Ø{dx:g} с припуском не влезает "
                f"в вырез ~{rb.XLength:.0f}x{rb.YLength:.0f} мм — пропущено")

    # ── 2) грани сверху вниз: плоские (Adaptive) и наклонные/криволинейные
    #      (террасы по поверхности) ВПЕРЕМЕШКУ по высоте — сначала самые высокие.
    #      Берём только достижимые сверху: над гранью есть материал заготовки,
    #      грань смотрит вверх (есть проекция на XY), над ней НЕТ тела детали.
    #      Смотрящие вниз/вбок, накрытые, нависания — второй установ. ──
    def is_handled(f):
        s = surf_name(f)
        nz = f.normalAt(0, 0).z if s == "Plane" else None
        if s == "Plane" and (abs(nz) > 0.999 or abs(nz) < 0.001):
            return True    # горизонтальные (идут ниже) и вертикальные плоскости (стенки)
        if s == "Cylinder" and abs(f.Surface.Axis.z) > 0.999:
            return True    # вертикальные цилиндрические стенки/скругления
        return False

    jm = job.Model.Group[0]  # клон модели внутри Job — его грани идут в Base операций
    include_top = sb.ZMax > bb.ZMax + 0.01
    faces, skipped = [], 0.0
    # плоские грани, смотрящие вверх (полки, донья карманов, уступы)
    up_faces = find_up_faces(shape, bb, include_top)
    log(f"кандидаты: {len(up_faces)} плоских граней вверх (include_top={include_top})")
    for fc in up_faces:
        probe = FreeCAD.Vector(fc["cx"], fc["cy"], fc["z"] + 0.3)
        if shape.isInside(probe, 1e-6, True):
            log(f"грань Z={fc['z']:.1f} ({fc['area']:.0f} мм²): накрыта материалом — "
                f"пропущена")
            skipped += fc["area"]          # накрыта материалом сверху — не достать
            continue
        region = cut_dead(fc["region"])
        if region is None:
            log(f"грань Z={fc['z']:.1f} ({fc['area']:.0f} мм²): целиком в мёртвой "
                f"зоне — пропущена")
            continue
        # final — дно ЧЕРНОВОЙ (грань + припуск), final0 — сама грань: чистовой
        # обязан идти до неё, иначе он оставит ровно тот припуск, ради снятия
        # которого и заведён.
        faces.append({"kind": "planar", "z": fc["z"], "final": fc["z"] + alw_z,
                      "final0": fc["z"],
                      "region": region, "idx": fc["idx"], "area": fc["area"],
                      "cx": fc["cx"], "cy": fc["cy"]})
    # наклонные/криволинейные грани с восходящей нормалью
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
        if nz < 0.01:      # смотрит вниз или строго вбок — проекции на XY нет
            skipped += f.Area
            continue
        if in_dead(mid.x, mid.y):
            log(f"наклонная грань ({f.Area:.0f} мм²): в мёртвой зоне — пропущена")
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
        faces.append({"kind": "slope", "z": fbb.ZMax, "final": fbb.ZMin + alw_z,
                      "final0": fbb.ZMin,
                      "rect": rect, "idx": idx, "area": f.Area, "face": f,
                      "cx": mid.x, "cy": mid.y})

    # сортировка сверху вниз; на одном уровне — от ближней к дальней
    levels = sorted({round(f["z"], 3) for f in faces}, reverse=True)
    ordered = []
    for lv in levels:
        ordered += _nearest_order([f for f in faces if round(f["z"], 3) == lv],
                                  lambda f: (f["cx"], f["cy"]))

    face_n = slope_n = 0
    # грани, готовые в заготовке (радиус гиба у гнутого листа, литые
    # поверхности), операциями не трогаем — см. formed_allowance
    skip_formed = bool(p.get("skip_formed_faces", True))
    formed_tol = float(p.get("formed_face_tol", 0.05))
    for fc in ordered:
        if fc["kind"] == "planar":
            top = local_start(fc["region"])
            if top is None or fc["final"] >= top - 1e-6:
                if top is None:
                    log(f"RoughFace (Z={fc['z']:.1f}): материала над гранью нет — "
                        f"пропущено")
                elif fin and top > fc["final0"] + 1e-6:
                    # Черновой снимать нечего (стадия съёма объёма дошла ровно
                    # до припуска), а чистовому есть: припуск на грани остался.
                    face_n += 1
                    rfb = fc["region"].BoundBox
                    finish_surface(f"RoughFace{face_n}", fc["idx"], top,
                                   fc["final0"],
                                   min(rfb.XLength, rfb.YLength))
                continue  # грань вровень с верхом материала — снимать нечего
            face_n += 1
            name = f"RoughFace{face_n}"
            if skip(name):
                continue
            rfb = fc["region"].BoundBox
            fin_width = min(rfb.XLength, rfb.YLength)
            # плоские грани снимаем 3D-проходом по поверхности (террасы), как и
            # наклонные — по требованию оператора вместо Adaptive-выборки
            op, dx = surface_ladder(name, fc["idx"], top, fc["final"],
                                    fin_width, alw_z, stepover=rough_slope_so)
            if not op:
                log(f"{name}: 3D-проход пуст на всех фрезах — Adaptive-выборкой")
                tcx, dx = choose_tc(name, min(rfb.XLength, rfb.YLength))
                op = make_adaptive(doc, job, tcx, name, fc["region"], p,
                                   top, fc["final"], alw_xy)
            note = f"готова грань {face_n} (Ø{dx:g}, Z={fc['z']:.1f}, {fc['area']:.0f} мм²)"
        else:
            top = local_start(fc["rect"])
            if top is None:
                log(f"RoughSlope (Z={fc['z']:.1f}): материала над гранью нет — "
                    f"пропущено")
                continue
            if skip_formed and formed_allowance(fc["face"], formed_tol) is None:
                log(f"RoughSlope (Z={fc['z']:.1f}, {fc['area']:.0f} мм²): "
                    f"заготовка нигде не выше поверхности грани на "
                    f"{formed_tol} мм — грань уже готова в заготовке, "
                    f"операции не нужны")
                continue
            slope_n += 1
            name = f"RoughSlope{slope_n}"
            if skip(name):
                continue
            rb2 = fc["rect"].BoundBox
            width = min(rb2.XLength, rb2.YLength)
            # ВОГНУТЫЙ радиус (гиб, галтель у стенки) ограничивает фрезу сверху:
            # плоская фреза радиусом больше R в такой угол не входит — её ось не
            # подойдёт к стенке ближе своего радиуса, и вся дуга остаётся целой.
            # Ø ≤ 2R. Выпуклые скругления (внешний угол) не ограничивают.
            rcap = concave_radius(jm.Shape.Faces[fc["idx"] - 1])
            if rcap is not None:
                width = min(width, 2.0 * rcap)
                log(f"{name}: вогнутый радиус R{rcap:.1f} — фреза не крупнее "
                    f"Ø{2.0 * rcap:.1f}")
            fin_width = width
            op, dx = surface_ladder(name, fc["idx"], top, fc["final"],
                                    width, alw_z, stepover=rough_slope_so)
            note = f"готова криволинейная грань {slope_n} (Ø{dx:g}, {fc['area']:.0f} мм²)"
        if op:
            ops.append(op)
            finish_surface(name, fc["idx"], top, fc["final0"], fin_width)
            write_partial(job, ops, p, note)
        else:
            log(f"{name}: (Z={fc['z']:.1f}) пустая траектория — пропущено")
    if skipped > 1.0:
        log(f"warn: {skipped:.0f} мм² поверхностей смотрят вниз/вбок или накрыты "
            f"материалом — сверху не достать, это второй установ")

    # ── 2b) дополнительные зоны съёма (указания оператора/ЛЛМ из auto_fix):
    #      принудительная дообработка там, где штатные операции не добрали. ──
    for k, z in enumerate(p.get("extra_zones") or [], 1):
        name = f"ExtraZone{k}"
        if skip(name):
            continue
        try:
            x0, x1 = sorted((float(z["x"][0]), float(z["x"][1])))
            y0, y1 = sorted((float(z["y"][0]), float(z["y"][1])))
            rect = Part.Face(Part.makePolygon([
                FreeCAD.Vector(x0, y0, 0), FreeCAD.Vector(x1, y0, 0),
                FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x0, y1, 0),
                FreeCAD.Vector(x0, y0, 0)]))
        except Exception as e:
            log(f"{name}: зона {z} не разобрана: {e}")
            continue
        rect = cut_dead(rect)
        if rect is None:
            log(f"{name}: целиком в мёртвой зоне — пропущено")
            continue
        ztop = local_start(rect)
        if ztop is None:
            log(f"{name}: над зоной нет материала заготовки — пропущено")
            continue
        if "z_top" in z:
            ztop = min(ztop, float(z["z_top"]))
        zbot = float(z.get("z_bottom", bb.ZMin))   # снизу клампится полом
        tcx, dx = choose_tc(name, min(x1 - x0, y1 - y0))
        op = make_adaptive(doc, job, tcx, name, rect, p, ztop, zbot, alw_xy)
        if not op and min(x1 - x0, y1 - y0) > dx + 0.2:
            log(f"{name}: узкая зона — перехожу на контурный проход изнутри")
            op = make_profile(doc, job, tcx, name, rect, p, ztop, zbot, alw_xy,
                              side="Inside")
        if op:
            ops.append(op)
            write_partial(job, ops, p, f"готова доп. зона {k}")
        else:
            log(f"{name}: пустая траектория — пропущено")

    # ── 3) внешний контур детали по силуэту — ПОСЛЕДНИМ: пока деталь жёстко
    #      держится в заготовке, снят весь объём выше; периметр обходим в конце.
    #      Один обвод Profile снаружи вдоль силуэта детали; лишний материал в
    #      углах заготовки, не касающийся детали, остаётся (так просил техпроцесс).
    #      Прорезаем именно внешний периметр детали, не выбирая всё поле. ──
    if filled is not None and peri_top is not None:
        if not skip("RoughPerimeter"):
            # внешний контур режем до дна ДЕТАЛИ (bb.ZMin, снизу клампится полом):
            # периметр отделяет деталь от рамки заготовки. Припуск по стенке
            # (OffsetExtra) при этом сохраняется. Верх обвода (peri_top) и сам
            # силуэт (filled) посчитаны выше — по ним же строилась полоса,
            # которую очистила стадия RoughClear.
            tcx, _ = choose_tc("RoughPerimeter", None)
            op = make_profile(doc, job, tcx, "RoughPerimeter", filled, p,
                              peri_top, bb.ZMin, alw_xy)
            if op:
                ops.append(op)
                if fin and not skip("FinishPerimeter"):
                    fop = make_profile(doc, job, tcx, "FinishPerimeter", filled,
                                       p, peri_top, bb.ZMin, 0.0)
                    if fop:
                        ops.append(fop)
                    else:
                        log("FinishPerimeter: пустая траектория — чистового нет")
                write_partial(job, ops, p, "готов внешний контур детали")
            else:
                log("внешний контур: пустая траектория — периметр не прорезан")

    if ops:
        log(f"черновая (по граням): припуск XY {alw_xy} / полы {alw_z} мм "
            f"(режим {mode}), слой {p['rough_stepdown']} мм, этапов: {len(ops)} "
            f"({', '.join(o.Label for o in ops)})")
    else:
        log("warn: черновая не дала ни одной операции")
    return ops


def make_layered_ops(doc, job, tc, shape, p):
    """ЭКСПЕРИМЕНТ (--rough-mode layers): послойная черновая «как технолог в NX
    делает Cavity Mill». Ничего не угадывает про типы фич — на каждой высоте
    ответ точный:  материал(Z) = проекция заготовки − тень детали выше Z.
    Высоты режутся диапазонами между характерными уровнями детали (верхи полок,
    дно), наклонные/криволинейные участки дробятся по ROUGH_STEPDOWN. Каждая
    связная область диапазона — отдельная операция Adaptive (Contour — область
    у края заготовки, Pocket — замкнутая внутри); Adaptive знает заготовку и
    пропускает воздух. Припуск: по стенкам StockToLeave, по полкам — границы
    диапазонов сдвинуты на припуск выше граней."""
    mag = round(p.get("rough_allowance", 0.5), 1)
    mode = p.get("rough_allowance_mode", "none")
    alw_xy = mag if mode in ("xy", "all") else 0.0   # StockToLeave (стенки)
    alw_z = mag if mode == "all" else 0.0            # полы (границы диапазонов)
    step = float(p["rough_stepdown"])
    bb = shape.BoundBox
    sb = job.Stock.Shape.BoundBox
    ops = []

    ssil = build_silhouette(job.Stock.Shape, sb, step)
    if ssil is None:
        log("warn: силуэт заготовки не построился — послойная черновая невозможна")
        return ops
    stock_filled = Part.makeFace([f.OuterWire for f in ssil.Faces],
                                 "Part::FaceMakerBullseye")

    # ── 1) характерные уровни детали ──
    bottom = bb.ZMin + alw_z
    levels = {sb.ZMax, bottom}
    slant_spans = []
    for f in shape.Faces:
        s = surf_name(f)
        fz1, fz2 = f.BoundBox.ZMin, f.BoundBox.ZMax
        if s == "Plane":
            nz = f.normalAt(0, 0).z
            if nz > 0.999:
                levels.add(fz2 + alw_z)  # полка: дно диапазона = грань + припуск
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
            for fc in _slice_faces(job.Stock.Shape, z):
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
            grow = float(p["tool_diameter"]) + alw_xy
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
        faces = _nearest_order([f for f in region.Faces if f.Area > 1.0],
                               lambda f: (f.CenterOfMass.x, f.CenterOfMass.y))
        for ri, rf in enumerate(faces, 1):
            try:  # у края заготовки — контур, замкнутая внутри — карман
                kind = ("Contour" if rf.distToShape(stock_filled.OuterWire)[0] < 0.01
                        else "Pocket")
            except Exception:
                kind = "Region"
            op = make_adaptive(doc, job, tc, f"B{bi}{kind}{ri}", rf, p,
                               top_z, bot_z, alw_xy)
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


def tool_passport(num, diam, catalog):
    """Комментарий с паспортом фрезы для строки вызова инструмента.

    Требование ОЭЦМ: по программе должно быть видно, ЧЕМ она написана —
    диаметр, радиус при вершине, длина режущей части, вылет из оправки и длина
    сборки. Неизвестное печатаем как «?»: пробел в паспорте наладчик заметит,
    а молча пропущенный параметр — нет.
    """
    # Каталог приходит из JSON, где ключи словаря ВСЕГДА строки: искать по
    # float(diam) бессмысленно, паспорт молча выйдет пустым («R=? FL=?»).
    e = {}
    for k, val in (catalog or {}).items():
        try:
            if abs(float(k) - float(diam)) < 1e-6:
                e = val or {}
                break
        except (TypeError, ValueError):
            continue

    def v(key, fmt="{:g}"):
        x = e.get(key)
        return fmt.format(x) if x is not None else "?"

    name = e.get("name") or "D{:g}".format(diam)
    return ("(Tool T{}: {} D={:.2f} R={} FL={} H={} L={})"
            .format(num, name, diam, v("r", "{:.2f}"), v("fl"), v("h"), v("l")))


def insert_tool_passports(gcode, tools, catalog):
    """Дописывает паспорт после каждой смены инструмента `( M6 T<n> )`.

    grbl_post печатает смену комментарием, поэтому и паспорт идёт комментарием
    рядом — так он переживает любой постпроцессор и не мешает стойке.
    """
    import re as _re
    by_num = {int(n): float(d) for n, d in tools.items()}
    out = []
    for line in gcode.splitlines(True):
        out.append(line)
        m = _re.search(r"\(\s*M0?6\s+T(\d+)\s*\)", line)
        if m:
            n = int(m.group(1))
            if n in by_num:
                out.append(tool_passport(n, by_num[n], catalog) + "\n")
    return "".join(out)


def reorder_first_positioning(gcode):
    """После смены инструмента — сначала XY, потом опускание по Z.

    Замечание ОЭЦМ: «после строки с выбором инструмента происходит потенциально
    опасное перемещение — сначала опускание по оси Z, а затем в плоскости XY».
    Так и есть: FreeCAD открывает операцию парой

        G0 Z28.984
        G0 X31.171 Y23.695 Z28.984

    и первая строка — это СПУСК с высоты смены инструмента, после которого
    фреза едет поперёк уже опущенной. Заводская программа делает наоборот
    (`PR_1_01`: `L X0 Y-28. FMAX` → `L Z44. FMAX`).

    Меняем местами ровно эту пару и только там, где спуск заведомо безопасен
    менять — в НАЧАЛЕ ПРОГРАММЫ и сразу после смены инструмента: там фреза
    стоит в точке смены, то есть в верхней точке хода по Z, и поперечное
    перемещение на этой высоте ничего не задевает. Внутри операции порядок уже
    правильный (отвод на плоскость безопасности, потом XY) — туда не лезем.
    """
    import re as _re
    lines = gcode.splitlines(True)
    out = []
    armed = True                    # начало программы = та же ситуация
    pend = []                       # отложенные «G0 только по Z», подряд
    nswap = 0

    def _axes(code):
        return {a: float(v) for a, v in _re.findall(r"\b([XYZ])(-?[\d.]+)", code)}

    for line in lines:
        code = line.split("(")[0].strip()
        if not code:                                   # комментарий
            if _re.search(r"\(\s*M0?6\s+T\d+\s*\)", line):
                out.extend(q[0] for q in pend)         # не потерять отложенное
                armed, pend = True, []
            out.append(line)
            continue
        if not armed or not _re.match(r"\s*G0*0\b(?!\d)", code):
            out.extend(q[0] for q in pend)             # пара не сложилась
            pend = []
            armed = armed and not _re.search(r"\b[XYZ]-?[\d.]", code)
            out.append(line)
            continue

        ax = _axes(code)
        if set(ax) == {"Z"}:
            # Ходов «только по Z» подряд бывает несколько: Path Engrave печатает
            # подъём на плоскость безопасности дважды. Копим их все, схлопывая
            # повтор одной и той же высоты, — иначе перестановка на такую пару
            # не срабатывает и «сначала Z, потом XY» остаётся в программе.
            if not pend or abs(pend[-1][1] - ax["Z"]) > 1e-6:
                pend.append((line, ax["Z"]))
            continue
        if not pend:
            armed = False
            out.append(line)
            continue

        # ждали пару: G0 в XY на той же высоте, что и последний спуск
        if ({"X", "Y"} <= set(ax)
                and abs(ax.get("Z", pend[-1][1]) - pend[-1][1]) < 1e-6):
            # XY идёт первым и БЕЗ Z — фреза едет поперёк на той высоте, где
            # стоит (точка смены инструмента), и только потом опускается.
            out.append(_re.sub(r"\s*Z-?[\d.]+", "", code) + "\n")
            out.extend(q[0] for q in pend)
            nswap += 1
        else:
            out.extend(q[0] for q in pend)
            out.append(line)
        pend, armed = [], False

    out.extend(q[0] for q in pend)
    if nswap:
        log(f"порядок первых перемещений: XY → Z, исправлено мест: {nswap}")
    return "".join(out)


class StockMap:
    """Модель снятого материала 2.5D: карта «верх материала» в узлах сетки XY.

    Нужна, чтобы отличить спуск сквозь ВОЗДУХ от врезания в металл. Path Surface
    начинает каждый проход от верха ГАБАРИТНОГО БОКСА заготовки и идёт вниз на
    рабочей подаче; у гнутого листа (заготовка 003 занимает 8 % своего бокса)
    почти весь этот спуск — воздух. На базовом прогоне 003 это 430 спусков,
    7854 мм и треть машинного времени.

    Одной исходной заготовки мало: проходы идут слоями, и ко второму слою
    материал сверху уже снят. Поэтому карта не статическая — `cut()` опускает её
    вслед за фрезой, как это делает воксельный симулятор, только в 2.5D.

    Обе стороны считаются с запасом В БЕЗОПАСНУЮ СТОРОНУ:
      * при построении треугольник растеризуется по своему прямоугольнику —
        материал получается не ниже настоящего;
      * `top()` берёт максимум по диску радиуса фрезы + шаг сетки;
      * `cut()` опускает карту по диску радиуса фрезы − шаг сетки, то есть
        снимает МЕНЬШЕ, чем снимет фреза.
    Итог: карта всегда не ниже настоящего материала, ошибка приводит к лишнему
    рабочему ходу, а не к холостому в металл.

    Тело считается сплошным вниз от верха — для гнутого листа это неверно
    физически (под полкой воздух), но в ту же безопасную сторону: под полку
    фреза на холостом ходу не поедет.
    """

    def __init__(self, verts, tris, bbox, tool_r, pitch=0.5):
        import numpy as np
        self.np = np
        self.pitch = float(pitch)
        self.bb = bbox
        self.r = float(tool_r)
        nx = int(math.ceil(bbox.XLength / self.pitch)) + 1
        ny = int(math.ceil(bbox.YLength / self.pitch)) + 1
        self.nx, self.ny = nx, ny
        self.H = np.full((nx, ny), -1e9)

        vx = [v.x for v in verts]
        vy = [v.y for v in verts]
        vz = [v.z for v in verts]
        for a, b, c in tris:
            xs = (vx[a], vx[b], vx[c])
            ys = (vy[a], vy[b], vy[c])
            z = max(vz[a], vz[b], vz[c])
            sub = self.H[self._gx(min(xs)):self._gx(max(xs)) + 1,
                         self._gy(min(ys)):self._gy(max(ys)) + 1]
            np.maximum(sub, z, out=sub)

        # координаты узлов — для векторного расстояния до отрезка
        self.X = bbox.XMin + np.arange(nx) * self.pitch
        self.Y = bbox.YMin + np.arange(ny) * self.pitch

    def _gx(self, x):
        return min(max(int((x - self.bb.XMin) / self.pitch), 0), self.nx - 1)

    def _gy(self, y):
        return min(max(int((y - self.bb.YMin) / self.pitch), 0), self.ny - 1)

    def _window(self, x0, y0, x1, y1, r):
        return (self._gx(min(x0, x1) - r), self._gx(max(x0, x1) + r) + 1,
                self._gy(min(y0, y1) - r), self._gy(max(y0, y1) + r) + 1)

    def _dist2(self, i0, i1, j0, j1, x0, y0, x1, y1):
        """Квадрат расстояния от узлов окна до отрезка (x0,y0)-(x1,y1)."""
        np = self.np
        gx = self.X[i0:i1][:, None]
        gy = self.Y[j0:j1][None, :]
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        if L2 < 1e-12:
            return (gx - x0) ** 2 + (gy - y0) ** 2
        t = ((gx - x0) * dx + (gy - y0) * dy) / L2
        t = np.clip(t, 0.0, 1.0)
        return (gx - (x0 + t * dx)) ** 2 + (gy - (y0 + t * dy)) ** 2

    def top(self, x, y, tool_r=None):
        """Верх материала в радиусе фрезы вокруг (x, y); -inf — материала нет.

        tool_r — радиус ТЕКУЩЕЙ фрезы: в многоинструментальной программе радиус
        меняется по ходу, и брать главную фрезу нельзя (см. `cut`)."""
        r = (self.r if tool_r is None else tool_r) + self.pitch   # запас наружу
        if not (self.bb.XMin - r <= x <= self.bb.XMax + r
                and self.bb.YMin - r <= y <= self.bb.YMax + r):
            return float("-inf")
        i0, i1, j0, j1 = self._window(x, y, x, y, r)
        d2 = self._dist2(i0, i1, j0, j1, x, y, x, y)
        sub = self.H[i0:i1, j0:j1]
        vals = sub[d2 <= r * r]
        return float(vals.max()) if vals.size else float("-inf")

    def cut(self, x0, y0, x1, y1, z, tool_r=None):
        """Снять материал вдоль отрезка на уровень z (радиус — с недобором).

        tool_r ОБЯЗАТЕЛЕН в многоинструментальной программе: снять по радиусу
        главной фрезы там, где работает Ø1, значит стереть в модели материал,
        которого фреза не касалась, — и следующий подвод уедет холостым в
        металл. Ошибка в эту сторону единственная опасная во всём пассе."""
        r = max((self.r if tool_r is None else tool_r) - self.pitch, 0.0)
        i0, i1, j0, j1 = self._window(x0, y0, x1, y1, r)
        if i0 >= i1 or j0 >= j1:
            return
        d2 = self._dist2(i0, i1, j0, j1, x0, y0, x1, y1)
        sub = self.H[i0:i1, j0:j1]
        m = d2 <= r * r
        self.np.copyto(sub, self.np.minimum(sub, z), where=m)


def _parse_blocks(gcode):
    """G-код → список кадров с посчитанными позициями. Комментарии сохраняются."""
    import re as _re
    out = []
    x = y = z = 0.0
    have = False
    mode = None
    feed = 0.0
    for raw in gcode.splitlines(True):
        code = raw.split("(")[0].strip()
        if not code:
            out.append({"raw": raw, "mode": None})
            continue
        g = _re.search(r"\bG(0|1|2|3)\b(?!\d)", code)
        if g:
            mode = int(g.group(1))
        f = _re.search(r"\bF(-?[\d.]+)", code)
        if f:
            feed = float(f.group(1))
        ax = {a: float(v) for a, v in _re.findall(r"\b([XYZ])(-?[\d.]+)", code)}
        if not ax or mode is None:
            out.append({"raw": raw, "mode": None})
            continue
        nx_, ny_, nz_ = ax.get("X", x), ax.get("Y", y), ax.get("Z", z)
        ijk = {a: float(v) for a, v in _re.findall(r"\b([IJK])(-?[\d.]+)", code)}
        out.append({"raw": raw, "mode": mode, "feed": feed,
                    "p0": None if not have else (x, y, z),
                    "p1": (nx_, ny_, nz_),
                    "ij": (ijk.get("I", 0.0), ijk.get("J", 0.0))})
        x, y, z, have = nx_, ny_, nz_, True
    return out


def _arc_track(p0, p1, ijk, cw, need, sag_max=0.005):
    """Дуга G2/G3 → ломаная от начала дуги, длиной не меньше `need`.

    Ломаная нужна, чтобы врезаться рампой перед контурным проходом: там за
    входом сразу идёт дуга, а по хорде дуги фрезу вести нельзя — для внешнего
    контура хорда лежит ВНУТРИ дуги, то есть в теле детали. Поэтому шаг
    выбирается по стрелке прогиба: sag_max 0.005 мм — на два порядка меньше
    шага воксельной сверки и любого допуска.
    """
    import math as _m
    cx, cy = p0[0] + ijk[0], p0[1] + ijk[1]
    r = _m.hypot(p0[0] - cx, p0[1] - cy)
    if r < 1e-6:
        return None
    a0 = _m.atan2(p0[1] - cy, p0[0] - cx)
    a1 = _m.atan2(p1[1] - cy, p1[0] - cx)
    da = a1 - a0
    if cw:
        while da >= 0:
            da -= 2 * _m.pi
    else:
        while da <= 0:
            da += 2 * _m.pi
    step = 2.0 * _m.acos(max(-1.0, min(1.0, 1.0 - sag_max / r)))  # угол на хорду
    if step < 1e-6:
        return None
    n = int(_m.ceil(min(abs(da), (need + step * r) / r) / step))
    pts = [(p0[0], p0[1])]
    s = -step if cw else step
    for k in range(1, n + 1):
        a = a0 + s * k
        if (a - a0) / (da if abs(da) > 1e-12 else 1.0) > 1.0:
            break
        pts.append((cx + r * _m.cos(a), cy + r * _m.sin(a)))
    return pts if len(pts) > 1 else None


def _ramp(entry, target, track, angle_deg, min_len=0.5):
    """Врезание «змейкой» по траектории, которую фреза всё равно сейчас режет.

    Замечание ОЭЦМ: «опасные вертикальные врезания». Ход вниз заменяется на
    спуск под углом ВДОЛЬ БУДУЩЕГО РЕЗА и обратно, столько раз, сколько нужно
    для глубины. Рампа не выходит за траекторию, которую программа и так
    собиралась пройти, поэтому зарезать ею нечего.

    entry — (x, y, z_сверху), target — z внизу, track — ломаная будущего реза,
    начинающаяся в точке входа (прямой ход = два узла, дуга = `_arc_track`).
    Возвращает список точек (x, y, z) или None, если рампу негде разместить —
    тогда остаётся отвесный вход на вертикальной подаче.
    """
    import math as _m
    x0, y0, z0 = entry
    depth = z0 - target
    if not track or len(track) < 2 or depth <= 0:
        return None
    # длина ломаной нарастающим итогом
    acc = [0.0]
    for a, b in zip(track, track[1:]):
        acc.append(acc[-1] + _m.hypot(b[0] - a[0], b[1] - a[1]))
    total = acc[-1]
    if total < min_len:
        return None

    def at(s):
        """Точка на ломаной на расстоянии s от начала."""
        s = max(0.0, min(total, s))
        for i in range(1, len(acc)):
            if s <= acc[i] or i == len(acc) - 1:
                seg = acc[i] - acc[i - 1]
                t = 0.0 if seg < 1e-12 else (s - acc[i - 1]) / seg
                a, b = track[i - 1], track[i]
                return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        return track[-1]

    def leg(pts, a, b, za, zb):
        """Ход по ломаной от a до b с выдачей ВСЕХ её узлов между ними.

        Без этого рампа по дуге пошла бы хордой: на контурном проходе радиусом
        2 мм это 0.18 мм внутрь дуги, а у внешнего контура внутри — тело детали.
        """
        mid = [s for s in acc if min(a, b) - 1e-9 < s < max(a, b) + 1e-9
               and abs(s - a) > 1e-9 and abs(s - b) > 1e-9]
        mid.sort(reverse=(b < a))
        span = b - a
        for s in mid + [b]:
            t = 1.0 if abs(span) < 1e-12 else (s - a) / span
            px, py = at(s)
            pts.append((px, py, za + t * (zb - za)))

    tan = _m.tan(_m.radians(angle_deg))
    pts, z, pos, direction, left, guard = [], z0, 0.0, 1, depth / tan, 0
    while left > 1e-9 and guard < 400:
        guard += 1
        room = (total - pos) if direction > 0 else pos
        if room < 1e-9:
            direction = -direction
            continue
        step = min(left, room)
        nxt = pos + step * direction
        nz = max(target, z - step * tan)
        leg(pts, pos, nxt, z, nz)
        pos, z = nxt, nz
        left -= step
        direction = -direction
    if not pts:
        return None
    if pos > 1e-9:                                   # вернуться в точку входа
        leg(pts, pos, 0.0, target, target)
    pts[-1] = (x0, y0, target)
    return pts


def optimize_links(gcode, stock_map, vert_feed, clearance=1.0, air_cuts=False,
                   ramp_angle=0.0, horiz_feed=None, tool_radii=None,
                   stepdown=None):
    """Подвод и врезание по модели снятого материала.

    Замечание ОЭЦМ: «лишние перемещения и опасные вертикальные врезания». У нас
    это одно место — длинный отвесный спуск на рабочей подаче от верха габарита
    заготовки (Path Surface начинает проход именно оттуда). Каждый такой спуск
    делится на две части:

      * холостую — до «верх материала + зазор»; где материала на вертикали нет
        вовсе, весь спуск становится холостым;
      * рабочую — остаток. При ramp_angle > 0 она идёт РАМПОЙ вдоль будущего
        реза, иначе отвесно, но на ВЕРТИКАЛЬНОЙ подаче (Path Surface ставит там
        горизонтальную — это и есть опасное врезание из отчёта).

    air_cuts=True переводит в холостой ход ещё и ГОРИЗОНТАЛЬНЫЕ рабочие ходы
    заведомо выше материала. Отдельным флагом: ошибка здесь стоит дороже —
    лишний спуск по воздуху безвреден, а пропущенный рез оставит металл.
    """
    import re as _re
    blocks = _parse_blocks(gcode)
    out = []
    n_cut = n_all = n_air = n_ramp = 0
    saved = 0.0
    # Радиус фрезы меняется по ходу многоинструментальной программы, а модель
    # снятого материала обязана считать по ТЕКУЩЕЙ: снять по радиусу главной
    # там, где работает Ø1, значит стереть материал, которого фреза не касалась.
    radii = {int(k): float(v) / 2.0 for k, v in (tool_radii or {}).items()}
    cur_r = None
    # ── боковой рез: фреза идёт на своей высоте, а в её следе стоит материал
    #    заметно выше дна хода — значит режет БОКОМ на всю эту глубину. Операция
    #    на это не рассчитана: подача выбрана под съём слоя, а не под паз в
    #    полтора десятка миллиметров, и заметить такое по сверке нельзя — сверка
    #    меряет тело детали, а срезается тут остаток заготовки.
    #    Ловится той же 2.5D-моделью снятого материала, по которой считается
    #    подвод. Радиус берётся с НЕДОБОРОМ (как в cut): материал ровно на
    #    границе диска — это стенка, вдоль которой фреза и должна идти.
    side_lim = None if stepdown is None else max(3.0 * float(stepdown), 3.0)
    side_max, side_at, side_op, cur_op = 0.0, None, None, None

    def head_of(code):
        h = _re.sub(r"\s*Z-?[\d.]+", "", code.split("F")[0]).strip()
        return _re.sub(r"^G0*[123]\b", "", h).strip()

    def next_cut_track(i, need):
        """Ломаная будущего реза после кадра i — по ней идёт рампа.

        Копится по НЕСКОЛЬКИМ ходам подряд, пока не наберётся нужная длина:
        одного хода не хватает. У контурного прохода первый отрезок после входа
        бывает длиной 0.16 мм (скругление угла зоны), и рампа по нему не
        помещалась — вход оставался отвесным. Дуги раскладываются в частую
        ломаную (`_arc_track`): по хорде фрезу вести нельзя.

        Останавливаемся на холостом ходе (после него фреза уже не там, где
        вошла) и на ходе, меняющем Z вместе с XY, — он не лежит в плоскости
        врезания. Чисто отвесные ходы пропускаем."""
        pts, total = [], 0.0
        for b in blocks[i + 1:i + 60]:
            if b.get("p0") is None:
                continue
            if b["mode"] == 0:
                break
            p0, p1 = b["p0"], b["p1"]
            flat = abs(p1[0] - p0[0]) > 1e-6 or abs(p1[1] - p0[1]) > 1e-6
            if abs(p1[2] - p0[2]) > 1e-6:
                if flat:
                    break                     # 3D-ход, не наша плоскость
                continue                      # отвесный — смотрим дальше
            if not flat:
                continue
            if b["mode"] == 1:
                seg = [(p0[0], p0[1]), (p1[0], p1[1])]
            else:
                seg = _arc_track(p0, p1, b.get("ij", (0.0, 0.0)),
                                 b["mode"] == 2, max(need - total, 0.1))
                if not seg:
                    break
            pts = seg if not pts else pts + seg[1:]
            total += sum(math.dist(a, c) for a, c in zip(seg, seg[1:]))
            if total >= need:
                break
        return pts if len(pts) >= 2 else None

    for i, b in enumerate(blocks):
        if b["mode"] is None or b.get("p0") is None:
            m = _re.search(r"\(\s*M0?6\s+T(\d+)\s*\)", b["raw"])
            if m:
                cur_r = radii.get(int(m.group(1)), cur_r)
            m = _re.search(r"\(Begin operation:\s*(.+?)\)", b["raw"])
            if m:
                cur_op = m.group(1)
            out.append(b["raw"])
            continue
        x, y, z = b["p0"]
        nx_, ny_, nz_ = b["p1"]
        if b["mode"] == 0:                           # холостой материал не трогает
            out.append(b["raw"])
            continue

        code = b["raw"].split("(")[0].strip()
        head = head_of(code)
        feed = b.get("feed") or vert_feed
        vertical = (abs(nx_ - x) < 1e-6 and abs(ny_ - y) < 1e-6 and nz_ < z - 1e-6)

        if vertical:
            zt = stock_map.top(x, y, cur_r)
            z_safe = min(z, zt + clearance)
            # Спуск целиком по воздуху — и когда материала на вертикали нет
            # вовсе, и когда фреза не доходит до его верха (зазор ещё не
            # выбран): врезания тут нет, рампа не нужна.
            if z_safe <= nz_ + 1e-3 or nz_ >= zt - 1e-3:
                out.append(_join("G0", head, nz_, None))
                n_all += 1
                saved += z - nz_
            else:
                if z_safe < z - 1e-3:
                    out.append(_join("G0", head, z_safe, None))
                    n_cut += 1
                    saved += z - z_safe
                pts = None
                if ramp_angle > 0:
                    need = (z_safe - nz_) / math.tan(math.radians(ramp_angle))
                    pts = _ramp((x, y, z_safe), nz_,
                                next_cut_track(i, need), ramp_angle)
                if pts:
                    fr = horiz_feed or feed
                    for px, py, pz in pts:
                        out.append("G1 X%.3f Y%.3f Z%.3f F%.3f\n"
                                   % (px, py, pz, fr))
                    n_ramp += 1
                else:
                    out.append(_join("G1", head, nz_, vert_feed))
        elif air_cuts and b["mode"] == 1 and min(z, nz_) > clearance + max(
                stock_map.top(x, y, cur_r), stock_map.top(nx_, ny_, cur_r),
                stock_map.top((x + nx_) / 2.0, (y + ny_) / 2.0, cur_r)):
            out.append(_join("G0", head, nz_, None))  # рабочий ход по воздуху
            n_air += 1
        else:
            out.append(b["raw"])

        if side_lim is not None and cur_r:
            zb = min(z, nz_)
            pr = max(cur_r - 2.0 * stock_map.pitch, stock_map.pitch)
            # Середину дуги нельзя брать как середину ХОРДЫ: она лежит внутри
            # дуги и на обводе контура заезжает в тело детали — ложная тревога
            # на каждом скруглении угла.
            pts_probe = [(x, y), (nx_, ny_), _mid_point(b, x, y, nx_, ny_)]
            for px, py in pts_probe:
                d = stock_map.top(px, py, pr) - zb
                if d > side_max:
                    side_max, side_at, side_op = d, (px, py, zb), cur_op

        stock_map.cut(x, y, nx_, ny_, min(z, nz_), cur_r)

    if side_lim is not None and side_max > side_lim:
        px, py, pz = side_at
        log(f"ВНИМАНИЕ: боковой рез — {side_op or '?'} идёт на Z={pz:.2f}, а в "
            f"следе фрезы стоит материал до Z={pz + side_max:.2f} "
            f"(X={px:.1f} Y={py:.1f}). Фреза срежет его боком на "
            f"{side_max:.1f} мм: этот материал должна была снять более ранняя "
            f"операция, и сверка такого не покажет — она меряет тело детали")

    if n_cut or n_all or n_air or n_ramp:
        log(f"подвод: {n_all} спусков целиком по воздуху, {n_cut} укорочено, "
            f"{n_ramp} врезаний рампой"
            + (f", {n_air} холостых резов" if n_air else "")
            + f"; снято с рабочей подачи {saved:.0f} мм")
    return "".join(out)


def _mid_point(b, x, y, nx_, ny_):
    """Середина кадра НА самой траектории: у дуги — по дуге, не по хорде."""
    if b["mode"] not in (2, 3):
        return ((x + nx_) / 2.0, (y + ny_) / 2.0)
    i, j = b.get("ij", (0.0, 0.0))
    cx, cy = x + i, y + j
    r = math.hypot(x - cx, y - cy)
    if r < 1e-9:
        return ((x + nx_) / 2.0, (y + ny_) / 2.0)
    a0 = math.atan2(y - cy, x - cx)
    a1 = math.atan2(ny_ - cy, nx_ - cx)
    da = a1 - a0
    if b["mode"] == 2:                       # G2 — по часовой
        while da >= 0:
            da -= 2 * math.pi
    else:
        while da <= 0:
            da += 2 * math.pi
    am = a0 + da / 2.0
    return (cx + r * math.cos(am), cy + r * math.sin(am))


def _join(g, head, z, feed):
    s = f"{g} {head} Z{z:.3f}" if head else f"{g} Z{z:.3f}"
    if feed is not None:
        s += f" F{feed:.3f}"
    return s + "\n"



def mill(doc, feat, p, stock_solid=None):
    """Последовательная обработка: черновая по этапам (отверстия → грани →
    периметр). → текст G-Code.
    stock_solid — произвольная заготовка из файла (уже в координатах детали);
    None — заготовка = габаритный бокс детали + поля."""
    bb = feat.Shape.BoundBox
    # зазор от стола: ни одна операция не опускается ниже дна детали + зазор
    # (деталь лежит на столе; сквозные вырезы оставляют плёнку этой толщины)
    p["_floor_limit"] = bb.ZMin + float(p.get("floor_clearance", 0.5))
    if p.get("floor_clearance", 0.5) > 0:
        log(f"зазор от стола: {p['floor_clearance']:g} мм — фреза не ниже "
            f"Z={p['_floor_limit']:.2f}")

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
    export_stock(job, p)

    tc = job.Tools.Group[0]
    feed = float(p["feed_rate"])
    rpm = float(p["spindle_speed"])
    # набор фрез (мм), по убыванию; главная (черновая) — крупнейшая ИЗ НАБОРА
    base_set = sorted({round(float(x), 3) for x in
                       (p.get("tool_set") or [p["tool_diameter"]])}, reverse=True)
    tool_d = base_set[0]
    p["tool_diameter"] = tool_d          # шапка/пороги — от главной фрезы
    # Пофрезные переопределения (SET_OP_TOOLS, действие set_op_tool из auto_fix)
    # добавляются в набор: choose_tc выбирает БЛИЖАЙШИЙ доступный диаметр, и без
    # этого просьба «возьми Ø3 на RoughSlope2» молча прилипала бы к единственной
    # фрезе набора. Для ЛЛМ-петли смена фрезы — первая ступень лестницы правок,
    # так что вместе с одноинструментальным дефолтом она бы отвалилась.
    extra = {round(float(v), 3) for v in (p.get("set_op_tools") or {}).values()}
    tset = sorted(set(base_set) | extra, reverse=True)
    if extra - set(base_set):
        log("фрезы из SET_OP_TOOLS добавлены в набор: "
            + ", ".join("Ø%g" % d for d in sorted(extra - set(base_set),
                                                  reverse=True)))

    def _setup_tc(tcobj, d, num):
        set_prop(tcobj.Tool, "Diameter", FreeCAD.Units.Quantity(f"{d} mm"))
        tcobj.HorizFeed = FreeCAD.Units.Quantity(f"{feed} mm/min")
        tcobj.VertFeed = FreeCAD.Units.Quantity(f"{feed / 4.0} mm/min")
        tcobj.SpindleSpeed = rpm
        set_prop(tcobj, "ToolNumber", num)
        # дефолтный инструмент Job зовётся «5mm Endmill» независимо от диаметра —
        # переименовываем, чтобы комментарий (TC: ...) в G-Code не врал
        tcobj.Label = f"TC: Endmill D{d:g}mm"
        tcobj.Tool.Label = f"Endmill D{d:g}mm"

    _setup_tc(tc, tool_d, 1)
    pool = {tool_d: tc}
    if len(tset) > 1:
        import Path.Tool.Controller as Controller
        for i, d in enumerate([x for x in tset if x != tool_d], start=2):
            bit = doc.copyObject(tc.Tool)
            tcn = Controller.Create(f"TC_D{d:g}", tool=bit, toolNumber=i)
            _setup_tc(tcn, d, i)
            pool[d] = tcn        # НЕ в job.Tools пока — иначе Op.Create падает
        log(f"набор фрез: {', '.join('Ø%g' % d for d in tset)} "
            f"(главная Ø{tool_d:g}); неглавные добавятся в программу по факту")
    p["_tool_pool"] = pool
    p["_tool_diams"] = tset
    doc.recompute()

    # в описание идёт только диаметр — единственный размер, который мы задаём;
    # остальные размеры (длина, хвостовик) у дефолтного инструмента FreeCAD —
    # библиотечные заглушки, печатать их в программу опасно
    tool_desc = (f"endmill (flat), D{tool_d:g} mm" if len(tset) == 1
                 else "endmills " + "/".join(f"D{d:g}" for d in tset)
                      + f" mm (main D{tool_d:g})")
    log(f"фреза: концевая плоская (endmill) Ø{tool_d:g} мм"
        + (f" +{len(tset) - 1}" if len(tset) > 1 else "")
        + f" | подача {p['feed_rate']:g} мм/мин | шпиндель {p['spindle_speed']:g} об/мин")

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
    )

    ops = []
    if p.get("rough_allowance", 0) > 0:
        if p.get("rough_mode", "stages") == "layers":
            ops.extend(make_layered_ops(doc, job, tc, feat.Shape, p))
        else:
            ops.extend(make_roughing_ops(doc, job, tc, feat.Shape, p))
    else:
        log("черновая отключена (ROUGH_ALLOWANCE=0)")

    if not ops:
        raise RuntimeError("ни одной операции с траекторией — проверьте параметры")

    # многоинструментальность: неглавные фрезы во время создания операций держались
    # ВНЕ job.Tools (иначе FreeCAD роняет Op.Create при >1 инструменте). Теперь
    # добавляем в группу те, что реально задействованы, и перенумеровываем —
    # постпроцессор впишет смены инструмента; неиспользованные фрезы удаляем.
    used = {}
    for op in ops:
        tco = getattr(op, "ToolController", None)
        if tco is not None and tco is not tc:
            used[tco.Name] = tco
    for n, tco in enumerate(used.values(), start=2):
        if tco not in job.Tools.Group:
            job.Tools.addObject(tco)
        set_prop(tco, "ToolNumber", n)
    for d, tco in list(pool.items()):
        if tco is not tc and tco.Name not in used:
            try:
                doc.removeObject(tco.Name)
            except Exception:
                pass
    if used:
        doc.recompute()
        allt = [tc] + list(used.values())
        log(f"инструментов в программе: {len(allt)} (" + ", ".join(
            f"T{getattr(t, 'ToolNumber', '?')} Ø{t.Tool.Diameter.Value:g}"
            for t in allt) + ")")

    # порядок операций = порядок выполнения
    tools_map = {getattr(t, "ToolNumber", 0): t.Tool.Diameter.Value
                 for t in ([tc] + list(used.values()))}
    body = export_gcode(job, ops, p["postprocessor"])
    body = insert_tool_passports(body, tools_map, p.get("tool_catalog"))
    if p.get("safe_start_order", True):
        body = reorder_first_positioning(body)
    if p.get("air_plunge_rapid", True):
        try:
            sh = job.Stock.Shape
            verts, tris = sh.tessellate(0.2)
            smap = StockMap(verts, tris, sh.BoundBox, tool_d / 2.0)
            body = optimize_links(
                body, smap, float(feed) / 4.0,
                float(p.get("air_plunge_clearance", 1.0)),
                air_cuts=bool(p.get("air_cuts_rapid", False)),
                ramp_angle=float(p.get("ramp_angle", 0.0)),
                horiz_feed=float(feed), tool_radii=tools_map,
                stepdown=float(p.get("rough_stepdown", 1.0)))
        except Exception as e:   # без модели программа валидна, просто длиннее
            log(f"warn: подвод не оптимизирован ({e})")
    return p["_gcode_header"] + body


def main():
    with open(os.environ["FREECAD_WORKER_PARAMS"]) as f:
        p = json.load(f)

    doc = FreeCAD.newDocument("CAM")
    solid = load_model(p["model_path"], p.get("scale_to_mm", 1.0))
    if not solid.isValid():
        log("warn: тело детали невалидно — пробую починить (sew + makeSolid)")
        try:
            sh = solid.copy()
            sh.sewShape()
            fixed = Part.makeSolid(sh)
            if fixed.isValid() and fixed.Volume > 0:
                solid = fixed
                log("деталь починена")
            else:
                log("warn: починить не удалось — продолжаю на исходном "
                    "(грани с неопределённой поверхностью пропускаются)")
        except Exception as e:
            log(f"warn: починка не удалась ({e}) — продолжаю, "
                f"битые грани пропускаются")
    journal = []   # трансформации детали — повторяются на заготовке из файла
    if p.get("auto_orient", True):
        solid = auto_orient(solid, journal)
        solid = orient_hole_axis_up(solid, journal)   # полка с отверстием — в XY
        solid = orient_flange_down(solid, journal)    # полка вниз, стенка вверх
        solid = orient_features_up(solid, journal)
        solid = orient_wall_to_yz(solid, journal)     # стенка — в плоскость YZ
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
        if p.get("stock_align"):
            # координатам файла заготовки не доверяем (он мог быть привязан к
            # другой детали сборки) — выравниваем по детали
            stock_solid = align_stock(stock_solid, solid.BoundBox)
            sab = stock_solid.BoundBox
            log(f"заготовка выровнена по детали (X край в край, Y центр, дно в дно): "
                f"X {sab.XMin:.1f}..{sab.XMax:.1f}, Y {sab.YMin:.1f}..{sab.YMax:.1f}, "
                f"Z {sab.ZMin:.1f}..{sab.ZMax:.1f}")
        else:
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

    gcode = mill(doc, feat, p, stock_solid)

    with open(p["gcode_path"], "w", encoding="utf-8") as f:
        f.write(gcode)
    log(f"OK gcode_lines={gcode.count(chr(10)) + 1} path={p['gcode_path']}")


# freecadcmd исполняет этот файл как скрипт (не как __main__), поэтому вызываем напрямую
if os.environ.get("FREECAD_WORKER_PARAMS"):
    try:
        main()
    except Exception:
        import traceback
        # префикс [worker] на КАЖДОЙ строке traceback — иначе хост (он показывает
        # только строки с [worker]) проглотит настоящую причину падения
        for _line in traceback.format_exc().splitlines():
            log(_line)
        raise
