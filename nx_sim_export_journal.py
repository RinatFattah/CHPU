"""
nx_sim_export_journal.py — выполняется в NX (run_journal.exe, batch).

Экспорт результата симуляции в STEP: открывает IPW-файл (*_ipw.prt), который
ISV сохранил автоматически (SaveAsPartfile — см. nx_sim_journal.py), берёт
фасетное тело с максимумом треугольников (обработанная заготовка), скрывает
остальное и пишет STEP AP242 ED2 (фасеты умеет только он). Batch-экспорт
фасетных тел через StepCreator проверен и работает.

Параметры (env NX_SIM_EXPORT_PARAMS, JSON): prt, out_step, min_triangles.
"""

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import NXOpen
import NXOpen.UF as UF


def log(msg):
    print(f"[nxexp] {msg}", flush=True)


def main():
    with open(os.environ["NX_SIM_EXPORT_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)

    session = NXOpen.Session.GetSession()
    part, st = session.Parts.OpenActiveDisplay(
        p["prt"], NXOpen.DisplayPartOption.AllowAdditional)
    st.Dispose()
    wp = session.Parts.Work
    uf = UF.UFSession.GetUFSession()

    def tris(fb):
        try:
            return uf.Facet.AskNFacetsInModel(fb.Tag)
        except Exception:
            return -1

    facets = [(tris(fb), fb) for fb in wp.FacetedBodies]
    for n, fb in facets:
        log(f"фасетное тело: {n} треугольников")
    min_tri = int(p.get("min_triangles", 50))
    good = [(n, fb) for n, fb in facets if n >= min_tri]
    if not good:
        raise RuntimeError(
            f"в {os.path.basename(p['prt'])} нет фасетного тела с ≥{min_tri} "
            f"треугольников — кнопка «Создать фасетное тело для ЗвПО» не "
            f"сработала (тел: {[n for n, _ in facets]})")
    n_best, best = max(good, key=lambda t: t[0])
    log(f"результат: тело с {n_best} треугольниками")

    # скрыть всё, кроме результата: PROCESS_HIDDEN_OBJECTS=No в def-файле
    # не пускает скрытое в экспорт
    hide = [b for b in wp.Bodies]
    hide += [fb for _, fb in facets if fb.Tag != best.Tag]
    if hide:
        try:
            session.DisplayManager.BlankObjects(hide)
            # экспорт идёт из файла (InputFile) — скрытие должно попасть на диск
            sv = part.Save(NXOpen.BasePart.SaveComponents.TrueValue,
                           NXOpen.BasePart.CloseAfterSave.FalseValue)
            sv.Dispose()
        except Exception as e:
            log(f"warn: скрытие лишних тел: {e}")

    out = p["out_step"]
    if os.path.exists(out):
        os.remove(out)
    base = session.GetEnvironmentVariableValue("UGII_BASE_DIR")
    sc = session.DexManager.CreateStepCreator()
    try:
        sc.SettingsFile = os.path.join(base, "translators", "step242",
                                       "ugstep242.def")
        sc.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap242ED2
        sc.ObjectTypes.Solids = True
        sc.ObjectTypes.Surfaces = True
        sc.ObjectTypes.FacetBodies = True
        sc.InputFile = p["prt"]
        sc.OutputFile = out
        sc.LayerMask = "1-256"
        sc.FileSaveFlag = False
        sc.ProcessHoldFlag = True
        sc.Commit()
    finally:
        sc.Destroy()
    size = os.path.getsize(out) if os.path.exists(out) else 0
    if size <= 0:
        raise RuntimeError("STEP не создался")
    log(f"OK triangles={n_best} size={size} step={out}")


if os.environ.get("NX_SIM_EXPORT_PARAMS"):
    main()
