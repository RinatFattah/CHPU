"""Разведка: можно ли задать СВОЮ форму пластины (InsertShape) у токарного резца.

Зачем. Плёнка 0.41·R = разница между точкой отсчёта модели резца NX и точкой по
ISO. Перебор положений вершины (runs/82) её не убрал: положение 4 сидит на
0.59·R от центра ИМЕННО ПОТОМУ, что пластина — ромб 55°. С ДРУГОЙ ФОРМОЙ
пластины те же девять положений легли бы иначе, и одно из них могло бы совпасть
с ISO-точкой (у квадратной пластины настоящий угол и есть пересечение
касательных). Подтипов в шаблонах всего два, но подтип задаёт державку и руку —
а форму пластины, возможно, можно менять отдельно полем `InsertShape`.

Проверялось раньше и НЕ отвечает на этот вопрос: числовые углы
(`NoseAngle`/`OrientAngle`/`ReliefAngle`) записываются и не действуют — но это
про УГЛЫ, а не про форму. `InsertShape` не проверялся ни разу.

Дамп: тип и значение полей формы, доступные члены перечисления, попытка
поставить каждый и перечитать после Commit. Headless (run_journal), секунды.

Параметры env NX_TURN_SHAPE (JSON): work_prt, out_json, log_path, subtypes.
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

p = json.load(open(os.environ["NX_TURN_SHAPE"], encoding="utf-8"))
LOG = p.get("log_path")
OUT = {}


def log(m):
    line = "[shape] " + str(m)
    print(line, flush=True)
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def enum_members(tb, field):
    """Члены перечисления поля `field`: [(имя, значение)].

    У NXOpen они лежат НЕ в типе значения, а в отдельном объекте-держателе с
    именем во множественном числе: InsertShape → InsertShapes,
    InsertPosition → InsertPositions, HolderStyle → HolderStyles. Именно на
    этом первый прогон зонда и промахнулся, показав пустые списки.
    """
    holder = None
    for hname in (field + "s", field + "es", field + "Types"):
        try:
            holder = getattr(tb, hname)
            break
        except Exception:
            continue
    if holder is None:
        return []
    out = []
    for n in sorted(dir(holder)):
        if n.startswith("_"):
            continue
        try:
            m = getattr(holder, n)
        except Exception:
            continue
        if not callable(m):
            out.append((n, m))
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
parent = setup.CAMGroupCollection.FindObject("GENERIC_MACHINE")

FIELDS = ("InsertShape", "SizeOption", "InsertPosition", "InsertForm",
          "ToolForm", "InsertType", "HolderStyle", "InsertMaterial")

for i, subtype in enumerate(p.get("subtypes", ["OD_55_R"])):
    info = {}
    try:
        tool = setup.CAMGroupCollection.CreateToolWithUserName(
            parent, "turning", subtype,
            NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue,
            f"SHP_{i}", f"Shp{i}")
        tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
    except Exception as e:
        log(f"{subtype}: создать не удалось — {type(e).__name__}: {e}")
        OUT[subtype] = {"error": str(e)}
        continue

    # 1. всё, что вообще похоже на форму пластины
    cand = sorted(n for n in dir(tb)
                  if not n.startswith("_")
                  and any(k in n for k in ("Insert", "Shape", "Form", "Holder",
                                           "Adapter", "Style")))
    info["_candidates"] = cand
    log(f"--- {subtype}: поля про пластину/державку ({type(tb).__name__}) ---")
    log("      " + ", ".join(cand))

    # 2. текущие значения и члены перечислений
    for name in FIELDS:
        try:
            val = getattr(tb, name)
        except Exception as e:
            info[name] = f"<нет: {type(e).__name__}>"
            continue
        mem = enum_members(tb, name)
        info[name] = {"value": repr(val), "type": type(val).__name__,
                      "members": [n for n, _ in mem]}
        log(f"--- {subtype}.{name} = {val!r} <{type(val).__name__}> ---")
        if mem:
            log("      варианты: " + ", ".join(n for n, _ in mem))

    # 3. попытка ПОСТАВИТЬ каждую форму и перечитать после Commit
    try:
        shape = tb.InsertShape
        tried = {}
        for n, m in enum_members(tb, "InsertShape"):
            try:
                setattr(tb, "InsertShape", m)
                got = repr(getattr(tb, "InsertShape"))
                tried[n] = f"принято, стало {got}"
            except Exception as e:
                tried[n] = f"FAIL {type(e).__name__}: {e}"
        info["_set_attempts"] = tried
        log(f"--- {subtype}: попытки задать форму ---")
        for n in sorted(tried):
            log(f"      {n:<24} {tried[n]}")
        try:                       # вернуть исходную, чтобы Commit не упал
            setattr(tb, "InsertShape", shape)
        except Exception:
            pass
    except Exception as e:
        log(f"   InsertShape недоступен: {type(e).__name__}: {e}")

    try:
        tb.Commit()
    except Exception as e:
        log(f"{subtype}: commit FAIL {type(e).__name__}: {e}")
    tb.Destroy()
    OUT[subtype] = info

with open(p["out_json"], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
log(f"OK out={p['out_json']}")
