#!/usr/bin/env python3
"""
freecad_diff_worker.py — исполняется ВНУТРИ FreeCAD (freecadcmd), не в обычном Python.

Булево сравнение «деталь vs результат симуляции» (оба в координатах ПРОГРАММЫ):
  недорез = (результат ∩ призма силуэта детали) − деталь — материал, который
            остался, но должен был быть снят (анализ только в границах детали:
            рамка заготовки вокруг периметра — это НЕ недорез, её оставляют);
  зарез   = деталь − результат — материал, который должен был остаться, но снят.

Недорезы, целиком лежащие ниже «дно + floor_clearance», складываются в отдельную
графу floor_skin (намеренная плёнка от стола) и дефектом не считаются.

Параметры (env FREECAD_DIFF_PARAMS, JSON): part (STEP детали), result (STEP
результата), json_path, floor_clearance, min_volume (мм³, фильтр шума
фасетирования результата).
"""

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

import FreeCAD
import Part
import Mesh


def log(msg):
    print(f"[diff] {msg}", flush=True)


def mesh_launder(s, tol=0.1):
    """Перестраивает тело через меш (tessellate → makeShapeFromMesh → makeSolid).
    Фасетные IPW-тела из NX OCCT в булевых операциях не переваривает (Null shape
    при любом fuzzy) — а «отстиранный» через меш солид работает; для уже
    треугольных граней потери точности практически нет."""
    verts, tris = s.tessellate(tol)
    m = Mesh.Mesh([[verts[i], verts[j], verts[k]] for i, j, k in tris])
    sh = Part.Shape()
    sh.makeShapeFromMesh(m.Topology, 0.05)
    out = Part.makeSolid(sh)
    if out.Volume < 0:
        out = out.copy()
        out.reverse()
    return out


def load_solid(path):
    shape = Part.Shape()
    shape.read(path)
    solids = shape.Solids or [Part.makeSolid(shape)]
    s = max(solids, key=lambda x: abs(x.Volume))
    if s.Volume < 0:
        # фасетные тела из NX бывают «вывернуты» (нормали внутрь): объём
        # отрицательный, булевы операции дают Null — переворачиваем ориентацию
        s = s.copy()
        s.reverse()
        log(f"{os.path.basename(path)}: тело вывернуто — ориентация исправлена "
            f"({s.Volume / 1000.0:.1f} см³)")
    if not s.isValid():
        try:
            s = s.copy()
            s.fix(0.01, 0.01, 0.1)   # фасетные тела часто с мелкими дефектами
            log(f"{os.path.basename(path)}: тело починено (fix)")
        except Exception as e:
            log(f"warn: починка тела не удалась: {e}")
    return s


def boolop(a, op, b, what):
    """Булева операция с эскалацией fuzzy-допуска: точные BREP против
    фасетного тела симуляции без допуска часто дают Null shape."""
    last = None
    for tol in (None, 0.05, 0.2):
        try:
            r = getattr(a, op)(b) if tol is None else getattr(a, op)(b, tol)
            if not r.isNull():
                if tol:
                    log(f"{what}: булева прошла с допуском {tol}")
                return r
        except Exception as e:
            last = e
    raise RuntimeError(f"булева {what} не удалась: {last}")


def silhouette_prism(solid):
    """Призма над ЗАЛИТЫМ силуэтом детали (внешние контуры) — границы анализа
    недореза. Всё вне призмы (рамка заготовки) недорезом не считается."""
    bb = solid.BoundBox
    zs, z = [bb.ZMax - 0.05], bb.ZMax - 2.0
    while z > bb.ZMin + 0.05:
        zs.append(z)
        z -= 2.0
    zs.append(bb.ZMin + 0.05)
    faces = []
    for zz in zs:
        try:
            wires = [w for w in solid.slice(FreeCAD.Vector(0, 0, 1), zz)
                     if w.isClosed()]
        except Exception:
            continue
        for w in wires:
            w = w.copy()
            w.translate(FreeCAD.Vector(0, 0, -zz))
            try:
                faces.append(Part.Face(w))   # залитый контур (без отверстий)
            except Exception:
                pass
    if not faces:
        raise RuntimeError("силуэт детали не построился")
    sil = faces[0] if len(faces) == 1 else faces[0].fuse(faces[1:])
    sil = Part.makeFace([f.OuterWire for f in sil.Faces], "Part::FaceMakerBullseye")
    sil.translate(FreeCAD.Vector(0, 0, bb.ZMin - 1.0))
    return sil.extrude(FreeCAD.Vector(0, 0, bb.ZLength + 2.0))


def solid_entry(s):
    b = s.BoundBox
    c = s.CenterOfMass
    return {
        "volume_mm3": round(s.Volume, 2),
        "center": [round(c.x, 1), round(c.y, 1), round(c.z, 1)],
        "size": [round(b.XLength, 1), round(b.YLength, 1), round(b.ZLength, 1)],
        "bbox": {"x": [round(b.XMin, 1), round(b.XMax, 1)],
                 "y": [round(b.YMin, 1), round(b.YMax, 1)],
                 "z": [round(b.ZMin, 1), round(b.ZMax, 1)]},
    }


def main():
    with open(os.environ["FREECAD_DIFF_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)
    min_vol = float(p.get("min_volume", 2.0))
    clearance = float(p.get("floor_clearance", 0.5))

    part = load_solid(p["part"])
    result = load_solid(p["result"])
    try:
        result = mesh_launder(result)
        log(f"результат перестроен через меш ({result.Volume / 1000.0:.1f} см³)")
    except Exception as e:
        log(f"warn: перестройка результата через меш не удалась ({e}) — как есть")
    pb = part.BoundBox
    floor_z = pb.ZMin + clearance
    log(f"деталь {part.Volume / 1000.0:.1f} см³, результат "
        f"{result.Volume / 1000.0:.1f} см³")

    # ВАЖНО (OCCT-квирк): булевы с фасетным телом надёжны, только когда оно
    # СПРАВА (part.cut(result) — ок; result.cut(part) молча не вычитает).
    # Поэтому недорез считается от «пустоты»: B = бокс габарита − деталь
    # (место, которое должно быть пустым); выбрано = B − результат;
    # недорез = B − выбрано. Фасетное тело — только правым операндом.
    box = Part.makeBox(pb.XLength, pb.YLength, pb.ZLength,
                       FreeCAD.Vector(pb.XMin, pb.YMin, pb.ZMin))
    B = box.cut(part)                                    # должно быть пусто
    empty_ok = boolop(B, "cut", result, "выбрано")       # реально выбрано
    undercut_raw = boolop(B, "cut", empty_ok, "недорез") # осталось невыбранным
    overcut_raw = boolop(part, "cut", result, "зарез")   # снято лишнее

    # намеренную плёнку у дна отрезаем СЛОЕМ (куском её не поймать: плёнка
    # связана с настоящими недорезами в одно тело через дно)
    floor_skin = 0.0
    if clearance > 0:
        slab = Part.makeBox(pb.XLength + 2, pb.YLength + 2,
                            (floor_z - pb.ZMin) + 0.06,
                            FreeCAD.Vector(pb.XMin - 1, pb.YMin - 1,
                                           pb.ZMin - 0.01))
        above = boolop(undercut_raw, "cut", slab, "над полом")
        floor_skin = max(0.0, undercut_raw.Volume - above.Volume)
        undercut_raw = above
    def real_defects(solids):
        """Фильтр: мелочь и «диффузные плёнки» фасетирования (тело заполняет
        <2% своего bbox — размазанная кожица толщиной сотые мм) — это шум
        тесселяции, не дефект. Компактный зарез/недорез заполняет bbox плотно."""
        keep, noise = [], 0.0
        for s in solids:
            if s.Volume < min_vol:
                continue
            b = s.BoundBox
            bbox_vol = max(b.XLength * b.YLength * b.ZLength, 1e-9)
            if s.Volume / bbox_vol < 0.02:
                noise += s.Volume
                continue
            keep.append(s)
        return keep, noise

    undercuts, u_noise = real_defects(undercut_raw.Solids)
    overcuts, o_noise = real_defects(overcut_raw.Solids)
    if u_noise or o_noise:
        log(f"шум фасетирования отброшен: недорез {u_noise:.1f} / "
            f"зарез {o_noise:.1f} мм³")
    inside_vol = part.Volume + floor_skin + sum(s.Volume for s in undercuts)

    undercuts.sort(key=lambda s: -s.Volume)
    overcuts.sort(key=lambda s: -s.Volume)
    data = {
        "part_volume_mm3": round(part.Volume, 1),
        "result_volume_in_part_footprint_mm3": round(inside_vol, 1),
        "floor_clearance_mm": clearance,
        "floor_skin_mm3": round(floor_skin, 1),
        "tessellation_noise_mm3": round(u_noise + o_noise, 1),
        "undercut_total_mm3": round(sum(s.Volume for s in undercuts), 1),
        "overcut_total_mm3": round(sum(s.Volume for s in overcuts), 1),
        "undercuts": [solid_entry(s) for s in undercuts[:15]],
        "overcuts": [solid_entry(s) for s in overcuts[:15]],
        "note": "недорез — материал, оставшийся в границах детали сверх модели; "
                "зарез — снятое, что должно было остаться. floor_skin — намеренная "
                "плёнка у дна (зазор от стола), НЕ дефект. Рамка заготовки вне "
                "силуэта детали не учитывается.",
    }
    with open(p["json_path"], "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    log(f"OK undercuts={len(undercuts)} overcuts={len(overcuts)} "
        f"floor_skin={floor_skin:.0f}mm3 json={p['json_path']}")


if os.environ.get("FREECAD_DIFF_PARAMS"):
    try:
        main()
    except Exception:
        import traceback
        for _line in traceback.format_exc().splitlines():
            log(_line)
        raise
