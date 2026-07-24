"""
freecad_describe_worker.py — выполняется ВНУТРИ FreeCAD (freecadcmd).

STEP/IGES/BREP → JSON-описание геометрии, удобное для LLM: габарит, объём,
грани по типам с размерами, отверстия (сквозные/глухие), верхние плоскости,
текстовая сводка. Параметры — JSON-файлом (env FREECAD_DESCRIBE_PARAMS):
  model_path — входной CAD-файл; json_path — куда писать результат.
"""

import json
import math
import os
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import FreeCAD
import Part


def log(msg):
    print(f"[describe] {msg}", flush=True)


def r(x, nd=3):
    return round(float(x), nd)


def vec(v, nd=3):
    return [r(v.x, nd), r(v.y, nd), r(v.z, nd)]


def face_entry(f):
    """Одна грань → компактная запись с типом и главными размерами."""
    s = f.Surface
    kind = type(s).__name__
    e = {"type": kind.lower(), "area_mm2": r(f.Area, 1)}
    bb = f.BoundBox
    e["bbox"] = {"x": [r(bb.XMin), r(bb.XMax)], "y": [r(bb.YMin), r(bb.YMax)],
                 "z": [r(bb.ZMin), r(bb.ZMax)]}
    try:
        if kind == "Plane":
            n = f.normalAt(0, 0)
            e["normal"] = vec(n)
            if abs(n.z) > 0.999:
                e["orientation"] = "horizontal_up" if n.z > 0 else "horizontal_down"
            elif abs(n.z) < 0.001:
                e["orientation"] = "vertical"
            else:
                e["orientation"] = f"slanted_{r(math.degrees(math.acos(abs(n.z))), 1)}deg"
        elif kind == "Cylinder":
            e["diameter_mm"] = r(2.0 * s.Radius)
            e["axis"] = vec(s.Axis)
            e["height_mm"] = r(bb.ZLength if abs(s.Axis.z) > 0.999
                               else max(bb.XLength, bb.YLength, bb.ZLength))
            u0, u1, v0, v1 = f.ParameterRange
            mid = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
            nrm = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
            radial = mid.sub(FreeCAD.Vector(s.Center.x, s.Center.y, s.Center.z))
            radial.z = 0 if abs(s.Axis.z) > 0.999 else radial.z
            e["concave"] = bool(nrm.dot(radial) < 0)  # вогнутая = стенка отверстия
            e["center"] = vec(s.Center)
        elif kind in ("Cone",):
            e["half_angle_deg"] = r(math.degrees(s.SemiAngle), 1)
            e["axis"] = vec(s.Axis)
        elif kind in ("Sphere", "Toroid", "Torus"):
            e["radius_mm"] = r(s.Radius)
    except Exception:
        pass
    return e


def find_holes(solid):
    """Цилиндрические отверстия с вертикальной осью: диаметр, глубина,
    сквозное/глухое (пробы точкой над и под, как в auto-orient CAM-worker'а)."""
    holes = {}
    for f in solid.Faces:
        s = f.Surface
        if type(s).__name__ != "Cylinder" or abs(s.Axis.z) < 0.999:
            continue
        u0, u1, v0, v1 = f.ParameterRange
        mid = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
        nrm = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        radial = FreeCAD.Vector(mid.x - s.Center.x, mid.y - s.Center.y, 0)
        if nrm.dot(radial) > 0:
            continue  # выпуклая стенка (бобышка), не отверстие
        key = (r(s.Center.x, 2), r(s.Center.y, 2), r(s.Radius, 3))
        fb = f.BoundBox
        cur = holes.setdefault(key, {"zmin": fb.ZMin, "zmax": fb.ZMax})
        cur["zmin"] = min(cur["zmin"], fb.ZMin)
        cur["zmax"] = max(cur["zmax"], fb.ZMax)
    out = []
    for (cx, cy, rad), zz in holes.items():
        top_open = not solid.isInside(
            FreeCAD.Vector(cx, cy, zz["zmax"] + 0.2), 1e-6, True)
        bot_open = not solid.isInside(
            FreeCAD.Vector(cx, cy, zz["zmin"] - 0.2), 1e-6, True)
        kind = ("through" if top_open and bot_open else
                "blind_from_top" if top_open else
                "blind_from_bottom" if bot_open else "internal")
        out.append({"type": kind, "diameter_mm": r(2 * rad),
                    "depth_mm": r(zz["zmax"] - zz["zmin"]),
                    "center_xy": [cx, cy], "z_range": [r(zz["zmin"]), r(zz["zmax"])]})
    return sorted(out, key=lambda h: -h["diameter_mm"])


def summary_text(d):
    """Короткая сводка по-русски — затравка для LLM-промпта."""
    sz = d["bbox"]["size_mm"]
    parts = [f"Тело {sz[0]}×{sz[1]}×{sz[2]} мм, объём {d['volume_mm3'] / 1000:.1f} см³, "
             f"{d['counts']['faces']} граней"]
    ft = d["face_types"]
    parts.append("грани: " + ", ".join(f"{k}×{v}" for k, v in ft.items()))
    if d["holes"]:
        hs = ", ".join(f"Ø{h['diameter_mm']}({'сквозное' if h['type'] == 'through' else 'глухое'})"
                       for h in d["holes"][:5])
        parts.append(f"отверстия: {hs}")
    up = [f for f in d["faces"] if f.get("orientation") == "horizontal_up"]
    if up:
        levels = sorted({f["bbox"]["z"][0] for f in up}, reverse=True)
        parts.append(f"горизонтальные полки на Z: {levels[:6]}")
    return "; ".join(parts)


def main():
    with open(os.environ["FREECAD_DESCRIBE_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)

    shape = Part.Shape()
    shape.read(p["model_path"])
    solids = shape.Solids
    solid = max(solids, key=lambda s: s.Volume) if solids else shape

    bb = solid.BoundBox
    com = solid.CenterOfMass if solids else FreeCAD.Vector(
        (bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2, (bb.ZMin + bb.ZMax) / 2)
    faces = sorted((face_entry(f) for f in solid.Faces),
                   key=lambda e: -e["area_mm2"])
    face_types = {}
    for e in faces:
        face_types[e["type"]] = face_types.get(e["type"], 0) + 1

    d = {
        "file": os.path.basename(p.get("source_name") or p["model_path"]),
        "units": "mm",
        "is_solid": bool(solids),
        "counts": {"solids": len(solids), "faces": len(solid.Faces),
                   "edges": len(solid.Edges), "vertices": len(solid.Vertexes)},
        "bbox": {"x": [r(bb.XMin), r(bb.XMax)], "y": [r(bb.YMin), r(bb.YMax)],
                 "z": [r(bb.ZMin), r(bb.ZMax)],
                 "size_mm": [r(bb.XLength), r(bb.YLength), r(bb.ZLength)]},
        "volume_mm3": r(solid.Volume, 1),
        "area_mm2": r(solid.Area, 1),
        "center_of_mass": vec(com),
        "face_types": face_types,
        "holes": find_holes(solid) if solids else [],
        # грани отсортированы по площади; хвост мелочи обрезан, чтобы JSON
        # оставался компактным для контекста LLM
        "faces": faces[:int(p.get("max_faces", 40))],
        "faces_truncated": max(0, len(faces) - int(p.get("max_faces", 40))),
    }
    d["summary"] = summary_text(d)

    with open(p["json_path"], "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    log(f"OK faces={len(faces)} holes={len(d['holes'])} json={p['json_path']}")


if os.environ.get("FREECAD_DESCRIBE_PARAMS"):
    main()
