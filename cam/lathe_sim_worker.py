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
    читается из шапки операции — `(Tool T4: twist drill D10.00 mm)`. Диаметр
    держится ПОСВЕРЛЁННО: центровочное Ø3.15 идёт перед спиральным Ø10, и один
    общий диаметр приписал бы центровке чужие 10 мм.

    Возвращает (moves, inner, d_drill), где d_drill — наибольший (для лога).
    """
    import math
    moves, inner = [], []
    d_drill = 0.0
    d_cur = 0.0
    op = ""

    def push(z1, r1, z2, r2):
        if op.startswith(("Drill", "Bore")):
            # сверло идёт по оси и режет своим радиусом, а не точкой
            inner.append((z1, d_cur / 2.0 if r1 < 1e-6 else r1,
                          z2, d_cur / 2.0 if r2 < 1e-6 else r2))
        else:
            moves.append((z1, r1, z2, r2))

    x = z = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            mo = re.search(r"\(Begin operation: (\S+?)\)", line)
            if mo:
                op = mo.group(1)
            md = re.search(r"(?:twist|center) drill D([\d.]+)", line)
            if md:
                d_cur = float(md.group(1))
                d_drill = max(d_drill, d_cur)
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
                        push(pz, px, cz, cx)
                        pz, px = cz, cx
                else:
                    push(z, x, nz, nx)
            x, z = nx, nz
    return moves, inner, d_drill


def inner_envelope(moves, grid):
    """Максимальный радиус, выбранный ВНУТРЕННИМИ операциями, по сетке z.

    Радиус сверла уже подставлен в движения при разборе (parse_moves.push):
    сверло режет своим диаметром, расточной идёт кромкой.
    """
    reached = [0.0] * len(grid)
    for z1, r1, z2, r2 in moves:
        lo, hi = (z1, z2) if z1 <= z2 else (z2, z1)
        for i, z in enumerate(grid):
            if lo - 1e-9 <= z <= hi + 1e-9:
                t = 0.0 if abs(z2 - z1) < 1e-12 else (z - z1) / (z2 - z1)
                reached[i] = max(reached[i], r1 + (r2 - r1) * t)
    return reached


def sample(profile, grid, default):
    """Профиль [(z, r)] → значения по возрастающей сетке z.

    Вне профиля — `default`. Для заготовки следующего установа это НЕ радиус
    прутка, а «материала нет»: профиль предыдущего установа кончается там, где
    его отрезали и подрезали, и за этими границами тела уже не существует.
    """
    pts = sorted((float(z), float(r)) for z, r in profile)
    if not pts:
        return [default] * len(grid)
    out, k = [], 0
    for z in grid:
        if z < pts[0][0] - 1e-9 or z > pts[-1][0] + 1e-9:
            out.append(default)
            continue
        while k + 1 < len(pts) and pts[k + 1][0] < z:
            k += 1
        z1, r1 = pts[k]
        z2, r2 = pts[min(k + 1, len(pts) - 1)]
        t = 0.0 if abs(z2 - z1) < 1e-12 else (z - z1) / (z2 - z1)
        out.append(r1 + (r2 - r1) * max(0.0, min(1.0, t)))
    return out


def envelope(moves, grid, seed):
    """Минимальный радиус, достигнутый резцом, по сетке z.

    seed — с чего начинаем: у первого установа это радиус прутка по всей длине,
    у второго — профиль, оставшийся от первого (склейка установов честная, а не
    «второй режет по целому прутку»).
    """
    n = len(grid)
    z_lo = grid[0]
    step = (grid[-1] - grid[0]) / (n - 1) if n > 1 else 1.0
    reached = list(seed)

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
    return reached


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

    # ВТОРОЙ УСТАНОВ: программа написана в своей СК (деталь перевёрнута), а
    # считаем мы всё в исходной — иначе результат не наложить ни на первый
    # установ, ни на модель. z' = z_mirror − z, и это ровно та же формула, что
    # в lathe_setups.mirror, только в обратную сторону.
    if p.get("z_mirror") is not None:
        m = float(p["z_mirror"])
        moves = [(m - z1, r1, m - z2, r2) for z1, r1, z2, r2 in moves]
        inner = [(m - z1, r1, m - z2, r2) for z1, r1, z2, r2 in inner]
        log(f"программа второго установа развёрнута в исходную СК (z' = {m:g} − z)")

    r_eps = p.get("min_radius", 0.05)
    n_grid = int(round((z_hi - z_lo) / step)) + 1
    grid = [z_lo + i * step for i in range(n_grid)]
    # Заготовка второго установа — то, что осталось от первого; ЗА пределами
    # его профиля материала нет (отрезано), поэтому вне диапазона r_eps.
    seed = (sample(p["stock_profile"], grid, r_eps) if p.get("stock_profile")
            else [r_stock] * n_grid)
    reached = envelope(moves, grid, seed)
    cut = sum(1 for r, s in zip(reached, seed) if r < s - 1e-6)
    log(f"сетка {len(grid)} точек шагом {step} мм, резец коснулся {cut} из них")
    # ОТРЕЗКА И ПОДРЕЗКА РАЗБИВАЮТ ТЕЛО. Там, где резец дошёл до оси, материала
    # больше нет: ниже отрезного реза остаётся хвост прутка, выше подрезки —
    # ничего. Самый длинный сплошной кусок — это и есть деталь.
    #
    # Обрезать по нему САМО ТЕЛО нельзя. У краёв куска радиус лишь чуть больше
    # r_eps, торцевая грань выходит слиэром в сотую миллиметра, и тесселяция
    # такого тела врёт у оси: воксельная сверка показывала фантомный зарез
    # 163 мм³ там, где по профилям съёма нет вовсе (сам профиль результата в той
    # зоне ВЫШЕ номинала — проверено поточечно). Поэтому тело строится по всей
    # сетке, как раньше, а кусок нужен только для профиля, который получит
    # СЛЕДУЮЩИЙ установ: иначе он унаследовал бы отрезанный хвост прутка как
    # заготовку и точил бы по воздуху.
    runs_solid, i = [], 0
    while i < len(grid):
        if reached[i] <= r_eps + 1e-6:
            i += 1
            continue
        j = i
        while j < len(grid) and reached[j] > r_eps + 1e-6:
            j += 1
        runs_solid.append((i, j))
        i = j
    if not runs_solid:
        raise RuntimeError("после программы не осталось материала")
    i0, i1 = max(runs_solid, key=lambda s: s[1] - s[0])
    if len(runs_solid) > 1:
        log(f"тело разрезано на {len(runs_solid)} части (отрезка/подрезка) — "
            f"деталь: z {grid[i0]:.2f}..{grid[i1 - 1]:.2f}")

    # Профиль результата → замкнутый контур в плоскости XZ → вращение вокруг Z.
    # Радиус зажимаем снизу: контур, КАСАЮЩИЙСЯ оси (после отрезки резец доходит
    # до X0), при вращении даёт самопересечение и невалидное тело.
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
    # изнутри, наружная огибающая о них ничего не знает.
    #
    # Тело отверстия строится СТОПКОЙ ЦИЛИНДРОВ, а не вращением контура.
    # Вращение давало у отверстия внутреннюю стенку радиусом r_eps — ровно
    # такую же, как у наружного результата (он тоже клампится r_eps у оси).
    # Две совпадающие цилиндрические грани на оси — вырожденный случай для
    # булевой операции OCCT: `cut` возвращался БЕЗ ошибки и почти без съёма
    # (на 22-13A вычиталось 1045 мм³ вместо 17000). У цельного цилиндра
    # стенки на оси нет, и вырождение исчезает.
    r_in = inner_envelope(inner, grid)
    if p.get("stock_bore"):        # отверстие, доставшееся от прошлого установа
        prev_in = sample(p["stock_bore"], grid, 0.0)
        r_in = [max(a, b) for a, b in zip(r_in, prev_in)]
    if any(r > r_eps for r in r_in):
        # ступени постоянного радиуса: отверстие — это сверло Ø d и расточка Ø D,
        # то есть единицы цилиндров, а не тысяча точек профиля
        runs = []                                    # [z_low, z_high, r]
        for z, r in zip(grid, r_in):
            if r <= r_eps:
                continue
            rq = round(r / 0.01) * 0.01
            if runs and abs(runs[-1][2] - rq) < 1e-9 and z - runs[-1][1] < 1.5 * step:
                runs[-1][1] = z
            else:
                runs.append([z, z, rq])
        if runs:
            # сверло входит СВЕРХУ: всё, что выше самой верхней просверленной
            # точки, оно уже прошло насквозь — верхнюю ступень продлеваем за торец
            runs[-1][1] = max(runs[-1][1], z_hi) + 1.0
            if runs[0][0] <= z_lo + step:            # сквозное — продлить и вниз
                runs[0][0] -= 1.0
            try:
                bore_solid = None
                for z0, z1, r in runs:
                    h = (z1 - z0) + step             # перекрытие соседних ступеней
                    cyl = Part.makeCylinder(r, h, App.Vector(0, 0, z0 - step / 2))
                    bore_solid = cyl if bore_solid is None else bore_solid.fuse(cyl)
                v0 = solid.Volume
                solid = solid.cut(bore_solid)
                log(f"осевое отверстие вычтено: Ø{2 * max(r[2] for r in runs):.2f} "
                    f"x {runs[-1][1] - runs[0][0]:.2f} мм, ступеней {len(runs)}, "
                    f"снято {v0 - solid.Volume:.1f} мм³, "
                    f"объём стал {solid.Volume:.1f} мм³")
            except Exception as e:
                log(f"warn: отверстие не вычлось: {e}")

    solid.exportStep(p["out_step"])
    # Профили результата — чтобы следующий установ начинал с того, что реально
    # осталось, а не с целого прутка.
    if p.get("out_json"):
        with open(p["out_json"], "w", encoding="utf-8") as f:
            json.dump({"profile": [[z, r] for z, r
                                   in zip(grid[i0:i1], reached[i0:i1])],
                       "bore": [[z, r] for z, r in zip(grid[i0:i1], r_in[i0:i1])],
                       "volume": solid.Volume}, f)
    log(f"OK volume={solid.Volume:.1f} step={p['out_step']}")


if os.environ.get("FREECAD_LATHE_SIM_PARAMS"):
    main()
