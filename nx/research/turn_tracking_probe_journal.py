"""Разведка: ТОЧКА ОТСЛЕЖИВАНИЯ токарного резца (TrackingBuilder).

Зачем. В NX ISV съём идёт на 0.4·R глубже программы, причём вылеты в таблице
стойки ($TC_DP3/DP4) на это НЕ влияют вовсе — проверено. Значит ISV ставит
модель пластины по своей привязке, а не по той, что подразумевает Schneidenlage
стойки. У токарного резца в NX есть `TrackingBuilder` — набор точек
отслеживания (теоретическая вершина, центр скругления, …). Если ISV берёт
положение пластины оттуда, это и есть искомый переключатель.

Дампит содержимое TrackingBuilder и всё, что похоже на положение вершины,
у подтипа из `subtypes`. Headless (run_journal), станок не нужен.

ШАГ 1 (07.08.2026): у резца ровно ОДНА точка отслеживания
(`NumberOfTrackPoints = 1`), и есть `GetTrackPoint`/`Modify`. Вопрос, ради
которого зонд писался: КАКУЮ точку пластины она называет — теоретическую
вершину P (её печатает наша программа) или центр скругления C = P + R·n. Если
центр, плёнка объясняется сразу и целиком: мы ведём вершину, ISV ведёт центр.

Параметры env NX_TURN_TRACK_PROBE (JSON): work_prt, out_json, log_path, subtypes.
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

p = json.load(open(os.environ["NX_TURN_TRACK_PROBE"], encoding="utf-8"))
LOG = p.get("log_path")
OUT = {}


def log(m):
    line = "[track] " + str(m)
    print(line, flush=True)
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def describe(obj, depth=0, seen=None):
    """Рекурсивный дамп свойств объекта: значения, вложенные билдеры, списки."""
    seen = seen if seen is not None else set()
    if id(obj) in seen or depth > 2:
        return "<...>"
    seen.add(id(obj))
    info = {}
    for name in sorted(dir(obj)):
        if name.startswith("_") or name[0].islower() or name == "Null":
            continue
        try:
            sub = getattr(obj, name)
        except Exception as e:
            info[name] = f"<get FAIL {type(e).__name__}: {e}>"
            continue
        if callable(sub):
            continue
        for attr in ("Value", "ValueAsString"):
            if hasattr(sub, attr):
                try:
                    info[name] = f"{getattr(sub, attr)!r}"
                    break
                except Exception:
                    pass
        else:
            if isinstance(sub, (int, float, str, bool)):
                info[name] = repr(sub)
            elif sub is None:
                info[name] = "None"
            else:
                nested = describe(sub, depth + 1, seen)
                info[name] = (f"<{type(sub).__name__}> {nested}"
                              if isinstance(nested, dict) and nested
                              else f"<{type(sub).__name__}>")
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

for i, subtype in enumerate(p.get("subtypes", ["OD_55_R"])):
    try:
        tool = setup.CAMGroupCollection.CreateToolWithUserName(
            parent, "turning", subtype,
            NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue,
            f"TRK_{i}", f"Trk{i}")
        tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
    except Exception as e:
        log(f"{subtype}: создать не удалось — {type(e).__name__}: {e}")
        OUT[subtype] = {"error": str(e)}
        continue
    info = {}
    # ставим ту же геометрию, что в боевом прогоне
    for prop, val in (("NoseRadiusBuilder", 0.4), ("SizeBuilder", 6.35)):
        try:
            getattr(tb, prop).Value = val
        except Exception as e:
            log(f"   {prop}: FAIL {e}")
    try:
        trk = tb.TrackingBuilder
        info["_tracking_api"] = sorted(n for n in dir(trk)
                                       if not n.startswith("_"))
        log(f"--- {subtype}: API точки отслеживания ---")
        log("      " + ", ".join(info["_tracking_api"]))
    except Exception as e:
        log(f"   TrackingBuilder API: FAIL {e}")
        trk = None

    # --- ШАГ 1: ЧТО ЭТО ЗА ТОЧКА ---
    if trk is not None:
        # сигнатуры: биндинги NXOpen несут их в докстрингах
        sig = {}
        for meth in ("GetTrackPoint", "Get", "Modify", "Create"):
            try:
                doc = (getattr(trk, meth).__doc__ or "").strip()
            except Exception as e:
                doc = f"<нет: {type(e).__name__}>"
            sig[meth] = doc
            log(f"--- {subtype}.Tracking.{meth} — сигнатура ---")
            for ln in doc.splitlines()[:12]:
                log("      " + ln.rstrip())
        info["_tracking_signatures"] = sig

        try:
            n_tp = int(trk.NumberOfTrackPoints)
        except Exception:
            n_tp = 1
        log(f"--- {subtype}: точек отслеживания {n_tp} ---")
        # GetTrackPoint(position) -> NXObject (позиция с НУЛЯ);
        # Get(pointTag) -> (name, radiusId, tpNumber, angle, radius,
        #                   xOffset, yOffset, adjustReg, cutcomReg)
        FIELDS = ("name", "radiusId", "tpNumber", "angle", "radius",
                  "xOffset", "yOffset", "adjustReg", "cutcomReg")
        pts = []
        for idx in range(max(1, n_tp)):
            try:
                pt = trk.GetTrackPoint(idx)
            except Exception as e:
                log(f"   GetTrackPoint({idx}): FAIL {type(e).__name__}: {e}")
                continue
            try:
                vals = trk.Get(pt)
            except Exception as e:
                log(f"   Get(точка {idx}): FAIL {type(e).__name__}: {e}")
                continue
            rec = dict(zip(FIELDS, vals))
            pts.append(rec)
            log(f"   точка {idx}: {getattr(pt, 'Name', '?')}")
            for k in FIELDS:
                log(f"      {k:<10} = {rec.get(k)!r}")
        info["_track_points"] = pts
    for name in ("TrackingBuilder", "InsertPositionBuilder", "InsertPosition",
                 "InsertShape", "SizeOption", "XMountBuilder", "YMountBuilder",
                 "TlZMountBuilder", "TlZOffsetBuilder", "TlXOffsetBuilder",
                 "OrientAngleBuilder", "NoseAngleBuilder", "NoseRadiusBuilder"):
        try:
            obj = getattr(tb, name)
        except Exception as e:
            info[name] = f"<нет: {type(e).__name__}>"
            continue
        d = describe(obj)
        info[name] = d if isinstance(d, dict) else repr(d)
        log(f"--- {subtype}.{name} ---")
        if isinstance(d, dict):
            for k in sorted(d):
                log(f"      {k} = {d[k]}")
        else:
            log(f"      {d}")
    OUT[subtype] = info
    try:
        tb.Commit()
    except Exception as e:
        log(f"{subtype}: commit FAIL {type(e).__name__}: {e}")
    tb.Destroy()

with open(p["out_json"], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
log(f"OK out={p['out_json']}")
