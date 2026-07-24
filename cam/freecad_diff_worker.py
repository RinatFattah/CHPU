#!/usr/bin/env python3
"""
freecad_diff_worker.py — исполняется ВНУТРИ FreeCAD (freecadcmd), не в обычном Python.

Сравнение «деталь vs результат симуляции» (оба в координатах ПРОГРАММЫ) методом
ВОКСЕЛЬНОГО РЕЙ-КАСТИНГА, без булевых операций и без isInside:
  - OCCT-булевы с фасетным телом из NX ненадёжны (молча не вычитают/Null shape,
    от детали к детали по-разному);
  - isInside на фасетном теле — миллисекунды на пробу, сетка не тянется.
Оба тела тесселируются один раз; для каждой XY-колонки сетки numpy-вектором
считаются пересечения вертикального луча с треугольниками (чётность = внутри).
Чётности не важна ориентация тела — «вывернутые» IPW из NX не мешают.

Классификация ячейки:
  в результате, но не в детали → НЕДОРЕЗ (ниже пола — floor_skin, намеренная
                                  плёнка от стола);
  в детали, но не в результате → ЗАРЕЗ.
Ячейки собираются в связные зоны (6-соседность), объёмы = число ячеек × pitch³.

Параметры (env FREECAD_DIFF_PARAMS, JSON): part, result, json_path,
floor_clearance, min_volume (мм³), pitch (мм, 0 = автоматически).
"""

import json
import math
import os
import sys

for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

import numpy as np

import Part


def log(msg):
    print(f"[diff] {msg}", flush=True)


def load_solid(path):
    shape = Part.Shape()
    shape.read(path)
    solids = shape.Solids or [Part.makeSolid(shape)]
    return max(solids, key=lambda x: abs(x.Volume))


def tri_arrays(solid, tol=0.1):
    """Тесселяция → массивы вершин треугольников A, B, C формы (m, 3)."""
    verts, tris = solid.tessellate(tol)
    V = np.array([(v.x, v.y, v.z) for v in verts], dtype=float)
    T = np.array(tris, dtype=int).reshape(-1, 3)
    return V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]


class ZCaster:
    """Вертикальный рей-кастинг: для колонки (x, y) — отсортированные Z
    пересечений луча с мешем; чётность количества ниже точки = «внутри»."""

    def __init__(self, solid, tol=0.1):
        A, B, C = tri_arrays(solid, tol)
        self.ax, self.ay, self.az = A[:, 0], A[:, 1], A[:, 2]
        self.bz, self.cz = B[:, 2], C[:, 2]
        # предвычисленные коэффициенты барицентрических координат в плоскости XY
        self.d = ((B[:, 1] - C[:, 1]) * (A[:, 0] - C[:, 0])
                  + (C[:, 0] - B[:, 0]) * (A[:, 1] - C[:, 1]))
        self.ok = np.abs(self.d) > 1e-12          # вертикальные грани — мимо
        self.dn = np.where(self.ok, self.d, 1.0)
        self.byc, self.cbx = B[:, 1] - C[:, 1], C[:, 0] - B[:, 0]
        self.cya, self.acx = C[:, 1] - A[:, 1], A[:, 0] - C[:, 0]
        self.cx, self.cy = C[:, 0], C[:, 1]
        # bbox треугольников по XY — быстрый отсев
        xs = np.stack([A[:, 0], B[:, 0], C[:, 0]])
        ys = np.stack([A[:, 1], B[:, 1], C[:, 1]])
        self.xmin, self.xmax = xs.min(0), xs.max(0)
        self.ymin, self.ymax = ys.min(0), ys.max(0)

    def crossings(self, x, y):
        m = (self.ok & (self.xmin <= x) & (x <= self.xmax)
             & (self.ymin <= y) & (y <= self.ymax))
        if not m.any():
            return np.empty(0)
        u = (self.byc[m] * (x - self.cx[m]) + self.cbx[m] * (y - self.cy[m])) / self.dn[m]
        v = (self.cya[m] * (x - self.cx[m]) + self.acx[m] * (y - self.cy[m])) / self.dn[m]
        w = 1.0 - u - v
        hit = (u >= -1e-9) & (v >= -1e-9) & (w >= -1e-9)
        if not hit.any():
            return np.empty(0)
        z = self.az[m][hit] * u[hit] + self.bz[m][hit] * v[hit] + self.cz[m][hit] * w[hit]
        z = np.sort(z)
        keep = np.ones(z.size, bool)         # дубли на общих рёбрах треугольников
        keep[1:] = np.diff(z) > 1e-6
        z = z[keep]
        if z.size % 2:                       # вырожденная колонка (касание кромки)
            z = z[:-1]
        return z

    def inside(self, x, y, zc):
        """zc — массив Z-центров ячеек; True = точка внутри тела."""
        zs = self.crossings(x, y)
        if zs.size == 0:
            return np.zeros(zc.size, bool)
        return (np.searchsorted(zs, zc) % 2) == 1


def clusters(cells):
    """Связные компоненты множества ячеек (ix,iy,iz), 6-соседность."""
    todo = set(cells)
    out = []
    while todo:
        seed = todo.pop()
        comp, stack = [seed], [seed]
        while stack:
            x, y, z = stack.pop()
            for n in ((x - 1, y, z), (x + 1, y, z), (x, y - 1, z),
                      (x, y + 1, z), (x, y, z - 1), (x, y, z + 1)):
                if n in todo:
                    todo.remove(n)
                    stack.append(n)
                    comp.append(n)
        out.append(comp)
    return out


def zone_entry(comp, origin, pitch, cell_vol):
    xs = [c[0] for c in comp]
    ys = [c[1] for c in comp]
    zs = [c[2] for c in comp]
    ox, oy, oz = origin

    def lo(v, o):
        return round(o + min(v) * pitch, 1)

    def hi(v, o):
        return round(o + (max(v) + 1) * pitch, 1)

    return {
        "volume_mm3": round(len(comp) * cell_vol, 1),
        "center": [round(ox + (sum(xs) / len(xs) + 0.5) * pitch, 1),
                   round(oy + (sum(ys) / len(ys) + 0.5) * pitch, 1),
                   round(oz + (sum(zs) / len(zs) + 0.5) * pitch, 1)],
        "size": [round(hi(xs, ox) - lo(xs, ox), 1),
                 round(hi(ys, oy) - lo(ys, oy), 1),
                 round(hi(zs, oz) - lo(zs, oz), 1)],
        "bbox": {"x": [lo(xs, ox), hi(xs, ox)],
                 "y": [lo(ys, oy), hi(ys, oy)],
                 "z": [lo(zs, oz), hi(zs, oz)]},
    }


def main():
    with open(os.environ["FREECAD_DIFF_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)
    min_vol = float(p.get("min_volume", 2.0))
    clearance = float(p.get("floor_clearance", 0.5))

    part = load_solid(p["part"])
    result = load_solid(p["result"])
    pb = part.BoundBox
    floor_z = pb.ZMin + clearance
    log(f"деталь {abs(part.Volume) / 1000.0:.1f} см³, результат "
        f"{abs(result.Volume) / 1000.0:.1f} см³")

    cast_p = ZCaster(part, 0.05)
    cast_r = ZCaster(result, 0.1)

    # шаг сетки: ~110 ячеек по большой стороне, в пределах 0.5..1.5 мм
    pitch = float(p.get("pitch", 0)) or min(
        1.5, max(0.5, max(pb.XLength, pb.YLength, pb.ZLength) / 110.0))
    nx = max(1, int(math.ceil(pb.XLength / pitch)))
    ny = max(1, int(math.ceil(pb.YLength / pitch)))
    nz = max(1, int(math.ceil(pb.ZLength / pitch)))
    origin = (pb.XMin, pb.YMin, pb.ZMin)
    cell_vol = pitch ** 3
    log(f"сетка {nx}x{ny}x{nz} (шаг {pitch:.2f} мм, {nx * ny * nz} ячеек)")

    zc = pb.ZMin + (np.arange(nz) + 0.5) * pitch
    iz_all = np.arange(nz)
    floor_mask = zc <= floor_z
    under_cells, over_cells, floor_cells = set(), set(), 0
    # микросдвиг колонок от узлов сетки — меньше попаданий луча точно в кромку
    jx, jy = 0.5 + 1.7e-3, 0.5 + 2.3e-3
    for iy in range(ny):
        y = pb.YMin + (iy + jy) * pitch
        for ix in range(nx):
            x = pb.XMin + (ix + jx) * pitch
            in_p = cast_p.inside(x, y, zc)
            in_r = cast_r.inside(x, y, zc)
            under = in_r & ~in_p
            over = in_p & ~in_r
            if under.any():
                fl = under & floor_mask
                floor_cells += int(fl.sum())
                for iz in iz_all[under & ~floor_mask]:
                    under_cells.add((ix, iy, int(iz)))
            if over.any():
                for iz in iz_all[over]:
                    over_cells.add((ix, iy, int(iz)))

    undercuts, overcuts = [], []
    for comp in clusters(under_cells):
        if len(comp) * cell_vol >= min_vol:
            undercuts.append(zone_entry(comp, origin, pitch, cell_vol))
    for comp in clusters(over_cells):
        if len(comp) * cell_vol >= min_vol:
            overcuts.append(zone_entry(comp, origin, pitch, cell_vol))
    undercuts.sort(key=lambda z: -z["volume_mm3"])
    overcuts.sort(key=lambda z: -z["volume_mm3"])
    floor_skin = floor_cells * cell_vol

    data = {
        "method": f"voxel ray-casting, шаг {pitch:.2f} мм (объёмы ± ячейка)",
        "part_volume_mm3": round(abs(part.Volume), 1),
        "floor_clearance_mm": clearance,
        "floor_skin_mm3": round(floor_skin, 1),
        "undercut_total_mm3": round(sum(z["volume_mm3"] for z in undercuts), 1),
        "overcut_total_mm3": round(sum(z["volume_mm3"] for z in overcuts), 1),
        "undercuts": undercuts[:15],
        "overcuts": overcuts[:15],
        "note": "недорез — материал, оставшийся в границах детали сверх модели; "
                "зарез — снятое, что должно было остаться. floor_skin — намеренная "
                "плёнка у дна (зазор от стола), НЕ дефект. Рамка заготовки вне "
                "габарита детали не учитывается.",
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
