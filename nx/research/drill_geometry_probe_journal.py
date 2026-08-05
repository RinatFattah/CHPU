"""Разведка: какая геометрия РЕАЛЬНО оказывается у сверла после Commit.

Съём в ISV не сходится с заданным диаметром: Ø10 даёт отверстие r 5.82
вместо 5.00, Ø6 — r 4.24 вместо 3.00. Ни аддитивно, ни пропорционально.
Здесь сверло создаётся ровно так же, как это делает make_drill(), а затем ВСЕ
свойства билдера перечитываются НОВЫМ билдером после Commit — только так видно,
что осталось в инструменте, а что NX подставил своё.

Headless (run_journal), станок не нужен. Параметры env NX_DRILL_GEOM (JSON):
work_prt, out_json, log_path, cases — список {diameter, flute}.
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

p = json.load(open(os.environ["NX_DRILL_GEOM"], encoding="utf-8"))
LOG = p.get("log_path")
OUT = {}


def log(m):
    line = "[geom] " + str(m)
    print(line, flush=True)
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def dump(tb):
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
        v = None
        for attr in ("Value", "ValueAsString"):
            if hasattr(obj, attr):
                try:
                    v = getattr(obj, attr)
                    break
                except Exception:
                    pass
        if v is not None:
            info[name] = v if isinstance(v, (int, float, str, bool)) else repr(v)
        elif isinstance(obj, (int, float, str, bool)):
            info[name] = obj
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
parent = setup.CAMGroupCollection.FindObject("GENERIC_MACHINE")

for i, case in enumerate(p["cases"]):
    dia, flute = float(case["diameter"]), float(case["flute"])
    key = f"D{dia:g}_F{flute:g}"
    tool = setup.CAMGroupCollection.CreateToolWithUserName(
        parent, "hole_making", "STD_DRILL",
        NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue,
        f"G_{key}", f"G_{key}")
    tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
    before = dump(tb)
    for prop, val in (("TlDiameterBuilder", dia), ("TlFluteLnBuilder", flute)):
        try:
            getattr(tb, prop).Value = val
        except Exception as e:
            log(f"{key}: {prop} не применилось ({type(e).__name__})")
    try:
        tb.Commit()
        ok = "OK"
    except Exception as e:
        ok = f"FAIL {type(e).__name__}: {str(e)[:100]}"
    tb.Destroy()
    # ПЕРЕЧИТЫВАЕМ новым билдером — иначе видно только то, что мы сами задали
    tb2 = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
    after = dump(tb2)
    tb2.Destroy()
    changed = {k: (before.get(k), after.get(k)) for k in after
               if before.get(k) != after.get(k)}
    OUT[key] = {"commit": ok, "after": after, "changed_by_commit": changed}
    log(f"=== {key}: commit {ok} ===")
    for k in sorted(after):
        mark = " <-- изменилось" if k in changed else ""
        log(f"    {k} = {after[k]}{mark}")

with open(p["out_json"], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
log(f"OK out={p['out_json']}")
os._exit(0)
