"""Разведка: параметры ТОКАРНОГО резца в NX (что вообще можно задать).

Создаёт токарный setup и по одному резцу каждого подтипа (OD_80_L / OD_80_R /
OD_GROOVE_L ...), затем печатает ВСЕ свойства билдера с текущими значениями.
Нужно, чтобы понять: почему InsertLengthBuilder не применяется, чем L отличается
от R, и есть ли явный угол ориентации пластины в державке (от него зависит, в
какую сторону смотрит режущая кромка — а значит и куда резец врезается).

Headless (run_journal), станок не нужен. Параметры env NX_TURN_TOOL_PROBE (JSON):
work_prt, out_json, log_path, subtypes (список).
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

p = json.load(open(os.environ["NX_TURN_TOOL_PROBE"], encoding="utf-8"))
LOG = p.get("log_path")
OUT = {}


def log(m):
    line = "[tool] " + str(m)
    print(line, flush=True)
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def value_of(obj):
    """Значение свойства билдера: у *Builder-объектов оно в .Value."""
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
            info[name] = f"{v!r}  ({type(obj).__name__})"
        elif isinstance(obj, (int, float, str, bool)):
            info[name] = f"{obj!r}"
        else:
            info[name] = f"<{type(obj).__name__}>"
    return info


session = NXOpen.Session.GetSession()
base = session.GetEnvironmentVariableValue("UGII_BASE_DIR")

session.Parts.NewDisplay(p["work_prt"], NXOpen.Part.Units.Millimeters)
wp = session.Parts.Work

session.ApplicationSwitchImmediate("UG_APP_MANUFACTURING")
session.IsCamSessionInitialized()
session.CreateCamSession()
session.CAMSession.SpecifyConfiguration(
    os.path.join(base, "mach", "resource", "configuration", "cam_general.dat"))
setup = wp.CreateCamSetup("turning")
log("токарный setup создан")

parent = setup.CAMGroupCollection.FindObject("GENERIC_MACHINE")

for i, subtype in enumerate(p.get("subtypes", ["OD_80_L", "OD_80_R"])):
    try:
        tool = setup.CAMGroupCollection.CreateToolWithUserName(
            parent, "turning", subtype,
            NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue,
            f"PROBE_{i}", f"Probe{i}")
        tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
    except Exception as e:
        log(f"{subtype}: СОЗДАТЬ НЕ УДАЛОСЬ — {type(e).__name__}: {e}")
        OUT[subtype] = {"error": f"{type(e).__name__}: {e}"}
        continue
    log(f"--- {subtype}: билдер {type(tb).__name__} ---")
    info = dump_builder(tb)
    for k in sorted(info):
        log(f"    {k} = {info[k]}")
    # пробуем задать длину пластины — интересует, применяется ли и что мешает
    try:
        tb.InsertLengthBuilder.Value = 12.0
        info["_set_InsertLength"] = f"OK -> {tb.InsertLengthBuilder.Value}"
    except Exception as e:
        info["_set_InsertLength"] = f"FAIL {type(e).__name__}: {e}"
    log(f"    _set_InsertLength: {info['_set_InsertLength']}")
    OUT[subtype] = info
    try:
        tb.Commit()
    except Exception as e:
        log(f"{subtype}: commit FAIL {type(e).__name__}: {e}")
    tb.Destroy()

with open(p["out_json"], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
log(f"OK out={p['out_json']}")
os._exit(0)
