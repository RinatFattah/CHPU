"""Разведка: КАНАВОЧНЫЙ резец — почему ISV не строит по нему форму съёма.

Контекст воспроизводится полностью (станок смонтирован, резцы в карманах
револьвера), потому что геометрия наследуется от кармана: в голом setup у
подтипов одни значения, в кармане — другие.

Печатает по каждому канавочному подтипу ВСЕ свойства билдера с значениями —
видно, что осталось None/0 и выглядит вырожденным. Плюс разведка библиотечного
пути: какие методы коллекции групп и какие классы NXOpen.CAM отвечают за
библиотеку инструмента (гипотеза: форму собирают из библиотеки, а не из
нескольких свойств голого билдера).

Headless (run_journal), CSE не нужен. Параметры env NX_TURN_GROOVE (JSON):
stock_step, machine, work_prt, out_json, log_path, subtypes.
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

p = json.load(open(os.environ["NX_TURN_GROOVE"], encoding="utf-8"))
LOG = p.get("log_path")
OUT = {}


def log(m):
    line = "[groove] " + str(m)
    print(line, flush=True)
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def value_of(obj):
    for attr in ("Value", "ValueAsString"):
        if hasattr(obj, attr):
            try:
                return getattr(obj, attr)
            except Exception:
                pass
    return None


def dump_builder(tb):
    info = {}
    for name in sorted(dir(tb)):
        if name.startswith("_") or name[0].islower():
            continue
        try:
            obj = getattr(tb, name)
        except Exception as e:
            info[name] = f"<get FAIL {type(e).__name__}>"
            continue
        if callable(obj):
            continue
        v = value_of(obj)
        if v is not None:
            info[name] = f"{v!r}"
        elif obj is None:
            info[name] = "None"
        elif isinstance(obj, (int, float, str, bool)):
            info[name] = f"{obj!r}"
        else:
            info[name] = f"<{type(obj).__name__}>"
    return info


session = NXOpen.Session.GetSession()
base = session.GetEnvironmentVariableValue("UGII_BASE_DIR")

imp = session.DexManager.CreateStep242Importer()
imp.ImportTo = NXOpen.Step242Importer.ImportToOption.NewPart
imp.SetMode(NXOpen.BaseImporter.Mode.NativeFileSystem)
imp.SewSurfaces = True
imp.ObjectTypes.Solids = True
imp.SettingsFile = os.path.join(base, "translators", "step242", "step242ug.def")
imp.InputFile = p["stock_step"]
imp.OutputFile = p["work_prt"]
imp.FileOpenFlag = False
imp.ProcessHoldFlag = True
imp.Commit()
imp.Destroy()
session.Parts.OpenActiveDisplay(p["work_prt"], NXOpen.DisplayPartOption.AllowAdditional)
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
log(f"станок смонтирован: {p['machine']}")

# ── что вообще есть по части БИБЛИОТЕКИ инструмента ──
api = {"CAMGroupCollection": sorted(
           m for m in dir(setup.CAMGroupCollection)
           if any(k in m for k in ("Librar", "Retrieve", "Import", "Catalog"))),
       "NXOpen.CAM": sorted(
           n for n in dir(NXOpen.CAM)
           if any(k in n for k in ("Librar", "Catalog")))}
OUT["_library_api"] = api
for k, v in api.items():
    log(f"библиотечный API {k}: {', '.join(v) or '—'}")

POCKETS = [f"POCKET_{i:02d}" for i in range(1, 13)] + ["GENERIC_MACHINE"]
used = set()

for subtype in p.get("subtypes", ["OD_GROOVE_L"]):
    tool = tb = None
    for pname in POCKETS:
        if pname in used:
            continue
        try:
            parent = setup.CAMGroupCollection.FindObject(pname)
        except Exception:
            continue
        try:
            tool = setup.CAMGroupCollection.CreateToolWithUserName(
                parent, "turning", subtype,
                NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue,
                f"G_{subtype}", f"G{subtype}")
        except Exception as e:
            log(f"{subtype} в {pname}: {type(e).__name__}: {str(e)[:70]}")
            continue
        used.add(pname)
        try:
            tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
        except Exception as e:
            log(f"{subtype}: билдер не создался — {e}")
            tb = None
        break
    if tb is None:
        OUT[subtype] = {"error": "не создан"}
        continue
    info = dump_builder(tb)
    OUT[subtype] = info
    log(f"--- {subtype} ({type(tb).__name__}) ---")
    # печатаем ТОЛЬКО подозрительное: None и нули — вырожденная геометрия
    bad = {k: v for k, v in info.items() if v in ("None", "0.0", "0", "''")}
    good = {k: v for k, v in info.items()
            if k not in bad and not v.startswith("<")}
    log(f"    ЗАДАНО:    {', '.join(f'{k}={v}' for k, v in good.items())}")
    log(f"    ПУСТО:     {', '.join(bad)}")
    tb.Destroy()

with open(p["out_json"], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
log(f"OK out={p['out_json']}")
os._exit(0)
