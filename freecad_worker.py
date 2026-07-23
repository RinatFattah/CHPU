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
    # строчки вдоль ДЛИННОЙ стороны грани (меньше проходов и врезаний):
    # CutPatternAngle 0° = строчки вдоль X, 90° = вдоль Y
    try:
        fbb = model_obj.Shape.Faces[face_idx - 1].BoundBox
        cut_angle = 0.0 if fbb.XLength >= fbb.YLength else 90.0
    except Exception:
        cut_angle = 0.0
    set_prop(op, "CutPatternAngle", cut_angle)
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
    op.ClearanceHeight.Value = start_z + p["safe_height"]
    op.SafeHeight.Value = start_z + 3.0
    doc.recompute()

    n = len(op.Path.Commands) if op.Path else 0
    log(f"{name}: {n} команд (террасы по поверхности)")
    return op if n > 2 else None


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
    bb = shape.BoundBox
    sb = job.Stock.Shape.BoundBox
    start_z = sb.ZMax                      # верх заготовки

    sil = build_silhouette(shape, bb, p["rough_stepdown"])
    ops = []
    stock_shape = job.Stock.Shape

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

    # ── 1) сквозные вырезы любой формы, ПЕРВЫМИ (деталь ещё жёстко в заготовке) ──
    if sil is None:
        log("warn: силуэт не построился — вырезы и внешний контур пропущены")
    for i, region in enumerate(find_through_cuts(sil) if sil is not None else [], 1):
        rb = region.BoundBox
        hole_top = local_start(region)
        if hole_top is None:
            log(f"RoughHole{i}: над вырезом нет материала заготовки — пропущено")
            continue
        # сквозной вырез режем до дна ДЕТАЛИ (bb.ZMin), а НЕ до floor_z
        # (дно + припуск): у сквозного отверстия нет дна, чтобы оставлять там
        # припуск под чистовую — иначе на дне стоит кожура (видно без FINISH).
        # Припуск по стенкам (StockToLeave) при этом сохраняется.
        op = make_adaptive(doc, job, tc, f"RoughHole{i}", region, p,
                           hole_top, bb.ZMin, alw_xy)
        if not op and min(rb.XLength, rb.YLength) > float(p["tool_diameter"]) + 0.2:
            # узкий паз: адаптивной негде сделать винтовой заход, но фреза в паз
            # проходит — контурный обход ИЗНУТРИ (вход вертикальным врезанием)
            log(f"RoughHole{i}: узкий вырез — перехожу на контурный проход изнутри")
            op = make_profile(doc, job, tc, f"RoughHole{i}", region, p,
                              hole_top, bb.ZMin, alw_xy, side="Inside")
        if op:
            ops.append(op)
            write_partial(job, ops, p, f"готов вырез {i} "
                                       f"(~{rb.XLength:.0f}x{rb.YLength:.0f} мм)")
        else:
            log(f"RoughHole{i}: фреза Ø{p['tool_diameter']} с припуском не влезает "
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
        faces.append({"kind": "planar", "z": fc["z"], "final": fc["z"] + alw_z,
                      "region": fc["region"], "idx": fc["idx"], "area": fc["area"],
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
                      "rect": rect, "idx": idx, "area": f.Area,
                      "cx": mid.x, "cy": mid.y})

    # сортировка сверху вниз; на одном уровне — от ближней к дальней
    levels = sorted({round(f["z"], 3) for f in faces}, reverse=True)
    ordered = []
    for lv in levels:
        ordered += _nearest_order([f for f in faces if round(f["z"], 3) == lv],
                                  lambda f: (f["cx"], f["cy"]))

    face_n = slope_n = 0
    for fc in ordered:
        if fc["kind"] == "planar":
            top = local_start(fc["region"])
            if top is None or fc["final"] >= top - 1e-6:
                if top is None:
                    log(f"RoughFace (Z={fc['z']:.1f}): материала над гранью нет — "
                        f"пропущено")
                continue  # грань вровень с верхом материала — снимать нечего
            face_n += 1
            name = f"RoughFace{face_n}"
            op = make_adaptive(doc, job, tc, name, fc["region"], p,
                               top, fc["final"], alw_xy)
            if not op:
                # узкая полка — адаптивной выборке негде развернуться; снимаем
                # террасами по поверхности, как криволинейные грани
                log(f"{name}: узкая грань — перехожу на террасы по поверхности")
                op = make_surface_rough(doc, job, tc, name, jm, fc["idx"], p,
                                        top, fc["final"], alw_z)
            note = f"готова грань {face_n} (Z={fc['z']:.1f}, {fc['area']:.0f} мм²)"
        else:
            top = local_start(fc["rect"])
            if top is None:
                log(f"RoughSlope (Z={fc['z']:.1f}): материала над гранью нет — "
                    f"пропущено")
                continue
            slope_n += 1
            name = f"RoughSlope{slope_n}"
            op = make_surface_rough(doc, job, tc, name, jm, fc["idx"], p,
                                    top, fc["final"], alw_z)
            note = f"готова криволинейная грань {slope_n} ({fc['area']:.0f} мм²)"
        if op:
            ops.append(op)
            write_partial(job, ops, p, note)
        else:
            log(f"{name}: (Z={fc['z']:.1f}) пустая траектория — пропущено")
    if skipped > 1.0:
        log(f"warn: {skipped:.0f} мм² поверхностей смотрят вниз/вбок или накрыты "
            f"материалом — сверху не достать, это второй установ")

    # ── 3) внешний контур детали по силуэту — ПОСЛЕДНИМ: пока деталь жёстко
    #      держится в заготовке, снят весь объём выше; периметр обходим в конце.
    #      Один обвод Profile снаружи вдоль силуэта детали; лишний материал в
    #      углах заготовки, не касающийся детали, остаётся (так просил техпроцесс).
    #      Прорезаем именно внешний периметр детали, не выбирая всё поле. ──
    if sil is not None:
        try:
            filled = Part.makeFace([f.OuterWire for f in sil.Faces],
                                   "Part::FaceMakerBullseye")
        except Exception as e:
            filled = None
            log(f"warn: внешний контур не построился: {e}")
        if filled is not None:
            # внешний контур режем до дна ДЕТАЛИ (bb.ZMin) ВСЕГДА, без припуска
            # по дну: периметр отделяет деталь от рамки заготовки — кожура на дне
            # не нужна. Припуск по стенке (OffsetExtra) при этом сохраняется.
            op = make_profile(doc, job, tc, "RoughPerimeter", filled, p,
                              start_z, bb.ZMin, alw_xy)
            if op:
                ops.append(op)
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
    export_stock(job, p)

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
    )

    ops = []
    if p.get("rough_allowance", 0) > 0:
        if p.get("rough_mode", "stages") == "layers":
            ops.extend(make_layered_ops(doc, job, tc, feat.Shape, p))
        else:
            ops.extend(make_roughing_ops(doc, job, tc, feat.Shape, p))
    else:
        log("черновая отключена (ROUGH_ALLOWANCE=0)")

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
