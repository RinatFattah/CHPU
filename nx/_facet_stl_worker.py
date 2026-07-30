"""Повернуть ФАСЕТНОЕ тело NX (результат ISV) в другую раму и записать STL.

BREP-поворот через OCCT рассыпает фасетное тело (exportStep → 0 граней),
поэтому работаем как с мешем: тесселляция → поворот вершин → STL. NX и любой
вьюер STL читают. Выполняется ВНУТРИ freecadcmd.

Параметры — env FACET_PARAMS (JSON): in (STEP), out_stl, axis [x,y,z],
angle (град), tol (точность тесселляции, опц.).
"""
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if os.environ.get("FACET_PARAMS"):
    import FreeCAD as App
    import Part
    import Mesh

    with open(os.environ["FACET_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)
    sh = Part.Shape()
    sh.read(p["in"])
    verts, tris = sh.tessellate(float(p.get("tol", 0.1)))
    ax = p.get("axis", [0.0, 1.0, 0.0])
    rot = App.Rotation(App.Vector(float(ax[0]), float(ax[1]), float(ax[2])),
                       float(p["angle"]))
    rv = [rot.multVec(v) for v in verts]
    facets = [[rv[a], rv[b], rv[c]] for (a, b, c) in tris]
    m = Mesh.Mesh(facets)
    m.write(p["out_stl"])
    ok = os.path.exists(p["out_stl"]) and m.CountFacets > 0
    print(f"[facet] {'OK' if ok else 'FAIL'} facets={m.CountFacets}", flush=True)
