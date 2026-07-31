#!/usr/bin/env python3
"""
lathe_reach.py — что из профиля достаёт ПРОХОДНОЙ резец, а что нет.

Зачем. Проходной резец — клин: от вершины идут главная кромка под углом в плане
φ и вспомогательная под φ₁ = 180 − φ − ε (ε — угол при вершине пластины). Когда
программа врезается вглубь (в канавку, под уступ), вспомогательная кромка тянется
по УЖЕ ОБТОЧЕННОЙ стенке позади и срезает её. Это не артефакт симулятора:
замерено в NX ISV на 4-13A — шестигранник срезан ровно скатом φ₁, и наклон совпал
с расчётным до 0.2° (см. .claude/CLAUDE.md, I10).

Поэтому профиль делится на две части:
  * ОГИБАЮЩАЯ G(z) ≥ R(z) — то, что резец может пройти, не задев ничего лишнего;
  * КАНАВКИ — материал между R(z) и G(z), его берёт канавочный резец (T2).

Геометрия (плоскость (z, r), ось z, вершина резца в (z₀, r₀), точение в −Z):
  * позади (z > z₀) нижняя граница резца — вспомогательная кромка:
        r = r₀ + (z − z₀)·tan φ₁,        длина хода по z: L·cos φ₁
  * впереди (z < z₀) — главная кромка:
        r = r₀ + (z₀ − z)/tan(φ − 90°),  длина хода по z: L·|cos φ|
Вершина ставится в (z₀, R(z₀)) без зареза, если обе границы идут не ниже профиля.
Отсюда огибающая — максимум профиля, «протянутого» вдоль обеих кромок.

Длина кромки L важна: подрез обрывается на ней (проверено — вдвое меньшая
пластина дала вдвое короче подрез), поэтому окна ограничены L, а не бесконечны.
"""

import math


def _sampler(profile):
    """Профиль [(z, r)] → функция R(z) с линейной интерполяцией."""
    pts = sorted(((float(z), float(r)) for z, r in profile), key=lambda p: p[0])
    zs = [p[0] for p in pts]

    def R(z):
        if z <= zs[0]:
            return pts[0][1]
        if z >= zs[-1]:
            return pts[-1][1]
        lo, hi = 0, len(zs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if zs[mid] <= z:
                lo = mid
            else:
                hi = mid
        z0, r0 = pts[lo]
        z1, r1 = pts[hi]
        if z1 - z0 < 1e-12:
            return max(r0, r1)
        return r0 + (r1 - r0) * (z - z0) / (z1 - z0)

    return R, zs[0], zs[-1]


def envelope(profile, approach_deg=107.5, nose_deg=55.0, edge_len=6.35,
             step=0.05, tol=0.02, hand="R"):
    """Профиль → (огибающая для проходного резца, список канавок).

    approach_deg — угол в плане φ, nose_deg — угол при вершине пластины ε;
    вспомогательный угол считается как φ₁ = 180 − φ − ε. edge_len — длина
    режущей кромки (у DCMT 07 02 04 — 6.35 мм).

    Возвращает (env, grooves):
      env     — [(z, r)] от торца вглубь (z по убыванию), шагом step;
      grooves — [{"z_hi", "z_lo", "r_min", "width_bottom", "volume_mm3"}].

    hand="L" — ЛЕВЫЙ резец, точит в обратную сторону (+Z): у него главная и
    вспомогательная кромки меняются местами относительно оси. Считается тем же
    кодом на зеркальном профиле — так исключено расхождение двух реализаций.
    """
    if hand == "L":
        mirrored = [(-z, r) for z, r in profile]
        env, grooves = envelope(mirrored, approach_deg, nose_deg, edge_len,
                                step, tol, hand="R")
        env = [(-z, r) for z, r in env][::-1]
        for g in grooves:
            g["z_hi"], g["z_lo"] = -g["z_lo"], -g["z_hi"]
        return env, grooves[::-1]
    minor_deg = 180.0 - approach_deg - nose_deg
    R, z_min, z_max = _sampler(profile)
    n = max(2, int(round((z_max - z_min) / step)) + 1)
    zs = [z_max - i * (z_max - z_min) / (n - 1) for i in range(n)]   # по убыванию
    rs = [R(z) for z in zs]

    # хвост «вперёд» вообще не режется, если главная кромка отвесная
    k_back = math.tan(math.radians(minor_deg)) if minor_deg > 0.01 else 0.0
    fwd = approach_deg - 90.0
    k_fwd = (1.0 / math.tan(math.radians(fwd))) if fwd > 0.01 else 1e9

    dz = (z_max - z_min) / (n - 1)
    w_back = max(1, int(edge_len * math.cos(math.radians(max(minor_deg, 0.0))) / dz))
    w_fwd = max(1, int(edge_len * abs(math.cos(math.radians(approach_deg))) / dz))

    env = list(rs)
    for i in range(n):
        best = rs[i]
        # позади вершины (больший z) — вспомогательная кромка
        for j in range(max(0, i - w_back), i):
            best = max(best, rs[j] - (zs[j] - zs[i]) * k_back)
        # впереди вершины (меньший z) — главная кромка
        for j in range(i + 1, min(n, i + w_fwd + 1)):
            best = max(best, rs[j] - (zs[i] - zs[j]) * k_fwd)
        env[i] = best

    grooves = []
    i = 0
    while i < n:
        if env[i] - rs[i] <= tol:
            i += 1
            continue
        j = i
        while j < n and env[j] - rs[j] > tol:
            j += 1
        seg = list(range(i, j))
        r_min = min(rs[k] for k in seg)
        flat = [zs[k] for k in seg if rs[k] <= r_min + 0.05]
        vol = sum(math.pi * (env[k] ** 2 - rs[k] ** 2) * dz for k in seg)
        grooves.append({
            "z_hi": zs[seg[0]], "z_lo": zs[seg[-1]], "r_min": r_min,
            "width_bottom": (max(flat) - min(flat)) if len(flat) > 1 else 0.0,
            "volume_mm3": vol,
        })
        i = j

    return [(zs[i], env[i]) for i in range(n)], grooves


def fit_blade(grooves, wanted, w_min=1.0, ignore_below=0.3):
    """Ширина канавочного резца: не шире самой узкой канавки.

    Технолог подбирает пластину под самый узкий паз. Зоны с вырожденным дном
    (< ignore_below) в подбор не идут: это не канавка, а сходящий на нет конус
    у отрезки — под него пластину не выбирают. Ниже w_min не опускаемся —
    тоньше пластин в ходу не бывает.

    Возвращает (ширина, есть_ли_зона_уже_пластины).
    """
    widths = [g["width_bottom"] for g in grooves if g["width_bottom"] >= ignore_below]
    w = max(w_min, min(wanted, min(widths))) if widths else float(wanted)
    w = math.floor(w * 10) / 10.0                       # до 0.1 мм вниз
    too_narrow = any(0 < g["width_bottom"] < w - 1e-6 for g in grooves)
    return w, too_narrow


def split_by_hand(profile, approach_deg=107.5, nose_deg=55.0, edge_len=6.35,
                  step=0.05, tol=0.02):
    """Кто что режет: правый проходной, левый проходной, канавочный.

    Правый резец (точит к патрону, −Z) не достаёт туда, где позади него, со
    стороны торца, стоит уступ выше текущего радиуса. Левый (точит от патрона,
    +Z) в этих местах как раз работает — у него кромки зеркальны. Что не берёт
    ни один, остаётся канавочному.

    Так же устроен и заводской техпроцесс: у КнААЗ в списке 14-31A стоят и
    правый проходной, и левый, и канавочный.

    Возвращает (env_R, left_zones, grooves):
      env_R      — огибающая для ПРАВОГО резца (по ней идут черновая и чистовая);
      left_zones — [{"z_hi","z_lo","volume_mm3"}] участки для ЛЕВОГО;
      grooves    — [{...}] то, что недостижимо обоим (канавочный).
    """
    env_r, _ = envelope(profile, approach_deg, nose_deg, edge_len, step, tol, "R")
    env_l, _ = envelope(profile, approach_deg, nose_deg, edge_len, step, tol, "L")
    R, _, _ = _sampler(profile)
    n = len(env_r)
    dz = abs(env_r[0][0] - env_r[-1][0]) / max(1, n - 1)

    kind = []                     # 'R' достаёт правый, 'L' левый, 'G' никто
    for i in range(n):
        z, er = env_r[i]
        el = env_l[i][1]
        r0 = R(z)
        if er - r0 <= tol:
            kind.append("R")
        elif el - r0 <= tol:
            kind.append("L")
        else:
            kind.append("G")

    def runs(mark):
        out, i = [], 0
        while i < n:
            if kind[i] != mark:
                i += 1
                continue
            j = i
            while j < n and kind[j] == mark:
                j += 1
            seg = range(i, j)
            vol = sum(math.pi * (max(env_r[k][1], R(env_r[k][0])) ** 2
                                 - R(env_r[k][0]) ** 2) * dz for k in seg)
            out.append({"z_hi": env_r[i][0], "z_lo": env_r[j - 1][0],
                        "volume_mm3": vol,
                        "r_min": min(R(env_r[k][0]) for k in seg)})
            i = j
        return out

    grooves = []
    for gz in runs("G"):
        seg_z = [z for z, _ in env_r if gz["z_lo"] <= z <= gz["z_hi"]]
        flat = [z for z in seg_z if R(z) <= gz["r_min"] + 0.05]
        gz["width_bottom"] = (max(flat) - min(flat)) if len(flat) > 1 else 0.0
        grooves.append(gz)
    return env_r, runs("L"), grooves
