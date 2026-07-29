#!/usr/bin/env python3
"""
lathe_gcode.py — осевой профиль детали → токарная управляющая программа.

Стратегия — продольное наружное точение, как у технолога:
  1. FaceOff   — подрезка торца заготовки до Z0;
  2. Rough1..N — черновая продольными проходами: на каждом уровне радиуса резец
     идёт от торца вглубь, пока профиль детали не поднимется выше этого уровня,
     затем отвод по X и возврат. Уровни идут от радиуса заготовки к детали с
     шагом DEPTH_OF_CUT, с припуском ALLOWANCE на чистовую;
  3. Finish    — чистовой проход ПО ПРОФИЛЮ (по всем точкам, к номиналу);
  4. Partoff   — отрезка (опционально).

Выход — явные траектории G0/G1 в плоскости G18, а НЕ контурный цикл стойки
(CYCLE95/G71): цикл переносит расчёт на стойку и даёт программу в 30 строк, но
требует поддержки конкретной стойкой и её симулятором. Явные движения исполнит
любая стойка и любой симулятор — для демонстрации это надёжнее. Переход на
CYCLE95 — замена одной функции: профиль уже извлечён, печатать его контуром.

Координаты: Z вдоль оси (0 — правый торец детали, обработка в −Z), X — РАДИУС
или ДИАМЕТР в зависимости от `diameter_mode` (стойки обычно ждут диаметр).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _r_at(profile, z):
    """Радиус детали на координате z (линейная интерполяция по профилю)."""
    pts = profile
    if z >= pts[0][0]:
        return pts[0][1]
    if z <= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        z1, r1 = pts[i]
        z2, r2 = pts[i + 1]
        if z2 <= z <= z1:
            if abs(z1 - z2) < 1e-9:
                return max(r1, r2)
            t = (z1 - z) / (z1 - z2)
            return r1 + t * (r2 - r1)
    return pts[-1][1]


def _max_r_between(profile, z_from, z_to):
    """Максимальный радиус детали на участке [z_to, z_from] (z убывает вглубь)."""
    rs = [_r_at(profile, z_from), _r_at(profile, z_to)]
    rs += [r for z, r in profile if z_to <= z <= z_from]
    return max(rs)


def generate(prof_data, params):
    """Профиль + параметры → (строки G-кода, статистика)."""
    profile = [(z, r) for z, r in prof_data["profile"]]
    profile.sort(key=lambda p: -p[0])            # от торца (Z max) вглубь

    r_stock = prof_data.get("stock_radius") or (prof_data["max_radius"] + 1.0)
    z_top = prof_data.get("stock_z_top", profile[0][0] + 1.0)
    z_end = profile[-1][0]

    ap = params["depth_of_cut"]
    allowance = params["allowance"]
    clear = params["clearance"]
    dia = params.get("diameter_mode", True)
    rpm = params["spindle_speed"]
    retract_r = r_stock + clear

    # ПОДАЧА НА ОБОРОТ (G95) — норма для точения: толщина стружки задаётся
    # миллиметрами на оборот шпинделя и не зависит от оборотов. При подаче в
    # мм/мин (G94) любое изменение оборотов меняет стружку, а с постоянной
    # скоростью резания обороты меняются на каждом диаметре.
    per_rev = params.get("feed_mode", "per_rev") == "per_rev"
    if per_rev:
        feed = params.get("feed_per_rev", 0.15)
        feed_finish = params.get("feed_per_rev_finish", 0.08)
        feed_partoff = params.get("feed_per_rev_partoff", feed / 2)
        fmt = lambda f: f"F{f:.3f}"
    else:
        feed = params["feed"]
        feed_finish = params.get("feed_finish", feed)
        feed_partoff = params.get("feed_partoff", feed / 2)
        fmt = lambda f: f"F{f:.1f}"

    def X(r):
        return f"X{2 * r:.3f}" if dia else f"X{r:.3f}"

    g = []
    ops = []

    def op(name):
        g.append(f"(Begin operation: {name})")
        ops.append(name)

    g.append(f"(Lathe part: L{prof_data['length']:.2f} x "
             f"Dmax{2 * prof_data['max_radius']:.2f} mm)")
    pick = prof_data.get("stock_pick")
    if pick and pick.get("kind") == "hex":
        g.append(f"(Stock: HEX bar S{pick['size']:g} [{pick['series']}], "
                 f"circumscribed D{pick['diameter']:.2f}, "
                 f"Z {z_top:.2f}..{z_end:.2f})")
    elif pick:
        g.append(f"(Stock: round bar D{pick['size']:g} [{pick['series']}], "
                 f"Z {z_top:.2f}..{z_end:.2f})")
    else:
        g.append(f"(Stock: bar D{2 * r_stock:.2f} mm, Z {z_top:.2f}..{z_end:.2f})")
    g.append(f"(Tool: turning insert {params.get('insert', 'DCMT070204R')}, "
             f"nose R{params.get('nose_radius', 0.4)} mm)")
    g.append(f"(Profile points: {len(profile)})")
    g.append(f"(X in {'DIAMETER' if dia else 'RADIUS'} mode, feed in "
             f"{'mm/rev G95' if per_rev else 'mm/min G94'})")
    g.append("G18 G21 G90")                       # плоскость ZX, мм, абсолютные
    g.append("G95" if per_rev else "G94")         # подача: мм/об или мм/мин
    g.append("T1 M6")
    g.append(f"G97 S{rpm} M3")                    # постоянные обороты
    g.append(f"G0 {X(retract_r)} Z{z_top + clear:.3f}")

    # 1. подрезка торца до Z0 (торец детали)
    op("FaceOff")
    z_face = profile[0][0]
    passes = max(1, int((z_top - z_face) / ap + 0.999))
    for i in range(passes):
        z = z_top - (i + 1) * (z_top - z_face) / passes
        g.append(f"G0 {X(r_stock + clear)} Z{z:.3f}")
        g.append(f"G1 X0.000 Z{z:.3f} {fmt(feed)}")
        g.append(f"G0 {X(r_stock + clear)} Z{z:.3f}")
    g.append(f"(Finish operation: FaceOff)")

    # 2. черновая продольными проходами
    n_rough = 0
    r_level = r_stock - ap
    r_min_target = max(min(r for _, r in profile), 0.0) + allowance
    while r_level > r_min_target:
        # докуда можно идти на этом радиусе: пока деталь ниже уровня
        z = profile[0][0]
        z_stop = z
        step = params["scan_step"]
        while z > z_end:
            z_next = max(z - step, z_end)
            if _max_r_between(profile, z, z_next) + allowance > r_level:
                break
            z_stop = z_next
            z = z_next
        if z_stop < profile[0][0] - 1e-6:
            n_rough += 1
            op(f"Rough{n_rough}")
            g.append(f"G0 {X(r_level)} Z{profile[0][0] + clear:.3f}")
            g.append(f"G1 {X(r_level)} Z{z_stop:.3f} {fmt(feed)}")
            g.append(f"G0 {X(r_level + clear)} Z{z_stop:.3f}")
            g.append(f"G0 {X(r_level + clear)} Z{profile[0][0] + clear:.3f}")
            g.append(f"(Finish operation: Rough{n_rough})")
        r_level -= ap

    # 3. чистовой проход по профилю
    op("Finish")
    g.append(f"G0 {X(retract_r)} Z{profile[0][0] + clear:.3f}")
    g.append(f"G0 {X(profile[0][1])} Z{profile[0][0] + clear:.3f}")
    for z, r in profile:
        g.append(f"G1 {X(r)} Z{z:.3f} {fmt(feed_finish)}")
    g.append(f"G0 {X(retract_r)} Z{z_end:.3f}")
    g.append("(Finish operation: Finish)")

    # 4. отрезка
    if params.get("partoff", True):
        op("Partoff")
        z_cut = z_end - params.get("partoff_width", 3.0)
        g.append(f"G0 {X(retract_r)} Z{z_cut:.3f}")
        g.append(f"G1 X0.000 Z{z_cut:.3f} {fmt(feed_partoff)}")
        g.append(f"G0 {X(retract_r)} Z{z_cut:.3f}")
        g.append("(Finish operation: Partoff)")

    g.append(f"G0 {X(retract_r)} Z{z_top + clear + 10:.3f}")
    g.append("M5")
    g.append("M2")

    stats = {"lines": len(g), "ops": ops, "rough_passes": n_rough,
             "stock_radius": r_stock, "profile_points": len(profile)}
    return g, stats


def write(prof_data, params, path):
    g, stats = generate(prof_data, params)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(g) + "\n")
    return stats
