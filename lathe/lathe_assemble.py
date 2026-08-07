#!/usr/bin/env python3
"""Итоговая деталь двух установов = ПЕРЕСЕЧЕНИЕ двух результатов ISV.

Почему пересечение, а не сцепка. Каждый установ гоняется в ISV по СВОЕМУ
чистому прутку и обрабатывает свою половину, чужую оставляя нетронутой.
Физически деталь — это то, что срезано и тем, и другим, то есть пересечение
двух тел. Подавать результат первого установа заготовкой во второй НЕЛЬЗЯ:
замерено (runs/87), что фасетная заготовка стоит лишних 0.15 мм по радиусу
плюс конусность — механизм не найден, поэтому обходим.

Пересечение считается ПО ПРОФИЛЯМ — для тел вращения это точно:

    наружный   r(z) = min(r₁(z), r₂(z))
    отверстие  r(z) = max(r₁(z), r₂(z))

Лыски шестигранника при этом не теряются — их и нет: точением они не делаются
ни в модели, ни в результате.

ПРОФИЛЬ СНИМАЕТСЯ СЕЧЕНИЕМ ТРЕУГОЛЬНИКОВ, а не по кольцам вершин фасета.
Разница принципиальная и стоила отдельного разбора (runs/97): у отверстия
второго установа между устьем (z −45.2, r 5.825) и вершиной сверла (z −17.0,
r 0.0) лежат 28 мм БЕЗ ЕДИНОГО КОЛЬЦА — стенка там одна длинная грань.
Интерполяция между соседними кольцами выдумала на этом месте сходящий на конус
канал, и в сверке это легло 700 мм³ недореза, которого нет. Держать значение
через разрыв тоже нельзя: под тот же признак попадают настоящие фаски 45°
длиной около миллиметра. Плоскость z = const пересекает рёбра треугольников
там, где поверхность реально проходит, — и вопрос закрыт.

Побочно это даёт правильные концы: где треугольников нет, материала НЕТ (а не
«нет данных»), и итоговое тело честно короче модели там, где ISV срезал торец
глубже программы.

ГРАБЛИ, каждая стоила отдельного прогона (runs/91…96):

1. ЗНАК переворота. СК второго установа: z' = −z + z_end. С «−z − z_end»
   заготовка уезжает на 92 мм в сторону (runs/91).
2. РАЗДЕЛЕНИЕ КОНТУРОВ. Наружную поверхность от стенки отверстия отделяет
   порог `r_split`; без него профиль схлопывается на тех z, где у наружной
   поверхности нет вершин (runs/92, объём 16712 вместо 28000).
3. ОТВЕРСТИЕ РАЗНОЕ ПО ДЛИНЕ (расточено, просверлено, дальше металл) — берётся
   профилем, а не одним радиусом (runs/93).
"""
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cam import freecad_cam                              # noqa: E402


def assemble(step1, step2, out_step, z_end, r_split=0.0, z_from=0.0,
             step=0.05, profile_json=None, timeout=900):
    """Собирает итоговую деталь двух установов в СОЛИД `out_step`.

    step1 — результат установа 1 (рама детали), step2 — установа 2 (своя рама,
    переводится сюда же переворотом z = −z' + z_end; преобразование —
    инволюция, поэтому формула одна на оба направления).

    Возвращает {"step", "volume", "z_range", "profile", "bore", "closed"}.
    """
    fc = freecad_cam.find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден (укажите FREECAD_CMD в конфиге)")
    for f in (step1, step2):
        if not os.path.exists(f):
            raise RuntimeError(f"нет результата симуляции: {f}")

    tdir = tempfile.gettempdir()
    pid = os.getpid()
    # входы и выход — через ASCII-temp: OCCT не открывает пути с кириллицей,
    # а 8.3-имя обрезает «.step» до «.STE» и формат перестаёт узнаваться
    ins = []
    for i, src in enumerate((step1, step2), 1):
        dst = os.path.join(tdir, f"lathe_itog_in{i}_{pid}.stp")
        with open(src, "rb") as a, open(dst, "wb") as b:
            b.write(a.read())
        ins.append(dst)
    tmp_out = os.path.join(tdir, f"lathe_itog_{pid}.step")
    tmp_prof = os.path.join(tdir, f"lathe_itog_prof_{pid}.json")

    pp = os.path.join(tdir, f"lathe_itog_params_{pid}.json")
    with open(pp, "w", encoding="utf-8") as f:
        json.dump({"step1": ins[0], "step2": ins[1], "z_end": float(z_end),
                   "r_split": float(r_split), "z_from": float(z_from),
                   "step": float(step), "out": tmp_out, "prof": tmp_prof}, f)

    worker = os.path.join(tdir, f"_lathe_itog_worker_{pid}.py")
    with open(worker, "w", encoding="utf-8") as f:
        f.write(_WORKER)
    try:
        proc = subprocess.run(
            [fc, worker], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env={**os.environ, "LATHE_ITOG_PARAMS": pp,
                 "QT_QPA_PLATFORM": "offscreen"}, timeout=timeout)
    finally:
        for f in ins + [pp, worker]:
            try:
                os.unlink(f)
            except OSError:
                pass

    lines = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    for l in lines:
        if l.startswith("[itog]") and " OK " not in l:
            print("   " + l)
    mark = next((l for l in lines if "[itog] OK" in l), None)
    if not mark or not os.path.exists(tmp_out):
        tail = "\n".join(l for l in lines if "[itog]" in l or "rror" in l)[-800:]
        raise RuntimeError(f"итоговая деталь не построилась "
                           f"(код {proc.returncode}). {tail}")

    os.makedirs(os.path.dirname(os.path.abspath(out_step)) or ".", exist_ok=True)
    with open(tmp_out, "rb") as src, open(out_step, "wb") as dst:
        dst.write(src.read())
    os.unlink(tmp_out)
    with open(tmp_prof, encoding="utf-8") as f:
        prof = json.load(f)
    if profile_json:
        with open(profile_json, "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False)
    os.unlink(tmp_prof)

    return {"step": out_step,
            "volume": float(re.search(r"volume=([\d.]+)", mark).group(1)),
            "closed": "closed=True" in mark,
            "z_range": (prof["outer"][0][0], prof["outer"][-1][0]),
            "profile": prof["outer"], "bore": prof["bore"]}


# ── воркер: исполняется ВНУТРИ FreeCAD (freecadcmd) ─────────────────────────
# Тут и снятие профилей, и пересечение, и проворот — чтобы фасетные тела
# читались один раз и не гонялись между процессами.
_WORKER = r'''
import json, os, math, sys
import numpy as np
import FreeCAD as App, Part

# stdout внутри freecadcmd — cp1251: без этого print с кириллицей приезжает
# в хост крокозябрами (а с «Ø» и вовсе убивает воркер UnicodeEncodeError)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

P = json.load(open(os.environ["LATHE_ITOG_PARAMS"], encoding="utf-8"))


def load(path, z_end=None):
    """Фасетное тело → треугольники в координатах (r, z) РАМЫ ДЕТАЛИ."""
    sh = Part.Shape()
    sh.read(path)
    v, f = sh.tessellate(0.01)
    R = np.hypot(np.array([p.x for p in v]), np.array([p.y for p in v]))
    Z = np.array([p.z for p in v], dtype=float)
    if z_end is not None:                       # результат установа 2
        Z = -Z + float(z_end)
    T = np.asarray(f, dtype=np.int64)
    RT, ZT = R[T], Z[T]
    print("[itog] %s: %d треугольников, z %.3f..%.3f"
          % (os.path.basename(path), len(T), Z.min(), Z.max()))
    return RT, ZT, ZT.min(axis=1), ZT.max(axis=1)


def section(body, z, r_split):
    """Сечение плоскостью z = const → (наружный радиус, радиус отверстия).

    Пересекаются РЁБРА треугольников — там, где поверхность реально проходит.
    Ноль наружного значит «материала на этом z нет», а не «нет данных».
    """
    RT, ZT, zlo, zhi = body
    m = (zlo <= z) & (zhi >= z)
    if not m.any():
        return 0.0, 0.0
    r, zz = RT[m], ZT[m]
    hits = []
    for a, b in ((0, 1), (1, 2), (2, 0)):
        z0, z1 = zz[:, a], zz[:, b]
        r0, r1 = r[:, a], r[:, b]
        ok = (np.minimum(z0, z1) <= z) & (np.maximum(z0, z1) >= z)
        d = z1 - z0
        safe = np.where(np.abs(d) > 1e-12, d, 1.0)
        t = np.where(np.abs(d) > 1e-12, (z - z0) / safe, 0.0)
        hits.append((r0 + (r1 - r0) * t)[ok])
    rr = np.concatenate(hits)
    if rr.size == 0:
        return 0.0, 0.0
    out = rr[rr > r_split]
    bore = rr[rr <= r_split]
    return (float(out.max()) if out.size else 0.0,
            float(bore.min()) if bore.size else 0.0)


b1 = load(P["step1"])
b2 = load(P["step2"], z_end=P["z_end"])
r_split, dz = P["r_split"], max(P["step"], 1e-3)
lo, hi = min(P["z_end"], P["z_from"]), max(P["z_end"], P["z_from"])
n = int(round((hi - lo) / dz))
grid = [lo + i * (hi - lo) / n for i in range(n + 1)]

outer, bore = [], []
for z in grid:
    o1, i1 = section(b1, z, r_split)
    o2, i2 = section(b2, z, r_split)
    outer.append((round(z, 4), min(o1, o2)))     # 0 = материала нет
    bore.append((round(z, 4), max(i1, i2)))      # 0 = отверстия нет

# Обрезать концы, где материала нет ни у одного установа: там тела нет, и
# контур лёг бы на ось. Так итог честно короче модели, если ISV срезал торец
# глубже программы, — это видно в сверке, а не спрятано.
live = [i for i, (_, r) in enumerate(outer) if r > 1e-6]
if not live:
    raise SystemExit("[itog] результаты установов не пересекаются — "
                     "проверьте z_end и раму координат")
a, b = live[0], live[-1] + 1
outer, bore = outer[a:b], bore[a:b]
print("[itog] профиль: %d точек, z %.3f..%.3f, r до %.3f; отверстие r до %.3f"
      % (len(outer), outer[0][0], outer[-1][0], max(r for _, r in outer),
         max(r for _, r in bore)))

json.dump({"outer": outer, "bore": bore},
          open(P["prof"], "w", encoding="utf-8"))

# Наружный контур вперёд по z, отверстие назад; радиус отверстия зажимается
# наружным, иначе на концах (где наружного нет) контур самопересекается.
fwd = [App.Vector(float(r), 0, float(z)) for z, r in outer]
bak = [App.Vector(min(float(rb), float(ro)), 0, float(z))
       for (z, rb), (_, ro) in zip(reversed(bore), reversed(outer))]
clean = []
for v in fwd + bak:
    if not clean or (v - clean[-1]).Length > 1e-7:
        clean.append(v)
if (clean[0] - clean[-1]).Length > 1e-7:
    clean.append(clean[0])
solid = Part.Face(Part.makePolygon(clean)).revolve(
    App.Vector(0, 0, 0), App.Vector(0, 0, 1), 360)
solid.exportStep(P["out"])
print("[itog] OK volume=%.1f closed=%s points=%d"
      % (solid.Volume, solid.isClosed(), len(clean)))
'''


def main():
    import argparse
    for s in (sys.stdout, sys.stderr):
        if (getattr(s, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(
        description="Результаты двух установов ISV → итоговая деталь (солид)")
    ap.add_argument("setup1", help="результат установа 1 (рама детали)")
    ap.add_argument("setup2", help="результат установа 2 (своя рама)")
    ap.add_argument("out", help="куда писать итог, .step")
    ap.add_argument("--z-end", type=float, required=True,
                    help="дальний торец детали в раме установа 1 (обычно −длина)")
    ap.add_argument("--r-split", type=float, default=0.0,
                    help="порог радиуса между стенкой отверстия и наружной "
                         "поверхностью, мм")
    ap.add_argument("--z-from", type=float, default=0.0,
                    help="ближний торец детали (по умолчанию 0)")
    ap.add_argument("--step", type=float, default=0.05, metavar="MM",
                    help="шаг сечений по оси (дефолт 0.05)")
    ap.add_argument("--profile-json", help="куда выгрузить снятые профили")
    a = ap.parse_args()
    res = assemble(a.setup1, a.setup2, a.out, a.z_end, a.r_split, a.z_from,
                   step=a.step, profile_json=a.profile_json)
    print(f"✅ {res['step']}  объём {res['volume']:.1f} мм³, "
          f"z {res['z_range'][0]:.2f}..{res['z_range'][1]:.2f}, "
          f"замкнут: {res['closed']}")


if __name__ == "__main__":
    main()
