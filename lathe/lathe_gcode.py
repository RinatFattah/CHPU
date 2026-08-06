#!/usr/bin/env python3
"""
lathe_gcode.py — осевой профиль детали → токарная управляющая программа.

Стратегия — продольное наружное точение, как у технолога, ДВУМЯ инструментами:
  T1, проходной резец:
  1. FaceOff   — подрезка торца заготовки до Z0;
  2. Rough1..N — черновая продольными проходами: на каждом уровне радиуса резец
     идёт от торца вглубь, пока профиль детали не поднимется выше этого уровня,
     затем отвод по X и возврат. Уровни идут от радиуса заготовки к детали с
     шагом DEPTH_OF_CUT, с припуском ALLOWANCE на чистовую;
  3. Finish    — чистовой проход по профилю (по всем точкам, к номиналу);
  T2, канавочный резец:
  4. Groove1..N — канавки и поднутрения врезаниями с перекрытием;
  5. Partoff    — отрезка (опционально).

Разделение работы между T1 и T2 — не украшение, а геометрия: проходной резец
клин, и врезаясь вглубь он волочит вспомогательной кромкой по уже обточенной
стенке позади (замерено в NX ISV). Что ему достижимо, считает `lathe_reach`;
остальное уходит канавочному. Так же устроен и заводской техпроцесс.
`--no-groove-tool` возвращает прежнее поведение: всё одним резцом.

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


def level_pass_end(target, level):
    """Докуда дойдёт ПРОДОЛЬНЫЙ проход на постоянном радиусе `level`.

    Проход идёт от торца вглубь и обрывается там, где профиль-цель поднимается
    выше уровня: дальше материал стоит стеной. Точка обрыва берётся с
    интерполяцией, поэтому конец прохода ложится ТОЧНО на контур — ровно так
    делает завод. Проверено по числам `UST1.NC`: у них конец прохода сдвигается
    на 0.082 при шаге 0.465 по радиусу, а это тангенс 80° — наклон того самого
    уступа на шестигранник; ниже по программе отношение становится 0.347/0.347,
    то есть 45°, наклон фаски на резьбу.

    `target` — профиль-цель (чистовой путь плюс припуск), отсортирован по
    убыванию z. Возвращает z конца прохода либо None, если стена стоит уже у
    торца и проход не режет ничего.
    """
    prev = None
    for z, r in target:
        if r > level + 1e-9:
            if prev is None:
                return None
            pz, pr = prev
            if r != pr:
                return pz + (z - pz) * (level - pr) / (r - pr)
            return pz
        prev = (z, r)
    return target[-1][0]


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


def _outward_normals(profile):
    """Внешняя нормаль детали в каждой точке профиля (z по убыванию).

    Направление вдоль профиля берётся центральной разностью, нормаль — поворотом
    на −90°: n = (d_r, −d_z). На цилиндре даёт (0, 1), то есть строго наружу.
    """
    n = len(profile)
    out = []
    for i in range(n):
        j0 = max(0, i - 1)
        j1 = min(n - 1, i + 1)
        dz = profile[j1][0] - profile[j0][0]
        dr = profile[j1][1] - profile[j0][1]
        norm = (dz * dz + dr * dr) ** 0.5 or 1.0
        out.append((dr / norm, -dz / norm))
    return out


def compensate_nose(profile, nose_radius, tip=(1.0, -1.0)):
    """Профиль детали → траектория ПРОГРАММНОЙ ТОЧКИ резца (эквидистанта).

    Зачем. У резца скруглённая вершина R, а мы до сих пор печатали точки самого
    профиля, как будто вершина острая. На цилиндре и торце это погрешности не
    даёт, а на КАЖДОМ уклоне даёт — замерено в NX ISV: до 0.3 мм на пологом
    конусе и 1.65 мм на крутом уступе. Заводская программа идёт с `G40`, то есть
    коррекция на стойке выключена и эквидистанту считает CAM — делаем так же.

    Геометрия: центр скругления должен идти по эквидистанте детали,
        C = точка_профиля + R · внешняя_нормаль,
    а печатаем мы МНИМУЮ ВЕРШИНУ — угол габаритного квадрата вокруг скругления,
    её положение задаётся положением режущей кромки (Schneidenlage, $TC_DP2):
        P = C + R · tip,   tip = (±1, −1) в долях R.
    Радиальная часть −1 обязательна: только с ней цилиндр выходит ровно в
    программный размер (проверено — цилиндры Ø10 дают ровно 5.000). Осевая
    часть зависит от соглашения стойки, поэтому вынесена в параметр.

    Профиль может дать самопересечение эквидистанты во вогнутых углах — путь
    прореживается до монотонного по z, иначе резец пойдёт назад.
    """
    if not nose_radius:
        return list(profile)
    normals = _outward_normals(profile)
    path = []
    for (z, r), (nz, nr) in zip(profile, normals):
        cz, cr = z + nose_radius * nz, r + nose_radius * nr
        path.append((cz + nose_radius * tip[0], cr + nose_radius * tip[1]))
    out = [path[0]]
    for pt in path[1:]:
        if pt[0] <= out[-1][0] + 1e-9:        # z не должен расти обратно
            out.append(pt)
    return out


# Ряд диаметров спиральных свёрл (ГОСТ 885), мм. Берём ближайшее МЕНЬШЕЕ —
# отверстие потом доводится расточным резцом в размер.
DRILL_SERIES = [2.0, 2.5, 3.0, 3.2, 3.5, 4.0, 4.2, 4.5, 5.0, 5.5, 6.0, 6.5,
                7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5, 12.0,
                12.5, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 22.0,
                24.0, 25.0, 26.0, 28.0, 30.0]


# Ряд наружных диаметров метрической резьбы (ГОСТ 8724), мм
METRIC_MAJOR = [6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 27, 30, 33, 36, 39, 42]


def find_thread_candidates(profile, min_len=3.0, tol=0.3):
    """Гладкие цилиндры, похожие на резьбовые участки.

    РЕЗЬБЫ В МОДЕЛИ НЕТ: CAD несёт только наружный диаметр, а шаг знает лишь
    чертёж (у 14-31A это Ø22 → M22×1.5, и определить «×1.5» из геометрии
    нельзя — у M22 бывают шаги 2.5, 1.5 и 1.0). Поэтому здесь только КАНДИДАТЫ:
    цилиндр длиной от min_len, чей диаметр попадает в ряд ГОСТ 8724. Нарезание
    включается, только когда обозначение задано явно (LATHE_THREADS).

    Возвращает [{"d", "z_hi", "z_lo", "length"}].
    """
    out = []
    i = 0
    pts = sorted(profile, key=lambda t: -t[0])
    n = len(pts)
    while i < n - 1:
        j = i
        while j + 1 < n and abs(pts[j + 1][1] - pts[i][1]) < 0.02:
            j += 1
        length = pts[i][0] - pts[j][0]
        d = 2 * pts[i][1]
        if length >= min_len:
            near = [m for m in METRIC_MAJOR if abs(m - d) <= tol]
            if near:
                out.append({"d": near[0], "d_model": round(d, 3),
                            "z_hi": pts[i][0], "z_lo": pts[j][0],
                            "length": round(length, 2)})
        i = max(j, i + 1)
    return out


def _pick_drill(d_max, series=None):
    """Наибольшее сверло из ряда, не превышающее d_max."""
    ser = series or DRILL_SERIES
    fit = [d for d in ser if d <= d_max + 1e-9]
    return max(fit) if fit else min(ser)


def compensate_nose_left(profile_asc, nose_radius, tip=(1.0, -1.0)):
    """Эквидистанта для ЛЕВОГО резца (идёт в +Z). Профиль подаётся по
    возрастанию z. Считается зеркалированием по z и той же функцией, что для
    правого, — чтобы не заводить вторую реализацию, которая разойдётся."""
    mirrored = [(-z, r) for z, r in profile_asc]        # тут z уже по убыванию
    out = compensate_nose(mirrored, nose_radius, tip)
    return [(-z, r) for z, r in out]


def generate(prof_data, params):
    """Профиль + параметры → (строки G-кода, статистика)."""
    profile = [(z, r) for z, r in prof_data["profile"]]
    profile.sort(key=lambda p: -p[0])            # от торца (Z max) вглубь
    part_profile = list(profile)                 # НОМИНАЛ детали, до огибающей
    part_raw = sorted([(z, r) for z, r in (prof_data.get("profile_raw")
                                          or prof_data["profile"])],
                      key=lambda t: -t[0])       # сырой номинал, для левого резца

    # Разделение работы между инструментами. Проходной резец — клин, и в канавку
    # он не лезет: вспомогательная кромка волочит по уже обточенной стенке
    # позади (замерено в NX ISV, см. lathe_reach). Поэтому профиль делится на
    # достижимую огибающую (её берёт T1) и канавки (их берёт канавочный T2).
    raw = prof_data.get("profile_raw")
    from lathe import lathe_reach

    # Геометрия ДВУХ проходных резцов. Достижимость считается по ЧИСТОВОМУ: он
    # оставляет окончательную поверхность, и именно его вспомогательный угол
    # решает, куда точение вообще заходит. Черновые слои ограничиваются
    # огибающей ЧЕРНОВОГО — он тупее и в уступ не лезет. Без --finish-tool обе
    # геометрии совпадают, и поведение ровно прежнее.
    fin_tool = params.get("finish_tool", False)
    geo_rough = (params.get("approach_angle", 107.5),
                 params.get("nose_angle", 55.0),
                 params.get("insert_edge", 6.35))
    geo_finish = ((geo_rough[0],
                   params.get("finish_nose_angle", 35.0),
                   params.get("finish_insert_edge", 6.35))
                  if fin_tool else geo_rough)

    grooves, left_zones, blade, blade_tight = [], [], 0.0, False
    if params.get("groove_tool", True):
        env, left_zones, grooves = lathe_reach.split_by_hand(
            raw or prof_data["profile"],
            approach_deg=geo_finish[0],
            nose_deg=geo_finish[1],
            edge_len=geo_finish[2])
        if not params.get("left_tool", True):
            left_zones = []          # без левого резца — всё как раньше
        if grooves or left_zones:
            blade, blade_tight = lathe_reach.fit_blade(
                grooves, params.get("groove_width", 3.0),
                params.get("groove_width_min", 1.0))
            profile = _simplify(env, params.get("simplify_tol", 0.01))
            raw = env                     # чистовой T1 идёт по огибающей

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
    # Установ. Во втором деталь перевёрнута и НУЛЬ ДРУГОЙ — без этой строки
    # программу невозможно поставить на станок правильно.
    if params.get("setup_note"):
        g.append(f"({params['setup_note']})")
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
    g.append(f"(Tool T{params.get('tool_number', 1)}: turning insert "
             f"{params.get('insert', 'DCMT070204R')}, "
             f"nose R{params.get('nose_radius', 0.4)} mm)")
    if blade:
        g.append(f"(Tool T{params.get('groove_tool_number', 2)}: grooving blade "
                 f"W{blade:.2f} mm - {len(grooves)} groove(s)"
                 + (", partoff" if params.get("partoff", True) else "") + ")")
    g.append(f"(Profile points: {len(profile)})")
    g.append(f"(X in {'DIAMETER' if dia else 'RADIUS'} mode, feed in "
             f"{'mm/rev G95' if per_rev else 'mm/min G94'})")
    g.append("G18 G21 G90")                       # плоскость ZX, мм, абсолютные
    g.append("G54")                               # рабочая система координат
    if dia:
        # DIAMON обязателен, а не «для порядка»: без него стойка читает X как
        # РАДИУС, координаты оказываются вдвое больше и прогон отвергается по
        # лимитам осей (проверено перебором преамбул на sim11).
        g.append("DIAMON")
    g.append("G95" if per_rev else "G94")         # подача: мм/об или мм/мин
    # Смена инструмента — «T<n>» и «M6» ОТДЕЛЬНЫМИ строками. Проверено перебором
    # на виртуальной стойке sim11_turn_2ax_sinumerik: вариант «T1 D1» без M6
    # стойка не принимает (инструмент не встаёт в шпиндель), а связка T+M6
    # работает — но ТОЛЬКО когда в subprog станка лежит TO_INI.SPF: без таблицы
    # ToolChange.SPF читает несуществующий $TC_TP1 и виснет.
    g.append(f"T{params.get('tool_number', 1)}")
    g.append("M6")
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

    # ЧЕРНОВОЙ ПРОХОДАМИ ПОСТОЯННОГО РАДИУСА НЕТ. Убран намеренно: такие проходы
    # не знают ни компенсации радиуса при вершине, ни ограничения огибающей, и
    # волочат кромкой на каждом уклоне — замерено, они давали зарез 0.29 мм на
    # уступе 43° и 1.23 мм на уступе 75°, тогда как один чистовой даёт 0.04.
    # Послойный съём вернулся ниже в другом виде — по эквидистанте (--contour-rough).

    # 3. чистовой проход по профилю — отрезками и ДУГАМИ
    n_arcs = 0
    # ЭКВИДИСТАНТА: чистовой идёт не по профилю детали, а по траектории
    # ПРОГРАММНОЙ ТОЧКИ резца — иначе на каждом уклоне остаётся зарез r·tg(α).
    # Считается ДО всего остального: и заход, и дуги должны строиться уже по
    # ней, иначе первый ход уйдёт назад (заход в одну точку, траектория из
    # другой) и центры первых дуг посчитаются от чужого начала.
    nose_r = params.get("nose_radius", 0.4) if params.get("nose_comp", True) else 0.0
    src = sorted([(z, r) for z, r in (raw or profile)], key=lambda t: -t[0])
    if nose_r:
        src = compensate_nose(src, nose_r, params.get("tip_offset", (1.0, -1.0)))
        # ОГРАНИЧЕНИЕ ОГИБАЮЩЕЙ — ПОСЛЕ компенсации, а не до.
        # Огибающая — это предельное положение ВЕРШИНЫ резца, а не поверхность
        # детали. Компенсировать её нельзя: компенсация опускает путь ниже
        # предела, и вспомогательная кромка начинает волочить по соседнему
        # уступу. На 4-13A так срезался шестигранник — перемычка через канавку
        # шла на 0.107 мм ниже предела по всей длине.
        if params.get("groove_tool", True):
            src, _ = lathe_reach.envelope(
                src, approach_deg=geo_finish[0], nose_deg=geo_finish[1],
                edge_len=geo_finish[2])
    # Предел ЧЕРНОВОГО резца — своя огибающая поверх той же эквидистанты. Он
    # тупее чистового, и в уступы, куда чистовой заходит, ему нельзя: подрежет
    # стенку позади. Разница между двумя пределами и есть то, что чистовой
    # снимает сверх припуска.
    src_rough = src
    if fin_tool and params.get("groove_tool", True) and nose_r:
        src_rough, _ = lathe_reach.envelope(
            src, approach_deg=geo_rough[0], nose_deg=geo_rough[1],
            edge_len=geo_rough[2])
    if params.get("arcs", True):
        # дуги ищем по СЫРОМУ (неупрощённому) пути: упрощение уже спрямило
        # скругления, и по ломаной окружность не восстановить
        segs = compress_lines(fit_arcs(src, params.get("arc_tol", 0.005)),
                              src[0], params.get("line_tol", 0.01))
    else:
        segs = [("line", z, r) for z, r in src[1:]]
    turn_profile = list(profile)  # что оставил T1 (огибающая) — нужно канавкам
    profile = src                # дальше по тексту чистовой идёт по этому пути

    # 2. ЧЕРНОВЫЕ ПРОХОДЫ ПО ЭКВИДИСТАНТЕ (--contour-rough, по умолчанию ВЫКЛ)
    #
    # Одним чистовым проходом снять всё нельзя физически: на 14-31A это Ø34 → Ø22
    # на резьбовом участке, а в самом глубоком месте 10.674 мм по радиусу за один
    # проход (замерено) при подаче 0.05 мм/об.
    # Заводской T1 берёт то же место слоями (81 рабочий ход при 51
    # у чистового). Поэтому съём раскладывается на слои — но НЕ так, как в
    # убранной старой черновой: там проходы шли постоянным радиусом, ничего не
    # знали ни о компенсации, ни об огибающей, и волочили кромкой на каждом
    # уклоне (0.29 мм на уступе 43°, 1.23 мм на 75°). Здесь каждый слой — ТА ЖЕ
    # чистовая траектория, отодвинутая по радиусу: и компенсация, и предел
    # огибающей уже внутри неё, а материал позади слоя отодвинут ровно на
    # столько же, так что зазор под вспомогательной кромкой сохраняется.
    # Выше заготовки путь прижимается к её радиусу — там резец идёт по воздуху.
    n_rough = 0
    if params.get("contour_rough", False) and params.get("rough_mode") == "levels":
        # ЧЕРНОВЫЕ ПРОХОДЫ ПОСТОЯННОГО ДИАМЕТРА — заводская схема (LATHE_ROUGH_MODE
        # = "levels"). Каждый проход идёт вдоль оси на своём радиусе и обрывается
        # там, где профиль поднимается выше него; шаг между уровнями = глубина
        # резания. Так устроен T1 в заводской UST1.NC: 11 проходов с шагом 0.465
        # мм по радиусу, конец каждого лежит точно на контуре.
        #
        # РИСК, о котором нельзя забывать: такой проход НЕ следует форме детали,
        # и на уклоне вспомогательная кромка резца идёт по уже обточенной стенке
        # позади. Ровно поэтому черновая постоянного радиуса была убрана в июле
        # (замерено: 0.29 мм зареза на уступе 43°, 1.23 мм на 75°). Завод так
        # работает потому, что у его пластины φ₁ = 32°, а шаблон NX в ISV даёт
        # 17.5° — то есть в симуляции волочение будет БОЛЬШЕ настоящего.
        ap_r = max(0.05, ap)
        allow = max(0.0, allowance)
        base = _simplify(src_rough, params.get("line_tol", 0.01))
        target = [(z, min(r + allow, r_stock)) for z, r in base]
        r_min = min(r for _, r in target)
        n_lv = max(0, -(-int((r_stock - r_min) * 1000) // int(ap_r * 1000)))
        for i in range(n_lv):
            level = max(r_min, r_stock - (i + 1) * ap_r)
            z_stop = level_pass_end(target, level)
            if z_stop is None or z_stop >= target[0][0] - 0.05:
                continue                     # проход ничего не снимает
            n_rough += 1
            op(f"Rough{n_rough}")
            g.append(f"G0 {X(retract_r)} Z{z_top + clear:.3f}")
            g.append(f"G0 {X(level)} Z{z_top + clear:.3f}")
            g.append(f"G1 {X(level)} Z{z_stop:.3f} {fmt(feed)}")
            # отвод по радиусу: всё, что снаружи уровня выше z_stop, этим же
            # проходом уже снято, поэтому наружу резец идёт по воздуху
            g.append(f"G0 {X(retract_r)} Z{z_stop:.3f}")
            g.append(f"(Finish operation: Rough{n_rough})")
    elif params.get("contour_rough", False):
        ap_r = max(0.05, ap)
        allow = max(0.0, allowance)
        deepest = max(r_stock - r for _, r in src_rough)
        n_rough = max(0, -(-int((deepest - allow) * 1000) // int(ap_r * 1000)))
        # Упрощаем ОДИН раз, ДО сдвига. Если упрощать каждый слой отдельно,
        # Дуглас-Пекер выбирает у них РАЗНЫЕ вершины, слои перестают быть
        # параллельными и глубина резания местами вылезает за заданную: на
        # 14-31A замерено 1.456 мм при ap = 1.0. С общими вершинами разница
        # между соседними слоями ровно ap в каждой точке (клиппинг по радиусу
        # заготовки её только уменьшает).
        base = _simplify(src_rough, params.get("line_tol", 0.01))
        for i in range(n_rough):
            off = allow + (n_rough - 1 - i) * ap_r
            pts = [(z, min(r + off, r_stock)) for z, r in base]
            # схлопнуть плато на радиусе заготовки: там резец идёт по воздуху,
            # и промежуточные вершины не нужны. Сами вершины при этом не
            # смещаются, так что параллельность слоёв сохраняется.
            pts = [pt for j, pt in enumerate(pts)
                   if not (0 < j < len(pts) - 1
                           and pt[1] >= r_stock - 1e-9
                           and pts[j - 1][1] >= r_stock - 1e-9
                           and pts[j + 1][1] >= r_stock - 1e-9)]
            op(f"Rough{i + 1}")
            g.append(f"G0 {X(retract_r)} Z{pts[0][0] + clear:.3f}")
            g.append(f"G0 {X(pts[0][1])} Z{pts[0][0] + clear:.3f}")
            g.append(f"G1 {X(pts[0][1])} Z{pts[0][0]:.3f} {fmt(feed)}")
            for z, r in pts[1:]:
                g.append(f"G1 {X(r)} Z{z:.3f} {fmt(feed)}")
            g.append(f"G0 {X(retract_r)} Z{z_end:.3f}")
            g.append(f"(Finish operation: Rough{i + 1})")

    # ЧИСТОВОЙ РЕЗЕЦ T8 — отдельным инструментом, как на заводе
    t_fin = params.get("finish_tool_number", 8)
    if fin_tool:
        g.append(f"G0 {X(retract_r)} Z{z_top + clear:.3f}")
        g.append("M5")
        g.append(f"T{t_fin}")
        g.append("M6")
        g.append(f"G97 S{rpm} M3")
        g.append(f"(Tool T{t_fin}: finishing insert "
                 f"{params.get('finish_insert', 'VCMT110304')}, "
                 f"nose R{nose_r:g} mm, {geo_finish[1]:g}° rhombic)")

    op("Finish")
    g.append(f"G0 {X(retract_r)} Z{src[0][0] + clear:.3f}")
    g.append(f"G0 {X(src[0][1])} Z{src[0][0] + clear:.3f}")
    g.append(f"G1 {X(src[0][1])} Z{src[0][0]:.3f} {fmt(feed_finish)}")
    zp, rp = src[0]
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


    # 3b. ЛЕВЫЙ проходной резец T3 — то, куда правый не достаёт
    #
    # Правый резец не заходит за уступ, который остаётся у него ПОЗАДИ, со
    # стороны торца: вспомогательная кромка волочит по нему (это и срезало
    # шестигранник). У левого кромки зеркальны, он точит от патрона (+Z) и эти
    # участки берёт штатно. Заводской комплект 14-31A устроен так же: правый
    # проходной OD_55_L, левый OD_35_L, канавочный.
    #
    # Участок, упирающийся В ТОРЕЦ ДЕТАЛИ, левому недоступен тоже: заходить
    # некуда, там ещё не отрезанный пруток. Это честно ВТОРОЙ УСТАНОВ — так же
    # поступает завод (UST1.NC + UST2.NC), и мы его не режем, а выносим в отчёт.
    n_left = 0
    second_setup = []
    doable = []
    for lz in left_zones:
        (second_setup if lz["z_lo"] <= z_end + 1e-6 else doable).append(lz)
    if doable:
        g.append(f"G0 {X(retract_r)} Z{z_top + clear:.3f}")
        g.append("M5")
        g.append(f"T{params.get('left_tool_number', 3)}")
        g.append("M6")
        g.append(f"G97 S{rpm} M3")
        g.append("(Tool T%d: left-hand turning insert, cuts towards +Z)"
                 % params.get("left_tool_number", 3))
    for lz in doable:
        pts = sorted([(z, r) for z, r in part_raw
                      if lz["z_lo"] - 1e-9 <= z <= lz["z_hi"] + 1e-9],
                     key=lambda t: t[0])          # по возрастанию z: идём в +Z
        if len(pts) < 2:
            continue
        path = (compensate_nose_left(pts, nose_r, params.get("tip_offset", (1.0, -1.0)))
                if nose_r else pts)
        n_left += 1
        op(f"LeftTurn{n_left}")
        # подход радиально в том z, где правый резец уже снял материал до профиля
        g.append(f"G0 {X(retract_r)} Z{path[0][0]:.3f}")
        g.append(f"G0 {X(path[0][1])} Z{path[0][0]:.3f}")
        for z, r in path[1:]:
            g.append(f"G1 {X(r)} Z{z:.3f} {fmt(feed_finish)}")
        g.append(f"G0 {X(retract_r)} Z{path[-1][0]:.3f}")
        g.append(f"(Finish operation: LeftTurn{n_left})")


    # 3c. ОСЕВОЕ ОТВЕРСТИЕ: сверление T4 + растачивание T5
    #
    # У этих деталей отверстие даёт больше половины съёма (14-31A: 2718 мм³ из
    # 4990, 22-13A: 10007 из 17344), так что без него программа неполна. Схема
    # заводская: сверлим с недоходом по диаметру, затем растачиваем в размер —
    # у них на 14-31A это сверло Ø10 и расточной до Ø11.9.
    n_drill = 0
    bore = params.get("bore") or []
    bore = [(z, r) for z, r in bore if r > 0.05]
    if bore and params.get("drill", True):
        z_top_b = max(z for z, _ in bore)
        z_bot_b = min(z for z, _ in bore)
        r_bore = min(r for _, r in bore)           # самое узкое место отверстия
        d_bore = 2 * r_bore
        allow = params.get("bore_allowance", 0.75)
        d_drill = _pick_drill(d_bore - 2 * allow,
                              params.get("drill_series"))
        depth = z_bot_b - (0.3 * d_drill if z_bot_b <= z_end + 1e-6 else 0.0)
        # ОТВЕРСТИЕ С ДВУХ СТОРОН. Сверлить и растачивать на всю длину из одного
        # установа — это вылет сверла Ø10 на 49 мм (L/D 4.9) и борштанги на 46
        # (L/D ≈ 4, жёсткость падает как куб вылета). Завод делает половину с
        # каждой стороны: на 14-31A сверло идёт до z −28.95, расточной до −25.50
        # при длине детали 46. hole_depth_* — предел ЭТОГО установа; None =
        # прежнее поведение, насквозь.
        z_lim_d = params.get("hole_depth_drill")
        if z_lim_d is not None:
            depth = max(depth, float(z_lim_d))
        z_lim_b = params.get("hole_depth_bore")
        z_bore_bot = (z_bot_b if z_lim_b is None
                      else max(z_bot_b, float(z_lim_b)))
        t4 = params.get("drill_tool_number", 4)
        t5 = params.get("bore_tool_number", 5)
        f_dr = params.get("feed_per_rev_drill", 0.06)
        f_bo = params.get("feed_per_rev_bore", 0.05)
        peck = params.get("drill_peck", 5.0)

        # ЦЕНТРОВКА: короткое жёсткое сверло намечает лунку, иначе длинное
        # спиральное уводит с оси. У завода это T4 Ø3.15 на Z−1.5, F0.05, S600.
        d_ctr = params.get("center_drill_d", 3.15)
        if params.get("center_drill", True) and d_drill > d_ctr:
            t7 = params.get("center_tool_number", 7)
            z_ctr = z_top_b - params.get("center_drill_depth", 1.5)
            g.append(f"G0 {X(retract_r)} Z{z_top + clear:.3f}")
            g.append("M5")
            g.append(f"T{t7}")
            g.append("M6")
            g.append(f"G97 S{params.get('center_speed', 600)} M3")
            g.append(f"(Tool T{t7}: center drill D{d_ctr:.2f} mm)")
            n_drill += 1
            op("DrillCenter1")
            g.append(f"G0 X0.000 Z{z_top_b + clear:.3f}")
            g.append(f"G1 X0.000 Z{z_ctr:.3f} "
                     f"{fmt(params.get('feed_per_rev_center', 0.05))}")
            g.append(f"G0 X0.000 Z{z_top_b + clear:.3f}")
            g.append("(Finish operation: DrillCenter1)")

        g.append(f"G0 {X(retract_r)} Z{z_top + clear:.3f}")
        g.append("M5")
        g.append(f"T{t4}")
        g.append("M6")
        g.append(f"G97 S{params.get('drill_speed', 800)} M3")
        g.append(f"(Tool T{t4}: twist drill D{d_drill:.2f} mm)")
        n_drill += 1
        op("Drill1")
        g.append(f"G0 X0.000 Z{z_top_b + clear:.3f}")
        z_cur = z_top_b
        while z_cur > depth + 1e-6:
            z_next = max(z_cur - peck, depth)
            g.append(f"G1 X0.000 Z{z_next:.3f} {fmt(f_dr)}")
            g.append(f"G0 X0.000 Z{z_top_b + clear:.3f}")   # вывод стружки
            if z_next > depth + 1e-6:
                g.append(f"G0 X0.000 Z{z_next + 0.5:.3f}")
            z_cur = z_next
        g.append("(Finish operation: Drill1)")

        # растачивание в размер, если сверло меньше отверстия
        if d_bore - d_drill > 0.05:
            g.append(f"G0 {X(retract_r)} Z{z_top + clear:.3f}")
            g.append("M5")
            g.append(f"T{t5}")
            g.append("M6")
            g.append(f"G97 S{params.get('bore_speed', 1000)} M3")
            g.append(f"(Tool T{t5}: boring bar, to D{d_bore:.2f} mm)")
            op("Bore1")
            d_safe = d_drill - 1.0
            g.append(f"G0 {X(d_safe / 2)} Z{z_top_b + clear:.3f}")
            g.append(f"G1 {X(d_bore / 2)} Z{z_top_b:.3f} {fmt(f_bo)}")
            g.append(f"G1 {X(d_bore / 2)} Z{z_bore_bot:.3f} {fmt(f_bo)}")
            g.append(f"G0 {X(d_safe / 2)} Z{z_bore_bot:.3f}")
            g.append(f"G0 {X(d_safe / 2)} Z{z_top_b + clear:.3f}")
            g.append("(Finish operation: Bore1)")


    # 3d. НАРЕЗАНИЕ РЕЗЬБЫ T6 — только по явно заданному обозначению
    #
    # Из модели резьбу не вывести: CAD несёт гладкий цилиндр наружного диаметра,
    # а шаг знает только чертёж. Поэтому участки лишь ДЕТЕКТИРУЮТСЯ и попадают
    # в отчёт, а режутся, если обозначение задано (LATHE_THREADS).
    n_thread = 0
    for th in (params.get("threads") or []):
        pitch = float(th["pitch"])
        d_maj = float(th.get("d") or th["d_major"])
        z_hi_t = float(th["z_from"])
        z_lo_t = float(th["z_to"])
        # глубина профиля метрической резьбы h = 0.6134·P (ГОСТ 24705)
        h = th.get("depth") or 0.6134 * pitch
        d_min_t = d_maj - 2 * h
        stepd = th.get("step", 0.1)          # съём за проход по ДИАМЕТРУ
        n_thread += 1
        if n_thread == 1:
            g.append(f"G0 {X(retract_r)} Z{z_top + clear:.3f}")
            g.append("M5")
            g.append(f"T{params.get('thread_tool_number', 6)}")
            g.append("M6")
            g.append(f"G97 S{params.get('thread_speed', 600)} M3")
            g.append(f"(Tool T{params.get('thread_tool_number', 6)}: "
                     f"threading insert)")
        op(f"Thread{n_thread}")
        g.append(f"(Thread M{d_maj:g}x{pitch:g}, depth {h:.3f} mm)")
        d_cur = d_maj - stepd
        while d_cur > d_min_t - 1e-9:
            g.append(f"G0 X{d_cur:.3f} Z{z_hi_t + clear:.3f}"
                     if dia else f"G0 X{d_cur / 2:.3f} Z{z_hi_t + clear:.3f}")
            # G33 — нарезание с шагом F (мм/об), синхронно со шпинделем
            g.append(f"G33 {'X%.3f' % d_cur if dia else 'X%.3f' % (d_cur / 2)} "
                     f"Z{z_lo_t:.3f} F{pitch:.3f}")
            g.append(f"G0 {X(retract_r)} Z{z_lo_t:.3f}")
            g.append(f"G0 {X(retract_r)} Z{z_hi_t + clear:.3f}")
            d_cur -= stepd
        g.append(f"(Finish operation: Thread{n_thread})")

    # 4. канавки и отрезка — КАНАВОЧНЫМ резцом T2
    #
    # Проходной резец сюда не лезет: он клин, и врезаясь вглубь волочит
    # вспомогательной кромкой по уже обточенной стенке позади (замерено в NX
    # ISV — так срезался шестигранник у 4-13A). Заводской техпроцесс делает
    # ровно это: канавки и отрезка отдельным канавочным резцом.
    n_groove = 0
    groove_cuts = []
    t2 = params.get("groove_tool_number", 2)
    want_partoff = params.get("partoff", True)
    t2_ready = []

    def ensure_t2():
        """Смена на канавочный — ЛЕНИВО, при первом настоящем использовании.

        Канавка может оказаться пустой (проходной резец добрал её сам), и
        эагерная смена оставляла в программе холостой блок T2 без единого хода.
        """
        if t2_ready:
            return
        t2_ready.append(True)
        g.append(f"G0 {X(retract_r)} Z{z_top + clear:.3f}")
        g.append("M5")            # смена инструмента — на остановленном шпинделе
        g.append(f"T{t2}")
        g.append("M6")
        g.append(f"G97 S{params.get('groove_speed', rpm)} M3")
        g.append(f"(Tool: grooving/parting blade W{blade:.2f} mm)")

    def plunge(z_c, r_target):
        """Врезание канавочным резцом в позиции z_c до радиуса r_target."""
        g.append(f"G0 {X(retract_r)} Z{z_c:.3f}")
        g.append(f"G1 {X(r_target)} Z{z_c:.3f} {fmt(feed_partoff)}")
        g.append(f"G0 {X(retract_r)} Z{z_c:.3f}")

    for gr in grooves:
        z_hi, z_lo = gr["z_hi"], gr["z_lo"]
        span = z_hi - z_lo
        # Позиции врезаний. Шаг мелкий (четверть пластины) не ради красоты:
        # глубина каждого врезания ограничена профилем под ВСЕЙ шириной
        # пластины, поэтому у крутой стенки резец вглубь не идёт, и попасть в
        # узкое дно можно только частой сеткой. Плюс отдельно ставится позиция
        # ПО САМОМУ ДНУ канавки — иначе при дне шириной с пластину её можно
        # проскочить и оставить канавку невыбранной.
        if span <= blade:
            zs_cut = [(z_hi + z_lo) / 2.0]
        else:
            step_cut = max(blade * 0.25, 0.1)
            n_cut = int((span - blade) / step_cut + 0.999) + 1
            zs_cut = [z_hi - blade / 2.0
                      - i * (span - blade) / (n_cut - 1) for i in range(n_cut)]
        # ЦЕНТР ДНА канавки. Не «первый минимум»: дно бывает шириной с пластину
        # (у 4-13A 1.55 мм при пластине 1.5), и попасть в него можно только
        # серединой — со сдвигом хоть на 0.1 мм пластина заденет стенку, и
        # ограничитель глубины не пустит резец вниз.
        samples = [(z_hi - k * span / 200.0) for k in range(201)]
        rr = [_r_at(part_profile, z) for z in samples]
        r_low = min(rr)
        flat = [z for z, r in zip(samples, rr) if r <= r_low + 0.02]
        z_deep = (max(flat) + min(flat)) / 2.0
        if all(abs(z_deep - z) > blade * 0.1 for z in zs_cut):
            zs_cut.append(z_deep)
            zs_cut.sort(reverse=True)
        cuts = []
        for z_c in zs_cut:
            # глубина ограничена профилем под ВСЕЙ пластиной: так резец не
            # срежет стенку канавки, даже если пластина шире дна
            lo, hi = z_c - blade / 2.0, z_c + blade / 2.0
            r_t = max(_max_r_between(part_profile, hi, lo),
                      _r_at(part_profile, hi), _r_at(part_profile, lo))
            # сравнивать надо с тем, что оставил проходной резец (огибающая),
            # а не с эквидистантой чистового прохода
            if r_t < _r_at(turn_profile, z_c) - 0.02:   # есть что снимать
                cuts.append((z_c, r_t))
        groove_cuts.append(cuts)
        if not cuts:
            continue
        n_groove += 1
        ensure_t2()
        op(f"Groove{n_groove}")
        for z_c, r_t in cuts:
            plunge(z_c, r_t)
        # ЧИСТОВОЙ КОНТУР канавки. Врезания оставляют стенки ступеньками — их
        # снимает проход УГЛОМ пластины: чтобы угол шёл по стенке, центр
        # пластины смещён на полширины В СТОРОНУ ДНА. Спускаемся по стенке со
        # стороны торца, идём по дну, поднимаемся по дальней стенке.
        prof_g = [(z, r) for z, r in part_raw
                  if z_lo - 1e-9 <= z <= z_hi + 1e-9]
        # ПО УМОЛЧАНИЮ ВЫКЛЮЧЕН: замерено, что контур чистит донья, но даёт
        # новый зарез на границе канавки (на 4-13A −2.55 мм у стенки к
        # шестиграннику и −0.34 у второй канавки). Причина не разобрана,
        # поэтому в дефолт не берём.
        if params.get("groove_contour") and len(prof_g) >= 3:
            r_low = min(r for _, r in prof_g)
            flat = [z for z, r in prof_g if r <= r_low + 0.02]
            z_mid = (max(flat) + min(flat)) / 2.0 if flat else (z_hi + z_lo) / 2
            contour = []
            for z, r in sorted(prof_g, key=lambda t: -t[0]):
                zc = z - blade / 2.0 if z > z_mid else z + blade / 2.0
                if z_lo - blade <= zc <= z_hi + blade:
                    contour.append((zc, r))
            if contour:
                g.append(f"G0 {X(retract_r)} Z{contour[0][0]:.3f}")
                g.append(f"G0 {X(contour[0][1])} Z{contour[0][0]:.3f}")
                for zc, r in contour[1:]:
                    g.append(f"G1 {X(r)} Z{zc:.3f} {fmt(feed_partoff)}")
                g.append(f"G0 {X(retract_r)} Z{contour[-1][0]:.3f}")
        g.append(f"(Finish operation: Groove{n_groove})")

    if want_partoff:
        ensure_t2()
        op("Partoff")
        # рез ВПЛОТНУЮ к торцу детали: правая грань пластины в z_end, значит её
        # середина — на полширины дальше. Прежний вариант (z_end − 3) резал
        # заведомо мимо детали, то есть не отрезал ничего.
        # partoff_z_ref — где на самом деле торец детали. В первом установе из
        # двух профиль обрезан по границе передачи работы, и z_end до торца не
        # доходит: резать надо по настоящему торцу, иначе отрежем полдетали.
        z_ref = params.get("partoff_z_ref")
        z_ref = z_end if z_ref is None else float(z_ref)
        z_cut = z_ref - (blade / 2.0 if blade else params.get("partoff_width", 3.0))
        g.append(f"G0 {X(retract_r)} Z{z_cut:.3f}")
        g.append(f"G1 X0.000 Z{z_cut:.3f} {fmt(feed_partoff)}")
        g.append(f"G0 {X(retract_r)} Z{z_cut:.3f}")
        g.append("(Finish operation: Partoff)")

    # Сколько осталось нетронутым: считаем по врезаниям канавочного, до какого
    # радиуса он реально добрался, и сравниваем с номиналом. Это честная строка
    # отчёта — лучше назвать остаток, чем сделать вид, что деталь готова.
    import math as _m
    uncut = sum(lz["volume_mm3"] for lz in second_setup)
    for gr, cuts in zip(grooves, groove_cuts):
        if not cuts:
            uncut += gr["volume_mm3"]
            continue
        reached = min(r for _, r in cuts)
        uncut += _m.pi * max(0.0, reached ** 2 - gr["r_min"] ** 2) *             (gr["z_hi"] - gr["z_lo"])

    g.append(f"G0 {X(retract_r)} Z{z_top + clear + 10:.3f}")
    g.append("M5")
    g.append("M2")

    stats = {"lines": len(g), "ops": ops, "rough_passes": n_rough,
             "stock_radius": r_stock, "profile_points": len(profile),
             "arcs": n_arcs, "grooves": n_groove, "blade": blade,
             "blade_tight": blade_tight,
             "groove_volume_mm3": sum(gr["volume_mm3"] for gr in grooves),
             "drills": n_drill, "threads": n_thread,
             "thread_candidates": find_thread_candidates(part_profile),
             "left_passes": n_left,
             "left_volume_mm3": sum(lz["volume_mm3"] for lz in doable),
             "second_setup": [(lz["z_hi"], lz["z_lo"], lz["volume_mm3"])
                              for lz in second_setup],
             "finish_tool": t_fin if fin_tool else 0,
             # насколько глубже припуска чистовому приходится резать в уступах,
             # куда черновой не заходит
             "finish_max_depth": (max((rr - r for (_, rr), (_, r)
                                       in zip(src_rough, src)), default=0.0)
                                  + allowance) if fin_tool else 0.0,
             "uncut_mm3": uncut}
    return g, stats


def write(prof_data, params, path):
    g, stats = generate(prof_data, params)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(g) + "\n")
    return stats
