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


def _circle_through(p1, p2, p3):
    """Окружность через три точки (z, r) → (zc, rc, radius) или None."""
    (z1, r1), (z2, r2), (z3, r3) = p1, p2, p3
    d = 2.0 * (z1 * (r2 - r3) + z2 * (r3 - r1) + z3 * (r1 - r2))
    if abs(d) < 1e-9:                       # точки на одной прямой
        return None
    s1, s2, s3 = z1 * z1 + r1 * r1, z2 * z2 + r2 * r2, z3 * z3 + r3 * r3
    zc = (s1 * (r2 - r3) + s2 * (r3 - r1) + s3 * (r1 - r2)) / d
    rc = (s1 * (z3 - z2) + s2 * (z1 - z3) + s3 * (z2 - z1)) / d
    rad = ((z1 - zc) ** 2 + (r1 - rc) ** 2) ** 0.5
    return zc, rc, rad


def _arc_dir(z0, r0, z1, r1, zm, rm, zc, rc):
    """Направление обхода дуги ПО СРЕДНЕЙ точке участка.

    Знак векторного произведения (начало→центр)×(начало→конец) сам по себе не
    годится: он говорит лишь, с какой стороны центр, но не по какой из двух
    дуг идти. Через среднюю точку профиля выбирается та дуга, что реально
    описывает деталь, — иначе резец пойдёт длинной стороной окружности и
    оставит материал.

    Возвращает True для G2 (по часовой в плоскости ZX).
    """
    import math
    a0 = math.atan2(r0 - rc, z0 - zc)
    a1 = math.atan2(r1 - rc, z1 - zc)
    am = math.atan2(rm - rc, zm - zc)

    def norm(a):
        while a < 0:
            a += 2 * math.pi
        while a >= 2 * math.pi:
            a -= 2 * math.pi
        return a

    # идём против часовой: лежит ли середина между началом и концом?
    d_end = norm(a1 - a0)
    d_mid = norm(am - a0)
    ccw = d_mid < d_end
    return not ccw


def _simplify(pts, tol):
    """Douglas–Peucker для прямых участков между дугами."""
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
    return _simplify(pts[:imax + 1], tol)[:-1] + _simplify(pts[imax:], tol)


def compress_lines(segs, first_point, tol=0.01):
    """Схлопывает подряд идущие отрезки: дуги ищутся по СЫРОМУ профилю, где
    прямые участки представлены сотнями точек через 0.1 мм. Дуги остаются
    как есть, прямые между ними упрощаются."""
    out = []
    buf = [first_point]
    for s in segs:
        if s[0] == "line":
            buf.append((s[1], s[2]))
            continue
        if len(buf) > 1:
            out += [("line", z, r) for z, r in _simplify(buf, tol)[1:]]
        out.append(s)
        buf = [(s[1], s[2])]
    if len(buf) > 1:
        out += [("line", z, r) for z, r in _simplify(buf, tol)[1:]]
    return out


def _arc_error(profile, p_start, p_end, zc, rc, rad, cw, samples=24):
    """Максимальное отклонение построенной ДУГИ от профиля, мм."""
    import math
    z0, r0 = p_start
    z1, r1 = p_end
    a0 = math.atan2(r0 - rc, z0 - zc)
    a1 = math.atan2(r1 - rc, z1 - zc)
    da = a1 - a0
    if cw:
        while da > 0:
            da -= 2 * math.pi
    else:
        while da < 0:
            da += 2 * math.pi
    worst = 0.0
    for k in range(1, samples):
        a = a0 + da * k / samples
        z = zc + rad * math.cos(a)
        r = rc + rad * math.sin(a)
        if z > max(z0, z1) + 1e-9 or z < min(z0, z1) - 1e-9:
            return float("inf")        # дуга вышла за диапазон Z участка
        worst = max(worst, abs(r - _r_at(profile, z)))
    return worst


def fit_arcs(profile, tol=0.005, min_pts=4, max_radius=5.0, min_sagitta=0.01):
    """Ломаный профиль → последовательность отрезков и ДУГ.

    Профиль снимается численно, поэтому скругления и радиусные переходы
    приходят пачками точек. Печатать их как G1 значит резать выпуклости
    хордами — это и даёт мелкий зарез на кромках. Здесь подряд идущие точки
    жадно укладываются в окружность: если все промежуточные лежат от неё не
    дальше tol, участок выводится одной дугой G2/G3.

    Возвращает список сегментов от ВТОРОЙ точки профиля:
      ("line", z, r) либо ("arc", z, r, zc, rc, cw)
    """
    segs = []
    i = 0
    n = len(profile)
    while i < n - 1:
        best = None
        # пробуем максимально длинный участок, начиная с самого длинного
        for j in range(n - 1, i + min_pts - 2, -1):
            mid = profile[(i + j) // 2]
            c = _circle_through(profile[i], mid, profile[j])
            if c is None:
                continue
            zc, rc, rad = c
            if rad > max_radius or rad < 1e-6:
                continue
            ok = True
            for k in range(i + 1, j):
                z, r = profile[k]
                if abs(((z - zc) ** 2 + (r - rc) ** 2) ** 0.5 - rad) > tol:
                    ok = False
                    break
            if not ok:
                continue
            # почти прямой участок дугой не описываем: стрелка прогиба меньше
            # min_sagitta означает, что это прямая, а дуга огромного радиуса
            # только теряет точность на I/K и путает стойку
            chord = ((profile[j][0] - profile[i][0]) ** 2
                     + (profile[j][1] - profile[i][1]) ** 2) ** 0.5
            half = min(chord / 2.0, rad)
            sagitta = rad - (rad * rad - half * half) ** 0.5
            if sagitta < min_sagitta:
                continue
            # ГЛАВНАЯ проверка — по самой дуге, а не по точкам профиля.
            # Точки профиля лежать на окружности могут, а дуга между ними
            # всё равно выгибаться наружу: на прямом участке длиной 5.6 мм
            # окружность R2.9 проходит через оба конца и уходит от детали на
            # 1.8 мм. Дискретизируем построенную дугу и сравниваем с профилем.
            cw_try = _arc_dir(profile[i][0], profile[i][1], profile[j][0],
                              profile[j][1], mid[0], mid[1], zc, rc)
            if _arc_error(profile, profile[i], profile[j], zc, rc, rad,
                          cw_try) > tol:
                continue
            best = (j, zc, rc, rad, cw_try)
            break
        if best:
            j, zc, rc, rad, cw = best
            segs.append(("arc", profile[j][0], profile[j][1], zc, rc, cw))
            i = j
        else:
            i += 1
            segs.append(("line", profile[i][0], profile[i][1]))
    return segs


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
    g.append("G54")                               # рабочая система координат
    if dia:
        g.append("DIAMON")                        # X трактуется как ДИАМЕТР
    g.append("G95" if per_rev else "G94")         # подача: мм/об или мм/мин
    # Смена инструмента на ТОКАРНОЙ стойке Sinumerik — «T<n> D<коррекция>»,
    # без M6: M6 запускает ToolChange.SPF с поворотом револьвера, и виртуальная
    # стойка на нём зависает. Заводская программа (CNC PILOT) тоже пишет
    # «T01.0» без M6.
    g.append(f"T{params.get('tool_number', 1)} D1")
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

    # 3. чистовой проход по профилю — отрезками и ДУГАМИ
    op("Finish")
    g.append(f"G0 {X(retract_r)} Z{profile[0][0] + clear:.3f}")
    g.append(f"G0 {X(profile[0][1])} Z{profile[0][0] + clear:.3f}")
    g.append(f"G1 {X(profile[0][1])} Z{profile[0][0]:.3f} {fmt(feed_finish)}")
    n_arcs = 0
    raw = prof_data.get("profile_raw")
    if params.get("arcs", True) and raw:
        # дуги ищем по СЫРОМУ профилю: упрощение уже спрямило скругления,
        # и по ломаной окружность не восстановить
        src = sorted([(z, r) for z, r in raw], key=lambda t: -t[0])
        segs = compress_lines(fit_arcs(src, params.get("arc_tol", 0.005)),
                              src[0], params.get("line_tol", 0.01))
        profile = src            # чистовой идёт по сырому профилю
    elif params.get("arcs", True):
        segs = fit_arcs(profile, params.get("arc_tol", 0.005))
    else:
        segs = [("line", z, r) for z, r in profile[1:]]
    zp, rp = profile[0]
    for s in segs:
        if s[0] == "line":
            _, z, r = s
            g.append(f"G1 {X(r)} Z{z:.3f} {fmt(feed_finish)}")
        else:
            _, z, r, zc, rc, cw = s
            # I и K — смещение ЦЕНТРА от начальной точки; I всегда в радиусах,
            # даже когда X выводится диаметром (так его читают Sinumerik/Fanuc)
            n_arcs += 1
            g.append(f"G{2 if cw else 3} {X(r)} Z{z:.3f} "
                     f"I{rc - rp:.4f} K{zc - zp:.4f} {fmt(feed_finish)}")
        zp, rp = s[1], s[2]
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
             "stock_radius": r_stock, "profile_points": len(profile),
             "arcs": n_arcs}
    return g, stats


def write(prof_data, params, path):
    g, stats = generate(prof_data, params)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(g) + "\n")
    return stats
