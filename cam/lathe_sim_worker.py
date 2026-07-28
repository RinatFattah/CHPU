#!/usr/bin/env python3
"""
lathe_sim_worker.py — съём материала по токарной программе (внутри FreeCAD).

Для точения съём считается ТОЧНО, без вокселей и фасеточных тел: результат
обточки — всегда тело вращения, поэтому достаточно проследить огибающую
траектории резца. Для каждой координаты z берём минимальный радиус, до
которого доходила режущая кромка на рабочей подаче (G1); где резец не был,
остаётся радиус заготовки. Полученный профиль вращаем вокруг оси — это и есть
обработанная деталь.

Ограничения модели: резец считается точкой (радиус при вершине не учитывается),
G0 не режет, отрезка даёт нулевой радиус в одной координате, а не паз шириной
с пластину. Для проверки геометрии программы этого достаточно; проверку
исполнимости на стойке даёт симуляция NX ISV.
"""

import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import FreeCAD as App
import Part


def log(msg):
    print(f"[sim] {msg}")


def parse_moves(path, diameter_mode):
    """G-код → список рабочих движений [(z1, r1, z2, r2)] в РАДИУСАХ."""
    moves = []
    x = z = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = re.sub(r"\(.*?\)", "", line).strip()
            if not line:
                continue
            m = re.match(r"G0*([0123])\b", line)
            if not m:
                continue
            code = int(m.group(1))
            nx, nz = x, z
            mx = re.search(r"X(-?\d+\.?\d*)", line)
            mz = re.search(r"Z(-?\d+\.?\d*)", line)
            if mx:
                nx = float(mx.group(1)) / (2.0 if diameter_mode else 1.0)
            if mz:
                nz = float(mz.group(1))
            if code >= 1 and x is not None and z is not None \
                    and nx is not None and nz is not None:
                moves.append((z, x, nz, nx))
            x, z = nx, nz
    return moves


def envelope(moves, z_lo, z_hi, step, r_stock):
    """Минимальный радиус, достигнутый резцом, по сетке z."""
    n = int(round((z_hi - z_lo) / step)) + 1
    grid = [z_lo + i * step for i in range(n)]
    reached = [r_stock] * n

    for z1, r1, z2, r2 in moves:
        lo, hi = min(z1, z2), max(z1, z2)
        i0 = max(0, int((lo - z_lo) / step))
        i1 = min(n - 1, int((hi - z_lo) / step) + 1)
        for i in range(i0, i1 + 1):
            zz = grid[i]
            if zz < lo - 1e-9 or zz > hi + 1e-9:
                continue
            if abs(z2 - z1) < 1e-9:
                r = min(r1, r2)                 # врезание по X на месте
            else:
                t = (zz - z1) / (z2 - z1)
                r = r1 + t * (r2 - r1)
            if r < reached[i]:
                reached[i] = r
    return grid, reached


def main():
    with open(os.environ["FREECAD_LATHE_SIM_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)

    r_stock = p["stock_radius"]
    z_hi = p["stock_z_top"]
    z_lo = p["stock_z_bottom"]
    step = p.get("step", 0.05)

    moves = parse_moves(p["gcode"], p.get("diameter_mode", True))
    log(f"рабочих движений (G1/G2/G3): {len(moves)}")

    grid, reached = envelope(moves, z_lo, z_hi, step, r_stock)
    cut = sum(1 for r in reached if r < r_stock - 1e-6)
    log(f"сетка {len(grid)} точек шагом {step} мм, резец коснулся {cut} из них")

    # Профиль результата → замкнутый контур в плоскости XZ → вращение вокруг Z.
    # Радиус зажимаем снизу: контур, КАСАЮЩИЙСЯ оси (после отрезки резец доходит
    # до X0), при вращении даёт самопересечение и невалидное тело.
    r_eps = p.get("min_radius", 0.05)
    pts = [App.Vector(max(r, r_eps), 0, z) for z, r in zip(grid, reached)]
    clean = [pts[0]]
    for v in pts[1:]:
        if (v - clean[-1]).Length > 1e-7:
            clean.append(v)
    if len(clean) < 2:
        raise RuntimeError("профиль результата вырожден")

    # обход: профиль снизу вверх → к оси → вниз по оси → замыкание
    outline = clean
    wire = Part.makePolygon(outline)
    axis_back = Part.makePolygon([outline[-1], App.Vector(r_eps, 0, outline[0].z),
                                  outline[0]])
    try:
        face = Part.Face(Part.Wire(wire.Edges + axis_back.Edges))
    except Exception as e:
        raise RuntimeError(f"контур результата не замкнулся в грань: {e}")
    solid = face.revolve(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 360)
    try:
        solid = solid.removeSplitter()
    except Exception:
        pass
    if not solid.isValid():
        solid = solid.removeSplitter()

    bb = solid.BoundBox
    log(f"результат: Ø{max(bb.XLength, bb.YLength):.2f} x {bb.ZLength:.2f} мм, "
        f"объём {solid.Volume:.1f} мм³")

    solid.exportStep(p["out_step"])
    log(f"OK volume={solid.Volume:.1f} step={p['out_step']}")


if os.environ.get("FREECAD_LATHE_SIM_PARAMS"):
    main()
