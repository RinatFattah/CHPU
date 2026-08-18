#!/usr/bin/env python3
"""
freecad_shift_worker.py — исполняется ВНУТРИ FreeCAD (freecadcmd).

Переносит тело STEP на вектор (dx, dy, dz) — перевод геометрии из одной
системы координат в другую (например, наш ноль по верхней плоскости детали →
заводской ноль по нижней).

Сдвиг делается `shape.translate` (движение размещения), а НЕ
`transformGeometry`: последний пересобирает аналитические поверхности в
BSpline, и цилиндры с плоскостями перестают быть цилиндрами и плоскостями —
дальше по цепочке это ломает и сечения, и распознавание граней.

Параметры (env FREECAD_SHIFT_PARAMS, JSON):
  in_step, out_step: пути (ASCII)
  shift: [dx, dy, dz]
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


def log(msg):
    print(f"[shift] {msg}", flush=True)


def main():
    with open(os.environ["FREECAD_SHIFT_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)
    dx, dy, dz = p["shift"]
    shape = Part.Shape()
    shape.read(p["in_step"])
    b = shape.BoundBox
    log(f"до:    X {b.XMin:.3f}..{b.XMax:.3f}  Y {b.YMin:.3f}..{b.YMax:.3f}  "
        f"Z {b.ZMin:.3f}..{b.ZMax:.3f}  ({len(shape.Solids)} тел)")
    shape.translate(FreeCAD.Vector(dx, dy, dz))
    b = shape.BoundBox
    log(f"после: X {b.XMin:.3f}..{b.XMax:.3f}  Y {b.YMin:.3f}..{b.YMax:.3f}  "
        f"Z {b.ZMin:.3f}..{b.ZMax:.3f}")
    doc = FreeCAD.newDocument("Shift")
    feat = doc.addObject("Part::Feature", "Shifted")
    feat.Shape = shape
    doc.recompute()
    Part.export([feat], p["out_step"])
    log(f"OK step={p['out_step']}")


if os.environ.get("FREECAD_SHIFT_PARAMS"):
    try:
        main()
    except Exception:
        import traceback
        for _line in traceback.format_exc().splitlines():
            log(_line)
        raise
