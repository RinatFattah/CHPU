#!/usr/bin/env python3
"""
verify.py — верификация результата обработки по припуску.

Сравнивает деталь ПОСЛЕ симуляции (STEP/STL, выгруженный из NX ISV — Фаза 6 — или из
CAMotics) с ЭТАЛОНОМ (исходная деталь) и проверяет, укладывается ли отклонение в
заданный припуск:
  • зарез (gouge)  — материал снят НИЖЕ номинала → брак, должен быть ≈ 0;
  • избыток (excess) — материал стоит ВЫШЕ номинала → это и есть припуск, ≤ заданного.

Результат симуляции — обычно IPW (вся оставшаяся заготовка): кроме поверхности детали
там есть ГРУБЫЙ ОСТАТОК — периметр заготовки и база под деталью (второй установ). В
вердикт по припуску идёт только ФИНИШНАЯ поверхность: достижимые грани детали (по маске
--verify-export) в пределах `band` от номинала. Всё дальше band — грубый остаток: он
считается и показывается отдельно (оранжевым), но в вердикт не входит. Недостижимые грани
(«второй установ») исключаются по маске и показаны серым.

ВАЖНО: оба входа должны быть в ОДНОЙ системе координат. Для этого и эталон, и симуляция
берут G-кодовую СК: `run_cam.py --verify-export` кладёт эталон и маски (STEP) уже в ней,
а NX/CAMotics симулируют ту же программу. Сырой экспорт из NX (координаты сборки) — не
совпадёт (verify предупредит о расхождении центров).

Критерий: PASS ⇔ max(зарез) ≤ gouge-tol И max(избыток на финишной поверхности) ≤ припуск.

Примеры:
  # эталон и маски из пайплайна — одной опцией
  python verify.py machined.step --from-export 75.6121.0.0411.003-A-CAM-DMC-635_1 --allowance 0.5

  # вручную
  python verify.py machined.step --nominal part.step --allowance 0.5 \
      --reachable part_reachable.step --unreachable part_unreachable.step

Выход: сводка в консоль, карта отклонений <machined>_deviation.ply (цветная,
открывается в MeshLab/FreeCAD/Blender) и, по флагу --json, отчёт; код 0 (PASS) / 2 (FAIL).

Зависимости: numpy, scipy, trimesh (pip install -r requirements.txt). STL читается
напрямую; STEP тесселлируется через FreeCAD одним запуском (--deflection).
"""
import argparse
import os
import sys

import numpy as np
import trimesh
from scipy.spatial import cKDTree

# Windows: консоль по умолчанию cp1251 — печать Ø/кириллицы/символов иначе падает.
for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

MESH_EXTS = {".stl", ".obj", ".ply", ".off", ".glb", ".gltf", ".3mf"}
STEP_EXTS = {".step", ".stp", ".iges", ".igs", ".brep", ".brp"}
# Точный point-to-triangle (без rtree) стоит O(точек × граней ref). Пока произведение
# в бюджете — считаем точно; иначе приближаем выборкой поверхности + KD-деревом.
EXACT_BUDGET = 40_000_000


# ── загрузка геометрии (STL напрямую; STEP → сетка через FreeCAD) ─────────────────
def _find_freecad(config_path=None):
    """freecadcmd для тесселляции STEP: FREECAD_CMD из config.yaml → автопоиск."""
    try:
        import config as _cfg
        import freecad_cam
    except Exception:
        return None
    for c in (config_path, "config.yaml"):
        if c and os.path.exists(c):
            try:
                _cfg.load(c)
            except Exception:
                pass
            break
    return freecad_cam.find_freecadcmd()


def _tessellate_steps(pairs, deflection, fc):
    """STEP → STL одним запуском FreeCAD. pairs: [(src_step, dst_stl), ...]."""
    import json
    import subprocess
    import tempfile
    spec = os.path.join(tempfile.gettempdir(), "verify_tess_spec.json")
    script = os.path.join(tempfile.gettempdir(), "verify_tess.py")
    with open(spec, "w", encoding="utf-8") as f:
        json.dump({"deflection": deflection, "pairs": pairs}, f)
    with open(script, "w", encoding="utf-8") as f:
        f.write("import json, Part, MeshPart\n"
                f"d = json.load(open(r\"{spec}\", encoding='utf-8'))\n"
                "for src, dst in d['pairs']:\n"
                "    s = Part.Shape(); s.read(src)\n"
                "    MeshPart.meshFromShape(Shape=s, LinearDeflection=d['deflection'],\n"
                "                           AngularDeflection=0.5).write(dst)\n")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    subprocess.run([fc, script], check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _as_mesh(path):
    m = trimesh.load_mesh(path)
    if isinstance(m, trimesh.Scene):          # несколько тел — склеиваем в один меш
        m = trimesh.util.concatenate(tuple(m.geometry.values()))
    return m


def load_meshes(paths, deflection, config_path=None):
    """Грузит список файлов как trimesh-меши (None → None). STL/OBJ/PLY — напрямую;
    STEP/IGES — тесселляция одним запуском FreeCAD (LinearDeflection=deflection)."""
    import tempfile
    steps = {i: p for i, p in enumerate(paths)
             if p and os.path.splitext(p)[1].lower() in STEP_EXTS}
    tmp = {}
    if steps:
        fc = _find_freecad(config_path)
        if not fc:
            raise SystemExit("❌ STEP-вход требует FreeCAD, а freecadcmd не найден. "
                             "Пропишите путь в config.yaml (FREECAD_CMD), передайте "
                             "--config FILE или подайте STL.")
        pairs = []
        for i, p in steps.items():
            dst = os.path.join(tempfile.gettempdir(), f"verify_{i}_{abs(hash(p))}.stl")
            tmp[i] = dst
            pairs.append([os.path.abspath(p), dst])
        _tessellate_steps(pairs, deflection, fc)
    return [(_as_mesh(tmp.get(i, p)) if p else None) for i, p in enumerate(paths)]


# ── знаковое расстояние: «+ снаружи ref, − внутри» ───────────────────────────────
def _sdf_exact(ref, pts, chunk=5000):
    """Точное расстояние точек до поверхности ref, знак — по внешней нормали
    ближайшей грани (ref должен быть watertight с наружу-ориентированными нормалями)."""
    fn = ref.face_normals
    out = np.empty(len(pts))
    for s in range(0, len(pts), chunk):
        q = pts[s:s + chunk]
        cp, d, tid = trimesh.proximity.closest_point_naive(ref, q)
        side = np.einsum("ij,ij->i", q - cp, fn[tid])
        out[s:s + chunk] = d * np.where(side < 0, -1.0, 1.0)
    return out


def _sdf_fast(ref, pts, n_samples):
    """Приближение для крупных мешей: плотная выборка поверхности ref + KD-дерево,
    знак — по нормали ближайшего сэмпла. Точность ≈ шаг выборки."""
    samp, fid = trimesh.sample.sample_surface(ref, n_samples)
    nrm = ref.face_normals[fid]
    d, i = cKDTree(samp).query(pts, workers=-1)
    side = np.einsum("ij,ij->i", pts - samp[i], nrm[i])
    return d * np.where(side < 0, -1.0, 1.0)


def sdf_outside(ref, pts, n_samples):
    if len(pts) * len(ref.faces) <= EXACT_BUDGET:
        return _sdf_exact(ref, pts)
    return _sdf_fast(ref, pts, n_samples)


def nearest_dist(ref, pts, n_samples, chunk=5000):
    """Беззнаковое расстояние точек до поверхности ref (для маскирования)."""
    if len(pts) * len(ref.faces) <= EXACT_BUDGET:
        out = np.empty(len(pts))
        for s in range(0, len(pts), chunk):
            out[s:s + chunk] = trimesh.proximity.closest_point_naive(ref, pts[s:s + chunk])[1]
        return out
    samp, _ = trimesh.sample.sample_surface(ref, n_samples)
    return cKDTree(samp).query(pts, workers=-1)[0]


def _pct(a, q):
    return float(np.percentile(a, q)) if len(a) else 0.0


def main():
    ap = argparse.ArgumentParser(
        description="Верификация детали после обработки по припуску (сравнение с эталоном)",
        epilog="Эталон и маски удобно получить: run_cam.py ... --verify-export")
    ap.add_argument("machined", help="результат симуляции: STL/STEP из NX ISV или CAMotics")
    ap.add_argument("--nominal", metavar="FILE", help="эталон (исходная деталь), STL/STEP")
    ap.add_argument("--from-export", metavar="BASE",
                    help="взять эталон и маски из <BASE>_part.stl / _reachable.stl / "
                         "_unreachable.stl (как их пишет run_cam.py --verify-export)")
    ap.add_argument("--allowance", type=float, required=True,
                    metavar="MM", help="припуск: верхняя граница избытка материала, мм")
    ap.add_argument("--gouge-tol", type=float, default=0.05, metavar="MM",
                    help="допустимый зарез ниже номинала, мм (дефолт 0.05)")
    ap.add_argument("--band", type=float, default=None, metavar="MM",
                    help="дальше band от номинала — грубый остаток (периметр/низ IPW, "
                         "второй установ), в вердикт по припуску не входит "
                         "(дефолт max(2, 4×припуск))")
    ap.add_argument("--reachable", metavar="FILE", help="маска достижимых граней (STL)")
    ap.add_argument("--unreachable", metavar="FILE",
                    help="маска недостижимых граней «второго установа» (STL)")
    ap.add_argument("--out", metavar="FILE", help="карта отклонений (PLY); дефолт — рядом")
    ap.add_argument("--json", metavar="FILE", help="машиночитаемый отчёт (JSON)")
    ap.add_argument("--samples", type=int, default=200000,
                    help="сэмплов поверхности для приближённого режима крупных мешей")
    ap.add_argument("--gouge-samples", type=int, default=40000,
                    help="сэмплов поверхности эталона для поиска зареза (плотность покрытия)")
    ap.add_argument("--deflection", type=float, default=0.03, metavar="MM",
                    help="точность тесселляции STEP → сетка, мм (мельче = точнее/дольше)")
    ap.add_argument("--config", metavar="FILE",
                    help="config.yaml (для FREECAD_CMD при STEP-входах)")
    args = ap.parse_args()

    nominal = args.nominal
    reach_f, unreach_f = args.reachable, args.unreachable
    if args.from_export:
        base = args.from_export

        def _pick(suffix):                       # берём .step, иначе .stl, иначе None
            for ext in (".step", ".stl"):
                if os.path.exists(base + suffix + ext):
                    return base + suffix + ext
            return None
        nominal = nominal or _pick("_part")
        reach_f = reach_f or _pick("_reachable")
        unreach_f = unreach_f or _pick("_unreachable")
        if not (reach_f and unreach_f):
            reach_f = unreach_f = None           # масок нет — проверяем всю поверхность
    if not nominal:
        ap.error("нужен --nominal FILE или --from-export BASE (файлы _part.* не найдены)")
    for f in [args.machined, nominal] + ([reach_f, unreach_f] if reach_f else []):
        if not os.path.exists(f):
            print(f"❌ Файл не найден: {f}")
            sys.exit(1)

    print(f"Результат:  {args.machined}")
    print(f"Эталон:     {nominal}")
    print(f"Припуск:    {args.allowance} мм | допуск зареза: {args.gouge_tol} мм")
    masks = bool(reach_f and unreach_f)
    if any(p and os.path.splitext(p)[1].lower() in STEP_EXTS
           for p in (args.machined, nominal, reach_f, unreach_f)):
        print(f"STEP→сетка: deflection {args.deflection} мм (FreeCAD)")
    M, P, R, U = load_meshes([args.machined, nominal,
                              reach_f if masks else None,
                              unreach_f if masks else None],
                             args.deflection, args.config)
    print(f"Маска:      {'достижимые/недостижимые грани заданы' if masks else 'нет (проверяю всю поверхность)'}")

    if not P.is_watertight:
        print("⚠  эталон не watertight — знак расстояния может быть неточным у краёв")
    # сверка СК: центры должны примерно совпадать. Габариты могут отличаться — результат
    # симуляции обычно размером с ЗАГОТОВКУ (IPW), крупнее детали; это не рассинхрон СК.
    dcenter = np.linalg.norm(M.bounds.mean(0) - P.bounds.mean(0))
    if dcenter > 0.5 * max(P.extents):
        print(f"⚠  центры эталона и результата расходятся на {dcenter:.1f} мм — возможно, "
              f"разные системы координат. Оба должны быть в СК G-кода (--verify-export).")

    V = M.vertices
    sd = sdf_outside(P, V, args.samples)           # + снаружи эталона
    excess = np.clip(sd, 0, None)                  # материал выше номинала
    below = np.clip(-sd, 0, None)                  # материал ниже номинала (зарез со стороны M)
    if masks:
        reach = nearest_dist(R, V, args.samples) <= nearest_dist(U, V, args.samples)
    else:
        reach = np.ones(len(V), bool)

    # band: точки дальше band от номинала — не финишная поверхность детали, а ГРУБЫЙ
    # ОСТАТОК (периметр заготовки, база под деталью — это второй установ). В вердикт по
    # припуску идёт только финишная поверхность (достижимая и в пределах band от номинала).
    band = args.band if args.band is not None else max(2.0, 4 * args.allowance)
    finished = reach & (excess <= band)
    leftover = excess > band

    # зарез: точки поверхности эталона, оказавшиеся СНАРУЖИ обработанной детали
    gsrc = R if masks else P                        # считаем зарез на достижимых гранях
    qg, _ = trimesh.sample.sample_surface(gsrc, args.gouge_samples)
    gouge = np.clip(sdf_outside(M, qg, args.samples), 0, None)

    ef = excess[finished]
    max_excess = float(ef.max()) if len(ef) else 0.0
    max_gouge = float(gouge.max())
    ok_excess = max_excess <= args.allowance + 1e-6
    ok_gouge = max_gouge <= args.gouge_tol + 1e-9
    verdict = ok_excess and ok_gouge

    # площади по граням (класс грани — по всем 3 вершинам)
    area = M.area_faces
    ffin = finished[M.faces].all(axis=1)
    fex = excess[M.faces].max(axis=1)
    fin_area = float(area[ffin].sum())
    over_area = float(area[ffin & (fex > args.allowance)].sum())
    leftover_area = float(area[leftover[M.faces].any(axis=1)].sum())

    print("\n── Отклонения ─────────────────────────────────────────")
    print(f"вершин результата: {len(V)} | финишная поверхность: {int(finished.sum())} "
          f"({100*finished.mean():.0f}%) | грубый остаток: {int(leftover.sum())}")
    print(f"ЗАРЕЗ (ниже номинала):    max {max_gouge:.3f} мм   "
          f"[{'OK' if ok_gouge else 'ПРЕВЫШЕН'} допуск {args.gouge_tol}]")
    print(f"ИЗБЫТОК финишной пов.:    max {max_excess:.3f} мм   "
          f"p95 {_pct(ef,95):.3f} | p50 {_pct(ef,50):.3f}   "
          f"[{'OK' if ok_excess else 'ПРЕВЫШЕН'} припуск {args.allowance}]")
    if fin_area > 0:
        print(f"площадь финишной пов. вне припуска: {over_area:.0f} / {fin_area:.0f} мм² "
              f"({100*over_area/fin_area:.1f}%)")
    if leftover.any():
        print(f"(инфо) грубый остаток > band {band:.1f} мм (периметр/низ/2-й установ): "
              f"{leftover_area:.0f} мм², max {float(excess[leftover].max()):.1f} мм — не в вердикте")
    if finished.any():
        fi = int(np.argmax(np.where(finished, excess, -1.0)))
        print(f"худший избыток финишной пов. @ ({V[fi,0]:.1f}, {V[fi,1]:.1f}, {V[fi,2]:.1f})")
    if max_gouge > 1e-6:
        gi = int(np.argmax(gouge))
        print(f"худший зарез @ ({qg[gi,0]:.1f}, {qg[gi,1]:.1f}, {qg[gi,2]:.1f})")
    print("───────────────────────────────────────────────────────")
    print(f"ВЕРДИКТ: {'✅ PASS — финишная поверхность в припуске' if verdict else '❌ FAIL'}")
    if not ok_gouge:
        print(f"  • зарез {max_gouge:.3f} > допуска {args.gouge_tol} мм (срезано лишнее)")
    if not ok_excess:
        print(f"  • избыток {max_excess:.3f} > припуска {args.allowance} мм на финишной поверхности")
    if leftover.any():
        print(f"  ⓘ {leftover_area:.0f} мм² грубого остатка (>band) не проверялось — это "
              f"периметр/низ заготовки или второй установ; смотри оранжевое на карте")

    # ── карта отклонений: цвет по вершинам результата ──
    col = np.tile([90, 180, 110, 255], (len(V), 1)).astype(np.uint8)      # зелёный: в припуске
    col[finished & (excess > args.allowance)] = [210, 70, 60, 255]        # красный: недосъём на детали
    col[~reach & (excess <= band)] = [140, 140, 140, 255]                 # серый: второй установ у пов.
    col[leftover] = [235, 150, 40, 255]                                   # оранжевый: грубый остаток
    col[below > args.gouge_tol] = [70, 110, 210, 255]                     # синий: зарез
    M.visual.vertex_colors = col
    out = args.out or (os.path.splitext(args.machined)[0] + "_deviation.ply")
    M.export(out)
    print(f"карта отклонений → {out}\n  (зелёный=в припуске, красный=избыток на детали, "
          f"синий=зарез, оранжевый=грубый остаток, серый=второй установ)")

    if args.json:
        import json
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "pass": bool(verdict),
                "allowance_mm": args.allowance, "gouge_tol_mm": args.gouge_tol,
                "band_mm": band,
                "max_gouge_mm": max_gouge, "max_excess_finished_mm": max_excess,
                "excess_p95_mm": _pct(ef, 95), "excess_p50_mm": _pct(ef, 50),
                "finished_fraction": float(finished.mean()),
                "finished_area_mm2": fin_area, "over_allowance_area_mm2": over_area,
                "leftover_area_mm2": leftover_area,
                "machined": args.machined, "nominal": nominal,
                "masks": masks, "deviation_map": out,
            }, f, ensure_ascii=False, indent=2)
        print(f"отчёт → {args.json}")

    sys.exit(0 if verdict else 2)


if __name__ == "__main__":
    main()
