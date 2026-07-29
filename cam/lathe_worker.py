#!/usr/bin/env python3
"""
lathe_worker.py — исполняется ВНУТРИ FreeCAD (freecadcmd): STEP → осевой профиль.

Токарная деталь целиком описывается профилем R(z) в плоскости ZX, поэтому вся
трёхмерная машинерия фрезерного worker'а (силуэты, зоны, достижимость) здесь не
нужна. Порядок работы:

  1. читаем тело, ищем ОСЬ ВРАЩЕНИЯ — по цилиндрическим/коническим/тороидальным
     граням: группируем по направлению оси, берём доминирующую по площади;
  2. поворачиваем деталь так, чтобы ось совпала с Z, а центр оси — с X0Y0;
  3. секущая плоскость через ось (нормаль Y) → плоское осевое сечение, из него
     берём половину X ≥ 0;
  4. профиль снимаем ЧИСЛЕННО: дискретизируем рёбра сечения и для сетки z берём
     максимальный радиус. Это устойчивее разбора топологии — фаски, скругления и
     сплайны одинаково превращаются в точки, а лишние вершины убирает упрощение
     по коридору (Douglas–Peucker).

Вывод — JSON: профиль [[z, r], ...], габариты, ось. Печатает маркеры [worker].
Запускается так же, как freecad_worker.py: параметры через env FREECAD_LATHE_PARAMS.
"""

import json
import os
import sys

# stdout внутри freecadcmd — cp1251; кириллица и Ø иначе роняют worker
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import FreeCAD as App
import Part


def log(msg):
    print(f"[worker] {msg}")


def find_rotation_axis(shape):
    """Ось вращения детали: доминирующее по площади направление осей
    цилиндров/конусов/торов. Возвращает (направление, точка на оси)."""
    groups = {}   # округлённое направление -> [площадь, [точки на оси]]
    for f in shape.Faces:
        s = f.Surface
        axis = point = None
        if hasattr(s, "Axis") and hasattr(s, "Center"):
            axis, point = s.Axis, s.Center
        elif hasattr(s, "Axis") and hasattr(s, "Apex"):        # конус
            axis, point = s.Axis, s.Apex
        if axis is None:
            continue
        d = App.Vector(axis)
        d.normalize()
        if d.z < 0 or (abs(d.z) < 1e-9 and (d.y < 0 or (abs(d.y) < 1e-9 and d.x < 0))):
            d = d.multiply(-1)                                  # ось без знака
        key = (round(d.x, 4), round(d.y, 4), round(d.z, 4))
        g = groups.setdefault(key, [0.0, []])
        g[0] += f.Area
        g[1].append(point)
    if not groups:
        raise RuntimeError("в детали нет цилиндров/конусов — это не тело вращения")
    key, (area, points) = max(groups.items(), key=lambda kv: kv[1][0])
    total = sum(v[0] for v in groups.values())
    log(f"ось вращения: {key}, по ней {area:.0f} из {total:.0f} мм² "
        f"криволинейных граней ({100 * area / total:.0f} %)")
    c = App.Vector(0, 0, 0)
    for p in points:
        c += App.Vector(p)
    c = c.multiply(1.0 / len(points))
    return App.Vector(*key), c


def find_wrench_flats(shape, tol=0.05):
    """Грани «под ключ»: шесть плоскостей с нормалями через 60° вокруг оси Z,
    равноудалённых от неё. Возвращает размер S (между противоположными
    гранями) или None. Деталь ищется УЖЕ выровненной осью на Z.

    Признак нужен для выбора проката: если грани совпадают с размером
    шестигранного проката, точить их не надо — они достаются от заготовки.
    """
    import math
    flats = []
    for f in shape.Faces:
        if type(f.Surface).__name__ != "Plane":
            continue
        n = f.Surface.Axis
        if abs(n.z) > 0.1:                       # не боковая — торец
            continue
        c = f.CenterOfMass
        dist = abs(n.x * c.x + n.y * c.y)        # расстояние от оси до грани
        ang = math.degrees(math.atan2(n.y, n.x)) % 60.0
        flats.append((dist, ang, round(f.BoundBox.ZMin, 1),
                      round(f.BoundBox.ZMax, 1)))
    if len(flats) < 6:
        return None

    # группируем по (расстояние от оси, диапазон Z) — один пояс граней
    groups = {}
    for dist, ang, z0, z1 in flats:
        key = (round(dist / max(tol, 1e-6)), z0, z1)
        groups.setdefault(key, []).append((dist, ang))
    for (_, z0, z1), items in groups.items():
        if len(items) != 6:
            continue
        dists = [d for d, _ in items]
        if max(dists) - min(dists) > tol:
            continue
        # нормали должны стоять через 60°, то есть по модулю 60 совпадать
        angs = [a for _, a in items]
        spread = max(angs) - min(angs)
        if spread > 2.0 and spread < 58.0:
            continue
        s = 2.0 * (sum(dists) / len(dists))
        log(f"грани под ключ: 6 плоскостей, S={s:.2f} мм, Z {z1:.1f}..{z0:.1f}")
        return round(s, 3)
    return None


def align_to_z(shape, axis, centre):
    """Ставит деталь осью вдоль Z, центром оси в X0Y0, правым торцом в Z0
    (обработка идёт в −Z, как принято в токарных программах)."""
    import math
    z = App.Vector(0, 0, 1)
    s = shape.copy()
    rot = App.Rotation()
    if (axis - z).Length > 1e-9:
        rax = axis.cross(z)
        if rax.Length < 1e-9:                       # ось антипараллельна Z
            rax = App.Vector(1, 0, 0)
        angle = math.degrees(axis.getAngle(z))
        rot = App.Rotation(rax, angle)
        s.rotate(App.Vector(0, 0, 0), rax, angle)   # вращаем вокруг начала
    c = rot.multVec(App.Vector(centre))             # ось после поворота
    bb = s.BoundBox
    s.translate(App.Vector(-c.x, -c.y, -bb.ZMax))   # ось → Z, правый торец → Z0
    return s


def profile_from_section(shape, step, simplify_tol):
    """Осевое сечение → профиль [(z, r)] численно: max радиус на каждом z."""
    bb = shape.BoundBox
    L = max(bb.XLength, bb.YLength, bb.ZLength) * 4 + 10
    plane = Part.makePlane(L, L, App.Vector(-L / 2, 0, bb.ZMin - 5),
                           App.Vector(0, 1, 0))       # плоскость ZX через ось
    sec = shape.common(plane)
    if not sec.Faces:
        raise RuntimeError("осевое сечение пустое — ось найдена неверно")
    log(f"осевое сечение: {len(sec.Faces)} граней, {len(sec.Edges)} рёбер, "
        f"площадь {sec.Area:.1f} мм²")

    # облако точек контура; берём половину X >= 0 (вторая — зеркало)
    pts = []
    for e in sec.Edges:
        n = max(2, int(e.Length / 0.05))
        for p in e.discretize(Number=min(n, 4000)):
            if p.x >= -1e-6:
                pts.append((p.z, abs(p.x)))
    if not pts:
        raise RuntimeError("в сечении нет точек с X >= 0")

    zmin = min(p[0] for p in pts)
    zmax = max(p[0] for p in pts)
    n = max(2, int(round((zmax - zmin) / step)) + 1)
    grid = [zmax - i * (zmax - zmin) / (n - 1) for i in range(n)]  # от торца вглубь
    prof = []
    for z in grid:
        near = [r for (pz, r) in pts if abs(pz - z) <= step * 0.75]
        if near:
            prof.append((z, max(near)))
    if len(prof) < 2:
        raise RuntimeError("профиль вырожден")

    log(f"профиль снят: {len(prof)} точек, шаг {step} мм, "
        f"Ø{2 * max(r for _, r in prof):.2f} макс")
    # СЫРОЙ профиль отдаётся вместе с упрощённым: упрощение спрямляет
    # скругления в ломаную, и восстановить по ней дуги уже нельзя — их надо
    # искать до потери точек (см. lathe_gcode.fit_arcs)
    return simplify(prof, simplify_tol), prof


def simplify(pts, tol):
    """Douglas–Peucker: выкидывает точки, лежащие в коридоре tol от хорды."""
    if len(pts) < 3:
        return pts
    z0, r0 = pts[0]
    z1, r1 = pts[-1]
    dz, dr = z1 - z0, r1 - r0
    norm = (dz * dz + dr * dr) ** 0.5 or 1.0
    imax, dmax = 0, 0.0
    for i in range(1, len(pts) - 1):
        z, r = pts[i]
        d = abs(dr * (z - z0) - dz * (r - r0)) / norm
        if d > dmax:
            imax, dmax = i, d
    if dmax <= tol:
        return [pts[0], pts[-1]]
    return simplify(pts[:imax + 1], tol)[:-1] + simplify(pts[imax:], tol)


def main():
    with open(os.environ["FREECAD_LATHE_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)

    shape = Part.Shape()
    shape.read(p["model_path"])
    if not shape.Solids:
        raise RuntimeError("в файле нет твёрдого тела")
    solid = shape.Solids[0] if len(shape.Solids) == 1 else shape
    log(f"деталь загружена: {solid.BoundBox.XLength:.2f} x "
        f"{solid.BoundBox.YLength:.2f} x {solid.BoundBox.ZLength:.2f} мм")

    axis, centre = find_rotation_axis(solid)
    aligned = align_to_z(solid, axis, centre)
    bb = aligned.BoundBox
    log(f"после выравнивания по Z: Ø{max(bb.XLength, bb.YLength):.2f} x "
        f"{bb.ZLength:.2f} мм, Z {bb.ZMin:.2f}..{bb.ZMax:.2f}")

    prof, prof_raw = profile_from_section(aligned, p.get("profile_step", 0.1),
                                          p.get("simplify_tol", 0.01))
    log(f"после упрощения: {len(prof)} точек (сырых {len(prof_raw)})")

    hex_s = find_wrench_flats(aligned)

    out = {
        "profile": [[round(z, 4), round(r, 4)] for z, r in prof],
        "length": round(bb.ZLength, 4),
        "max_radius": round(max(r for _, r in prof), 4),
        "axis": [round(axis.x, 6), round(axis.y, 6), round(axis.z, 6)],
        "z_range": [round(bb.ZMin, 4), round(bb.ZMax, 4)],
        "hex_across_flats": hex_s,
        "profile_raw": [[round(z, 4), round(r, 4)] for z, r in prof_raw],
    }
    with open(p["out_json"], "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # деталь в координатах программы — эталон для сравнения с результатом
    if p.get("part_out"):
        aligned.exportStep(p["part_out"])
        log(f"деталь в СК программы → {os.path.basename(p['part_out'])}")

    # заготовка-пруток из СОРТАМЕНТА (ГОСТ 2590 / 8560), см. lathe/stock.py
    if p.get("stock_out"):
        import math
        pick = None
        if p.get("repo_root"):
            # lathe.stock — чистый python без зависимостей, импортируется и
            # внутри FreeCAD; путь передаёт хост (корень может быть кириллицей)
            if p["repo_root"] not in sys.path:
                sys.path.insert(0, p["repo_root"])
            try:
                from lathe import stock as stock_std
                pick = stock_std.pick(2 * out["max_radius"],
                                      p.get("allowance_per_side", 3.15),
                                      hex_across_flats=hex_s,
                                      prefer_hex=p.get("prefer_hex", True))
                log(f"прокат: {stock_std.describe(pick)} — {pick['note']}")
            except Exception as e:
                log(f"warn: подбор сортамента не удался ({e}), "
                    f"беру деталь + припуск")
        if pick is None:                          # прежнее поведение
            d = 2 * (out["max_radius"] + p.get("stock_radial", 1.0))
            pick = {"kind": "round", "size": round(d, 3), "diameter": d,
                    "series": "без сортамента", "note": "деталь + припуск"}

        z0 = bb.ZMax + p.get("stock_face", 1.0)
        height = bb.ZLength + p.get("stock_face", 1.0) + p.get("stock_tail", 5.0)
        r_out = pick["diameter"] / 2.0
        if pick["kind"] == "hex":
            # шестигранный прокат: грани детали достаются от заготовки
            s = pick["size"]
            rc = s / math.sqrt(3.0)               # радиус описанной окружности
            pts = [App.Vector(rc * math.cos(math.radians(60 * i)),
                              rc * math.sin(math.radians(60 * i)), z0)
                   for i in range(6)]
            wire = Part.makePolygon(pts + [pts[0]])
            stock = Part.Face(wire).extrude(App.Vector(0, 0, -height))
        else:
            stock = Part.makeCylinder(r_out, height, App.Vector(0, 0, z0),
                                      App.Vector(0, 0, -1))
        stock.exportStep(p["stock_out"])
        log(f"заготовка: {pick['kind']} {pick['size']:g} × "
            f"{height:.2f} мм → {os.path.basename(p['stock_out'])}")
        out["stock_radius"] = round(r_out, 4)
        out["stock_z_top"] = round(z0, 4)
        out["stock_z_bottom"] = round(stock.BoundBox.ZMin, 4)
        out["stock_pick"] = pick
        with open(p["out_json"], "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)

    log(f"OK points={len(prof)} json={p['out_json']}")


if os.environ.get("FREECAD_LATHE_PARAMS"):
    main()
