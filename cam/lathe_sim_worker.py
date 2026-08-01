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


def parse_moves(path, diameter_mode, arc_step=0.05):
    """G-код → рабочие движения [(z1, r1, z2, r2)] в РАДИУСАХ, наружные и внутренние.

    Дуги G2/G3 разбиваются на хорды длиной ~arc_step: иначе съём считался бы
    по хорде во всю дугу, и выигрыш от дуг в чистовом проходе был бы не виден.

    ВНУТРЕННИЕ операции (Drill*, Bore*) собираются отдельно: они не обтачивают
    снаружи, а выбирают осевое отверстие, и в результате их надо ВЫЧЕСТЬ.
    Сверло к тому же режет своим диаметром, а не точкой, поэтому его диаметр
    читается из шапки операции — `(Tool T4: twist drill D10.00 mm)`.

    Возвращает (moves, inner, d_drill).
    """
    import math
    moves, inner = [], []
    d_drill = 0.0
    op = ""
    x = z = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            mo = re.search(r"\(Begin operation: (\S+?)\)", line)
            if mo:
                op = mo.group(1)
            md = re.search(r"twist drill D([\d.]+)", line)
            if md:
                d_drill = float(md.group(1))
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
            if code >= 1 and None not in (x, z, nx, nz):
                mi = re.search(r"I(-?\d+\.?\d*)", line)
                mk = re.search(r"K(-?\d+\.?\d*)", line)
                if code in (2, 3) and mi and mk:
                    # I/K — смещение центра от НАЧАЛЬНОЙ точки, I в радиусах
                    xc, zc = x + float(mi.group(1)), z + float(mk.group(1))
                    rad = math.hypot(x - xc, z - zc)
                    a0 = math.atan2(x - xc, z - zc)
                    a1 = math.atan2(nx - xc, nz - zc)
                    da = a1 - a0
                    if code == 2:                      # по часовой
                        while da > 0:
                            da -= 2 * math.pi
                    else:
                        while da < 0:
                            da += 2 * math.pi
                    steps = max(2, int(abs(da) * rad / arc_step) + 1)
                    pz, px = z, x
                    for s in range(1, steps + 1):
                        a = a0 + da * s / steps
                        cz = zc + rad * math.cos(a)
                        cx = xc + rad * math.sin(a)
                        (inner if op.startswith(("Drill", "Bore"))
                         else moves).append((pz, px, cz, cx))
                        pz, px = cz, cx
                else:
                    (inner if op.startswith(("Drill", "Bore"))
                     else moves).append((z, x, nz, nx))
            x, z = nx, nz
    return moves, inner, d_drill


def inner_envelope(moves, grid, d_drill):
    """Максимальный радиус, выбранный ВНУТРЕННИМИ операциями, по сетке z.

    Сверло режет своим диаметром, а не точкой: движение по оси (X≈0) снимает
    радиус d_drill/2. Расточной идёт кромкой, для него берётся сам радиус.
    """
    reached = [0.0] * len(grid)
    for z1, r1, z2, r2 in moves:
        lo, hi = (z1, z2) if z1 <= z2 else (z2, z1)
        for i, z in enumerate(grid):
            if lo - 1e-9 <= z <= hi + 1e-9:
                t = 0.0 if abs(z2 - z1) < 1e-12 else (z - z1) / (z2 - z1)
                r = r1 + (r2 - r1) * t
                if r < 1e-6:                 # сверло: режет диаметром
                    r = d_drill / 2.0
                reached[i] = max(reached[i], r)
    return reached


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

    moves, inner, d_drill = parse_moves(p["gcode"], p.get("diameter_mode", True))
    log(f"рабочих движений (G1/G2/G3): наружных {len(moves)}, "
        f"внутренних {len(inner)}" + (f", сверло Ø{d_drill:g}" if d_drill else ""))

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

    # ВЫЧЕСТЬ осевое отверстие: сверление и растачивание убирают материал
    # изнутри, наружная огибающая о них ничего не знает
    if inner:
        r_in = inner_envelope(inner, grid, d_drill)
        seg = [(z, r) for z, r in zip(grid, r_in) if r > r_eps]
        if len(seg) >= 2:
            ipts = [App.Vector(max(r, r_eps), 0, z) for z, r in seg]
            iclean = [ipts[0]]
            for v in ipts[1:]:
                if (v - iclean[-1]).Length > 1e-7:
                    iclean.append(v)
            iw = Part.makePolygon(iclean)
            iback = Part.makePolygon([iclean[-1],
                                      App.Vector(r_eps, 0, iclean[-1].z),
                                      App.Vector(r_eps, 0, iclean[0].z),
                                      iclean[0]])
            try:
                iface = Part.Face(Part.Wire(iw.Edges + iback.Edges))
                bore_solid = iface.revolve(App.Vector(0, 0, 0),
                                           App.Vector(0, 0, 1), 360)
                solid = solid.cut(bore_solid)
                log(f"осевое отверстие вычтено: Ø{2 * max(r for _, r in seg):.2f} "
                    f"x {seg[0][0] - seg[-1][0]:.2f} мм, "
                    f"объём стал {solid.Volume:.1f} мм³")
            except Exception as e:
                log(f"warn: отверстие не вычлось: {e}")

    solid.exportStep(p["out_step"])
    log(f"OK volume={solid.Volume:.1f} step={p['out_step']}")


if os.environ.get("FREECAD_LATHE_SIM_PARAMS"):
    main()
