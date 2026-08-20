"""Поворот STEP в другую систему координат. Выполняется ВНУТРИ freecadcmd.

Нужен, чтобы посадить деталь/заготовку на физическую ось шпинделя токарного
станка NX (наш пайплайн канонизирует ось на Z, а у станка она вдоль другой
мировой оси). Только для BREP-солидов — фасетное тело NX через Part.Shape
рассыпается (0 граней), его крутить этим воркером нельзя.

Параметры — env ROTATE_PARAMS (JSON): in, out, axis [x,y,z], angle (град).
OCCT не пишет в неASCII-пути, поэтому out всегда ASCII (хост копирует на место).
"""
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if os.environ.get("ROTATE_PARAMS"):
    import FreeCAD as App
    import Part

    with open(os.environ["ROTATE_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)
    sh = Part.Shape()
    sh.read(p["in"])
    ax = p.get("axis", [0.0, 1.0, 0.0])
    sh.rotate(App.Vector(0.0, 0.0, 0.0),
              App.Vector(float(ax[0]), float(ax[1]), float(ax[2])),
              float(p["angle"]))
    sh.exportStep(p["out"])
    ok = os.path.exists(p["out"]) and os.path.getsize(p["out"]) > 0
    print(f"[rotate] {'OK' if ok else 'FAIL'} "
          f"solids={len(sh.Solids)} faces={len(sh.Faces)}", flush=True)
