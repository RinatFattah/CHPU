"""Разведка: можно ли создать СВЕРЛО в токарном проекте NX через API.

Наш обвяз умеет делать только резцы, поэтому полная программа установа (с
центровкой, сверлением и расточкой) в ISV встаёт на первой же смене на
сверлильный инструмент — станция револьвера пуста. Здесь перебираются пары
(тип шаблона, подтип) и печатается, какая из них создаётся и каким билдером
задавать диаметр.

Headless (run_journal), станок не нужен. Параметры env NX_DRILL_PROBE (JSON):
work_prt, out_json, log_path, candidates — список [тип, подтип].
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

p = json.load(open(os.environ["NX_DRILL_PROBE"], encoding="utf-8"))
LOG = p.get("log_path")
OUT = {}


def log(m):
    line = "[drill] " + str(m)
    print(line, flush=True)
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def size_props(tb):
    """Свойства билдера, похожие на размер инструмента, с текущими значениями."""
    out = {}
    for name in sorted(dir(tb)):
        if name.startswith("_") or name[0].islower():
            continue
        if not any(k in name for k in ("Diam", "Length", "Angle", "Radius",
                                       "Flute", "TlNumber")):
            continue
        try:
            obj = getattr(tb, name)
        except Exception as e:
            out[name] = f"<get FAIL {type(e).__name__}>"
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
        out[name] = repr(v) if v is not None else f"<{type(obj).__name__}>"
    return out


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

for i, (ttype, subtype) in enumerate(p["candidates"]):
    key = f"{ttype}/{subtype}"
    try:
        tool = setup.CAMGroupCollection.CreateToolWithUserName(
            parent, ttype, subtype,
            NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue,
            f"PROBE_{i}", f"Probe{i}")
    except Exception as e:
        OUT[key] = {"create": f"FAIL {type(e).__name__}: {str(e)[:120]}"}
        log(f"{key}: создать не удалось — {type(e).__name__}: {str(e)[:120]}")
        continue
    rec = {"create": "OK"}
    for bname in ("CreateMillToolBuilder", "CreateTurnToolBuilder",
                  "CreateDrillToolBuilder"):
        if not hasattr(setup.CAMGroupCollection, bname):
            continue
        try:
            tb = getattr(setup.CAMGroupCollection, bname)(tool)
        except Exception as e:
            rec[bname] = f"FAIL {type(e).__name__}: {str(e)[:80]}"
            continue
        rec[bname] = type(tb).__name__
        rec["props"] = size_props(tb)
        # пробуем задать диаметр — важно не «есть свойство», а «применяется»
        for dname in ("TlDiameterBuilder", "DiameterBuilder"):
            if hasattr(tb, dname):
                try:
                    getattr(tb, dname).Value = 10.0
                    rec["set_" + dname] = f"OK -> {getattr(tb, dname).Value}"
                except Exception as e:
                    rec["set_" + dname] = f"FAIL {type(e).__name__}"
        try:
            tb.Commit()
            rec["commit"] = "OK"
        except Exception as e:
            rec["commit"] = f"FAIL {type(e).__name__}: {str(e)[:120]}"
        tb.Destroy()
        break
    OUT[key] = rec
    log(f"{key}: {rec.get('commit', '—')} | "
        + ", ".join(f"{k}={v}" for k, v in list(rec.get("props", {}).items())[:6]))

with open(p["out_json"], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
log(f"OK out={p['out_json']}")
os._exit(0)
