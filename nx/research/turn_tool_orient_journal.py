"""Разведка: поддаётся ли записи ориентация кромки резца в кармане револьвера.

Резец в токарном проекте создаётся в POCKET_01 смонтированного станка и
НАСЛЕДУЕТ параметры от кармана — подтип (OD_80_L / OD_80_R) на OrientAngle при
этом не влияет: и тот и другой приходят с 5°. Журнал воспроизводит ровно этот
контекст (станок + POCKET_01), пробует выставить угол и ЧИТАЕТ ОБРАТНО после
Commit — то есть отвечает, надо ли снимать флаг наследования.

Headless (run_journal). Параметры env NX_TURN_ORIENT (JSON): stock_step, machine,
work_prt, out_json, log_path.
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

p = json.load(open(os.environ["NX_TURN_ORIENT"], encoding="utf-8"))
LOG = p.get("log_path")
OUT = {}


def log(m):
    line = "[orient] " + str(m)
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

for pocket in ("POCKET_01", "GENERIC_MACHINE"):
    try:
        parent = setup.CAMGroupCollection.FindObject(pocket)
    except Exception as e:
        log(f"{pocket}: нет ({e})")
        continue
    for subtype in ("OD_80_L", "OD_80_R"):
        key = f"{pocket}/{subtype}"
        rec = {}
        try:
            tool = setup.CAMGroupCollection.CreateToolWithUserName(
                parent, "turning", subtype,
                NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue,
                f"P_{pocket}_{subtype}", f"P{subtype}")
            tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
        except Exception as e:
            log(f"{key}: создать не удалось — {type(e).__name__}: {e}")
            OUT[key] = {"error": str(e)}
            continue
        ob = tb.OrientAngleBuilder
        rec["orient_before"] = ob.Value
        # у InheritableDoubleBuilder значение может быть УНАСЛЕДОВАННЫМ — тогда
        # запись .Value молча не удержится; смотрим, есть ли флаг наследования
        rec["builder_attrs"] = [a for a in dir(ob)
                                if not a.startswith("_") and a[0].isupper()]
        for flag in ("InheritOption", "Inherit", "Inherited", "UseInherited"):
            if hasattr(ob, flag):
                try:
                    rec[f"flag_{flag}"] = str(getattr(ob, flag))
                except Exception as e:
                    rec[f"flag_{flag}"] = f"<{type(e).__name__}>"
        try:
            ob.Value = 95.0
            rec["orient_after_set"] = ob.Value
        except Exception as e:
            rec["orient_after_set"] = f"FAIL {type(e).__name__}: {e}"
        try:
            tb.Commit()
        except Exception as e:
            rec["commit"] = f"FAIL {type(e).__name__}: {e}"
        tb.Destroy()
        # читаем НОВЫМ билдером — только так видно, что реально сохранилось
        try:
            tb2 = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
            rec["orient_reread"] = tb2.OrientAngleBuilder.Value
            rec["nose_angle_reread"] = tb2.NoseAngleBuilder.Value
            rec["size_reread"] = tb2.SizeBuilder.Value
            rec["nose_radius_reread"] = tb2.NoseRadiusBuilder.Value
            tb2.Destroy()
        except Exception as e:
            rec["orient_reread"] = f"FAIL {type(e).__name__}: {e}"
        OUT[key] = rec
        log(f"{key}: before={rec.get('orient_before')} "
            f"set={rec.get('orient_after_set')} reread={rec.get('orient_reread')}")

with open(p["out_json"], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
log(f"OK out={p['out_json']}")
os._exit(0)
