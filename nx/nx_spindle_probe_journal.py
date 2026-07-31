"""Определить ось шпинделя токарного станка NX в МИРОВЫХ координатах.

Монтирует станок и читает матрицу стыка кинематического компонента SPINDLE —
её Z-ось и есть ось вращения шпинделя. Нужно, чтобы посадить деталь на шпиндель
автоматически (а не зашивать поворот под конкретный станок). Результат — JSON
{"spindle_axis": [x,y,z]}. Выполняется headless (run_journal).

Параметры env NX_SPINDLE_PROBE (JSON): stock_step (любое тело для work part),
machine, work_prt, out_json, log_path.
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
import NXOpen.CAM

p = json.load(open(os.environ["NX_SPINDLE_PROBE"], encoding="utf-8"))
LOG = p.get("log_path")


def log(m):
    line = "[spindle] " + str(m)
    print(line, flush=True)
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


session = NXOpen.Session.GetSession()
base = session.GetEnvironmentVariableValue("UGII_BASE_DIR")

imp = session.DexManager.CreateStep242Importer()
imp.ImportTo = NXOpen.Step242Importer.ImportToOption.NewPart
imp.SetMode(NXOpen.BaseImporter.Mode.NativeFileSystem)
imp.SewSurfaces = True
imp.ObjectTypes.Solids = True
imp.ObjectTypes.Surfaces = True
imp.SettingsFile = os.path.join(base, "translators", "step242", "step242ug.def")
imp.InputFile = p["stock_step"]
imp.OutputFile = p["work_prt"]
imp.FileOpenFlag = False
imp.ProcessHoldFlag = True
imp.Commit()
imp.Destroy()
part, st = session.Parts.OpenActiveDisplay(
    p["work_prt"], NXOpen.DisplayPartOption.AllowAdditional)
st.Dispose()
wp = session.Parts.Work

session.ApplicationSwitchImmediate("UG_APP_MANUFACTURING")
session.IsCamSessionInitialized()
session.CreateCamSession()
session.CAMSession.SpecifyConfiguration(
    os.path.join(base, "mach", "resource", "configuration", "cam_general.dat"))
setup = wp.CreateCamSetup("turning")
wp.CreateKinematicConfigurator()

generic = setup.CAMGroupCollection.FindObject("GENERIC_MACHINE")
mgb = setup.CAMGroupCollection.CreateMachineGroupBuilder(generic)
mount = setup.CreateNcmctPartMountingBuilder(p["machine"])
mount.CreateMachineSpindleObjects = False
mount.Positioning = (NXOpen.CAM.NcmctPartMountingBuilder
                     .PositioningTypes.KeepAssemblyConstraints)
mount.Commit()
mgb.ReplaceMachine(
    NXOpen.CAM.MachineGroupBuilder.RetrieveToolPocketInformation.Yes, mount)
mount.Destroy()
mgb.Destroy()
log(f"станок подключён: {p['machine']}")

kin = wp.KinematicConfigurator
axis = None
# основной путь: матрица стыка компонента SPINDLE (Z = ось вращения)
try:
    comp = kin.ComponentCollection.FindObject("SPINDLE")
    m = comp.GetJunctions()[0].Matrix
    mm = getattr(m, "Element", m)
    axis = [round(mm.Zx, 6), round(mm.Zy, 6), round(mm.Zz, 6)]
    log(f"ось шпинделя (SPINDLE junction Z) = {axis}")
except Exception as e:
    log(f"SPINDLE junction FAIL: {type(e).__name__}: {e}")
    # запасной путь: ZM токарной MCS
    try:
        mg = setup.CAMGroupCollection.FindObject("MCS_MAIN_SPINDLE")
        ob = setup.CAMGroupCollection.CreateTurnOrientGeomBuilder(mg)
        zc = getattr(ob.Mcs.Orientation, "Element", ob.Mcs.Orientation)
        axis = [round(zc.Zx, 6), round(zc.Zy, 6), round(zc.Zz, 6)]
        ob.Destroy()
        log(f"ось шпинделя (MCS_MAIN_SPINDLE fallback) = {axis}")
    except Exception as e2:
        log(f"MCS fallback FAIL: {type(e2).__name__}: {e2}")

if axis and any(abs(v) > 1e-6 for v in axis):
    with open(p["out_json"], "w", encoding="utf-8") as f:
        json.dump({"spindle_axis": axis, "machine": p["machine"]}, f)
    log(f"OK out={p['out_json']}")
else:
    log("ось шпинделя НЕ ОПРЕДЕЛЕНА")
os._exit(0)
