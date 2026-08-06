#!/usr/bin/env python3
"""
lathe_diff_worker.py — исполняется ВНУТРИ FreeCAD (freecadcmd), не в обычном Python.

ОСЕВОЙ анализ «деталь vs результат точения». Отличие от `freecad_diff_worker.py`
(фрезеровка) — не в методе, а в раскладке: тела вращения дефект дают не пятнами,
а ПОЯСАМИ по z, поэтому суммарный объём ничего не объясняет, а раскладка по z
объясняет всё.

Считает две вещи, независимые друг от друга:

1. ВОКСЕЛЬНЫЙ рей-кастинг — тот же движок, что во фрезерном пайплайне
   (`ZCaster` импортируется, а не копируется), но ячейки суммируются в пояса по z.
   Ловит и неосесимметричное (грани под ключ), объём меряет честно.

2. ПРОФИЛЬ по осевым сечениям — r(z) обоих тел под несколькими углами.
   Точность здесь микронная (сечение точное, не сетка), поэтому смещения
   порядка радиуса при вершине резца (0.4 мм) видны, тогда как воксель с шагом
   0.25 мм их размазывает. По углам берётся max и min: там, где они совпадают,
   тело осесимметрично и разность радиусов — честная ошибка обработки; где
   расходятся, это грани под ключ (их точением не берут).

Параметры (env LATHE_DIFF_PARAMS, JSON): part, result, json_path,
pitch (мм, 0 = авто), z_bin (мм), angles (сколько сечений на 90°), prof_step (мм).
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

import FreeCAD as App
import Part

# ZCaster и фильтр тонких плёнок берутся из фрезерного воркера — метод один и
# тот же, и расходиться двум реализациям нельзя. Хост кладёт оба файла рядом
# (в TEMP при кириллице).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from freecad_diff_worker import ZCaster, load_solid, thick_runs   # noqa: E402


def log(msg):
    print(f"[ldiff] {msg}", flush=True)


def radial_profile(shape, z_lo, z_hi, step, angles):
    """r(z) тела под несколькими углами → (grid, rmax, rmin).

    Сечение плоскостью через ось под углом θ, берётся ПОЛУПЛОСКОСТЬ u >= 0
    (u = x·cosθ + y·sinθ) — как в `lathe_worker.profile_from_section`, только
    углов несколько: одно сечение на шестиграннике покажет либо грань, либо
    ребро, и по одному не понять, осесимметрично ли тело.

    rmax — наибольший радиус по углам (что должен снять резец), rmin —
    наименьший (на шестиграннике это «под ключ»).
    """
    bb = shape.BoundBox
    R = max(bb.XLength, bb.YLength) * 2 + 10
    z0, z1 = z_lo - 5, z_hi + 5
    n = max(2, int(round((z_hi - z_lo) / step)) + 1)
    grid = [z_lo + i * (z_hi - z_lo) / (n - 1) for i in range(n)]
    per_angle = []
    for k in range(angles):
        th = math.pi / 2.0 * k / angles
        c, s = math.cos(th), math.sin(th)
        # ПОЛУплоскость строится явным прямоугольником (0..R по радиусу):
        # makePlane положил бы её симметрично относительно оси, и «правую
        # половину» пришлось бы вырезать отдельно
        half = Part.Face(Part.makePolygon([
            App.Vector(0, 0, z0), App.Vector(R * c, R * s, z0),
            App.Vector(R * c, R * s, z1), App.Vector(0, 0, z1),
            App.Vector(0, 0, z0)]))
        sec = shape.common(half)
        if not sec.Edges:
            continue
        pts = []
        for e in sec.Edges:
            m = max(2, int(e.Length / 0.02))
            for p in e.discretize(Number=min(m, 6000)):
                u = p.x * c + p.y * s
                if u >= -1e-6:
                    pts.append((p.z, abs(u)))
        if not pts:
            continue
        pts.sort()
        pz = np.array([q[0] for q in pts])
        pr = np.array([q[1] for q in pts])
        col = np.full(len(grid), np.nan)
        for i, z in enumerate(grid):
            m = np.abs(pz - z) <= step * 0.75
            if m.any():
                col[i] = pr[m].max()
        per_angle.append(col)
    if not per_angle:
        raise RuntimeError("осевое сечение пустое — тело не пересекает ось")
    A = np.vstack(per_angle)
    with np.errstate(invalid="ignore"):
        rmax = np.nanmax(A, axis=0)
        rmin = np.nanmin(A, axis=0)
    return grid, rmax, rmin


def voxel_by_z(part, result, pitch, z_bin, min_thick=0.0,
               z_limit=None, z_limit_bore=None, bore_radius=0.0):
    """Воксельный диф с раскладкой по поясам z.

    Классификация ячеек — ровно как во фрезерном движке (`freecad_diff_worker`),
    включая обе его поправки; отличается только суммирование: не в зоны-пятна,
    а в пояса по z.

    ВАЖНО про допуск по толщине. `thick_runs` режет короткие отрезки ВДОЛЬ ЛУЧА,
    то есть вдоль оси детали, — значит для точения он гасит тонкие ТОРЦЕВЫЕ
    расхождения (подрезка торца, дно канавки) и не трогает тонкие ЦИЛИНДРИЧЕСКИЕ
    плёнки: у кольца в сотые доли миллиметра по радиусу отрезок вдоль z длиной со
    всю обточенную поверхность. Радиальный допуск здесь недостижим в принципе —
    его даёт профильная половина анализатора (dr(z), микроны), см. `bands(tol=)`.

    ЗОНА ОТВЕТСТВЕННОСТИ УСТАНОВА. Когда сравнивается результат ОДНОГО установа,
    деталь целиком эталоном быть не может: всё, что оставлено следующему
    установу, иначе идёт в недорез и забивает счёт (на 14-31A это 10.7 тыс. мм³
    против 0.3 тыс. настоящего дефекта). Поэтому проверка режется по z:

      * `z_limit`      — докуда установ точил НАРУЖНУЮ поверхность;
      * `z_limit_bore` — докуда он растачивал отверстие (обычно глубже);
      * `bore_radius`  — радиус отверстия: колонки внутри него живут по второй
                         границе, снаружи — по первой.

    Ниже своей границы ячейка не считается ни недорезом, ни зарезом: там либо
    нетронутый пруток, либо намеренно недоделанное отверстие.

    Возвращает (bins, tot_u, tot_o, thin, checked, skipped) — два последних это
    объём детали внутри и вне проверяемой зоны.
    """
    pb, rb = part.BoundBox, result.BoundBox
    cast_p = ZCaster(part, 0.05)
    cast_r = ZCaster(result, 0.05)
    # Сетка поднимается до верха РЕЗУЛЬТАТА: над деталью стоит припуск заготовки
    # под подрезку торца, и недоснятый торец обязан попасть в счёт. ВНИЗ сетка
    # намеренно НЕ растёт — там пенёк от отрезки и остаток прутка, они к детали
    # не относятся (у фрезеровки там стол, причина другая, правило то же).
    z_top = max(pb.ZMax, rb.ZMax)
    nx = max(1, int(math.ceil(pb.XLength / pitch)))
    ny = max(1, int(math.ceil(pb.YLength / pitch)))
    nz = max(1, int(math.ceil((z_top - pb.ZMin) / pitch)))
    cell = pitch ** 3
    log(f"сетка {nx}x{ny}x{nz} (шаг {pitch:.2f} мм, {nx * ny * nz} ячеек), "
        f"z {pb.ZMin:.1f}..{z_top:.1f} (верх детали {pb.ZMax:.1f})")

    zc = pb.ZMin + (np.arange(nz) + 0.5) * pitch
    below_top = zc <= pb.ZMax
    run_len = max(1, int(math.ceil(min_thick / pitch - 1e-9)))
    if run_len > 1:
        log(f"порог толщины дефекта {min_thick} мм = {run_len} ячеек подряд "
            f"(вдоль оси; по радиусу его даёт профиль)")
    else:
        log(f"фильтр толщины неактивен: {min_thick} мм ≤ шага {pitch} мм")
    ib = np.floor((zc - pb.ZMin) / z_bin).astype(int)
    nb = int(ib.max()) + 1
    under = np.zeros(nb)
    over = np.zeros(nb)
    thin = checked = skipped = 0.0
    lim_out = -1e18 if z_limit is None else float(z_limit)
    lim_in = lim_out if z_limit_bore is None else float(z_limit_bore)
    keep_out = zc >= lim_out - 1e-9
    keep_in = zc >= lim_in - 1e-9
    r_bore = float(bore_radius or 0.0)
    if z_limit is not None:
        log(f"зона проверки: z ≥ {lim_out:.2f}" + (
            f", в отверстии (r ≤ {r_bore:.2f}) z ≥ {lim_in:.2f}"
            if r_bore > 0 and lim_in != lim_out else ""))
    jx, jy = 0.5 + 1.7e-3, 0.5 + 2.3e-3
    for iy in range(ny):
        y = pb.YMin + (iy + jy) * pitch
        for ix in range(nx):
            x = pb.XMin + (ix + jx) * pitch
            in_p = cast_p.inside(x, y, zc)
            in_r = cast_r.inside(x, y, zc)
            if not in_p.any():
                # В колонке нет тела детали: либо осевое/сквозное отверстие (его
                # считаем — непрорезанная дыра есть недорез), либо пруток мимо
                # габарита детали (не считаем). Различаем по высоте.
                in_r = in_r & below_top
            keep = (keep_in if (r_bore > 0 and math.hypot(x, y) <= r_bore)
                    else keep_out)
            checked += int((in_p & keep).sum()) * cell
            skipped += int((in_p & ~keep).sum()) * cell
            u_raw, o_raw = (in_r & ~in_p) & keep, (in_p & ~in_r) & keep
            # длина отрезка считается ДО обрезки по границе, иначе отсечённый
            # ею хвост длинного дефекта выглядел бы тонкой плёнкой
            u = thick_runs(in_r & ~in_p, run_len) & keep
            o = thick_runs(in_p & ~in_r, run_len) & keep
            thin += int(u_raw.sum() - u.sum() + o_raw.sum() - o.sum()) * cell
            if u.any():
                np.add.at(under, ib[u], cell)
            if o.any():
                np.add.at(over, ib[o], cell)
    bins = [{"z0": round(pb.ZMin + i * z_bin, 2),
             "z1": round(pb.ZMin + (i + 1) * z_bin, 2),
             "under_mm3": round(under[i], 2),
             "over_mm3": round(over[i], 2)} for i in range(nb)]
    return (bins, float(under.sum()), float(over.sum()), thin,
            checked, skipped)


def enrich_bins(bins, grid, prm, prn, z_limit=None, bore_radius=0.0):
    """Объём пояса → ОТКЛОНЕНИЕ ПО РАДИУСУ. Для тела вращения пересчёт точный:
    слой толщиной dr на радиусе r и длине L занимает 2πr·L·dr, откуда
    dr = V / (2πrL). Номинальный радиус берётся из профиля ДЕТАЛИ — он снимается
    всегда, потому что деталь настоящий солид (на фасетном результате сечение
    падает, см. main). Так радиальная величина получается и без профиля
    результата — а именно она и нужна: 13 мм³ в поясе ничего не говорят, 0.19 мм
    по радиусу говорят всё.

    Пересчёт верен только на осесимметричной боковой поверхности, поэтому пояс
    помечается:
      * `axisym=False` — грани под ключ, точением не берутся (законный недорез);
      * `face=True`    — номинальный радиус в поясе меняется быстрее, чем на
                         45°, то есть это торец или уступ: там расхождение
                         осевое, и делить его на окружность бессмысленно.
    """
    if not grid:
        return
    gz = np.asarray(grid)
    for b in bins:
        # Ниже наружной границы установа проверяется ТОЛЬКО отверстие, поэтому
        # и радиус для пересчёта там внутренний: делить съём в отверстии на
        # длину наружной окружности — бессмыслица.
        if (z_limit is not None and bore_radius
                and b["z1"] <= float(z_limit) + 1e-9):
            b["r_nom"] = round(float(bore_radius), 3)
            b["zone"] = "отверстие"
            k = 2.0 * math.pi * float(bore_radius) * abs(b["z1"] - b["z0"])
            if k > 1e-9:
                b["dr_under_mm"] = round(b["under_mm3"] / k, 3)
                b["dr_over_mm"] = round(-b["over_mm3"] / k, 3)
            continue
        m = (gz >= b["z0"]) & (gz < b["z1"])
        if not m.any():
            continue
        rn = prm[m]
        rn = rn[~np.isnan(rn)]
        if rn.size == 0 or rn.max() < 0.5:
            continue
        r_nom = float(rn.mean())
        b["r_nom"] = round(r_nom, 3)
        # осесимметричность считается ПО СЕЧЕНИЯМ (rmax − rmin в одном и том же
        # z), а не по разбросу вдоль пояса: на конусе радиус меняется от z к z,
        # и разброс вдоль пояса ничего не говорит про грани под ключ
        with np.errstate(invalid="ignore"):
            spread = np.nanmax(prm[m] - prn[m])
        b["axisym"] = bool(not np.isnan(spread) and spread < 0.02)
        dz = abs(b["z1"] - b["z0"])
        b["face"] = bool((rn.max() - rn.min()) > dz)
        k = 2.0 * math.pi * r_nom * dz
        if k > 1e-9:
            b["dr_under_mm"] = round(b["under_mm3"] / k, 3)
            b["dr_over_mm"] = round(-b["over_mm3"] / k, 3)


def main():
    with open(os.environ["LATHE_DIFF_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)

    part = load_solid(p["part"])
    result = load_solid(p["result"])
    pb, rb = part.BoundBox, result.BoundBox
    log(f"деталь {abs(part.Volume) / 1000.0:.2f} см³, результат "
        f"{abs(result.Volume) / 1000.0:.2f} см³")
    if max(abs(pb.Center.x), abs(pb.Center.y)) > 0.2:
        log(f"ВНИМАНИЕ: ось детали не в x=y=0 (центр {pb.Center.x:.2f},"
            f" {pb.Center.y:.2f}) — радиусы будут неверны")

    z_lo, z_hi = pb.ZMin, pb.ZMax
    step = float(p.get("prof_step", 0.05))
    angles = int(p.get("angles", 6))
    prof, prof_note = [], None
    grid, prm, prn, rrm, rrn = [], None, None, None, None
    # Профиль ДЕТАЛИ снимается отдельно от профиля результата и почти всегда
    # успешно: деталь — настоящий солид. Он нужен не только для dr(z), но и как
    # номинал для пересчёта поясов в радиус (enrich_bins), поэтому терять его
    # из-за фасетного результата нельзя.
    try:
        grid, prm, prn = radial_profile(part, z_lo, z_hi, step, angles)
        log(f"профиль детали снят: {len(grid)} точек, шаг {step} мм, "
            f"{angles} сечений")
    except Exception as exc:
        prof_note = f"профиль детали не снят: {type(exc).__name__}: {exc}"
        log(prof_note)
    if grid:
        try:
            _, rrm, rrn = radial_profile(result, z_lo, z_hi, step, angles)
        except Exception as exc:
            # Осевое сечение — булева операция, а на ФАСЕТНОМ теле (IPW из NX
            # ISV) OCCT её не делает: `common` возвращает Null shape. Это не
            # повод терять замер: воксели работают на любом теле, а радиальную
            # величину даёт пересчёт поясов через номинал.
            prof_note = (f"профиль результата не снят: "
                         f"{type(exc).__name__}: {exc}")
            log(prof_note + " — радиус считаю пересчётом поясов")
    if rrm is None:
        grid_rows = []
    else:
        grid_rows = list(enumerate(grid))
    z_limit = p.get("z_limit")
    z_limit_bore = p.get("z_limit_bore")
    bore_radius = float(p.get("bore_radius") or 0.0)
    for i, z in grid_rows:
        # профиль — это НАРУЖНАЯ поверхность, поэтому режется наружной границей:
        # ниже неё стоит пруток, и dr там был бы «недорезом» в миллиметры
        if z_limit is not None and z < float(z_limit) - 1e-9:
            continue
        row = {"z": round(z, 3)}
        for key, arr in (("part_rmax", prm), ("part_rmin", prn),
                         ("res_rmax", rrm), ("res_rmin", rrn)):
            row[key] = None if np.isnan(arr[i]) else round(float(arr[i]), 4)
        if row["part_rmax"] is not None and row["res_rmax"] is not None:
            row["dr"] = round(row["res_rmax"] - row["part_rmax"], 4)
            # осесимметричен ли НОМИНАЛ в этом сечении: если нет — это грани
            # под ключ, их точением не берут и в ошибку записывать нельзя
            row["axisym"] = bool(row["part_rmax"] - row["part_rmin"] < 0.02)
        prof.append(row)

    pitch = float(p.get("pitch", 0)) or 0.25
    z_bin = float(p.get("z_bin", 1.0))
    min_thick = float(p.get("min_thickness", 0.0))
    bins, tot_u, tot_o, thin, checked, skipped = voxel_by_z(
        part, result, pitch, z_bin, min_thick,
        z_limit, z_limit_bore, bore_radius)
    enrich_bins(bins, grid, prm, prn, z_limit, bore_radius)

    data = {
        "method": (f"воксельный рей-кастинг, шаг {pitch:.2f} мм, пояса по "
                   f"{z_bin:g} мм; профиль по {angles} осевым сечениям, "
                   f"шаг {step:g} мм"),
        "part_volume_mm3": round(abs(part.Volume), 1),
        "result_volume_mm3": round(abs(result.Volume), 1),
        "z_range": [round(z_lo, 2), round(z_hi, 2)],
        "z_limit": z_limit,
        "z_limit_bore": z_limit_bore,
        "bore_radius": bore_radius or None,
        "checked_volume_mm3": round(checked, 1),
        "skipped_volume_mm3": round(skipped, 1),
        "min_thickness_mm": min_thick,
        "thin_film_mm3": round(thin, 1),
        "undercut_total_mm3": round(tot_u, 1),
        "overcut_total_mm3": round(tot_o, 1),
        "by_z": bins,
        "profile": prof,
        "profile_note": prof_note,
        "note": "dr = r_результата − r_детали по наибольшему радиусу: >0 недорез, "
                "<0 зарез. axisym=false — сечение неосесимметрично (грани под "
                "ключ), точение там ни при чём. thin_film — отсечено фильтром "
                "толщины ВДОЛЬ ОСИ; радиальный допуск даёт только профиль.",
    }
    with open(p["json_path"], "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    log(f"OK недорез={tot_u:.1f} зарез={tot_o:.1f} плёнка={thin:.1f} "
        f"json={p['json_path']}")


if os.environ.get("LATHE_DIFF_PARAMS"):
    try:
        main()
    except Exception:
        import traceback
        for _line in traceback.format_exc().splitlines():
            log(_line)
        raise
