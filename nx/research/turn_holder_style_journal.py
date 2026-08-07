"""Разведка: какой УГОЛ В ПЛАНЕ φ даёт каждый стиль державки.

Зачем. Волочение резца по уже обточенному задаёт вспомогательный угол
φ₁ = 180 − φ − ε. Наш генератор считает достижимость под φ = 93° (DCMT в
державке 93°), а шаблон NX даёт φ = 107.5° — отсюда φ₁ = 17.5° вместо 32°, и
спуски, заложенные генератором, резец в ISV не отслеживает: на 14-31A это дало
конус вместо цилиндра на шестиграннике (runs/87).

Стилей державки в NX 22 (A…V). Подбирать их прогонами ISV по 90 с нельзя, да и
не нужно: φ читается прямо из резца. ВАЖНО — читать надо под GENERIC_MACHINE:
в кармане револьвера то же свойство `OrientAngle` показывает уже φ₁, а не φ
(у шаблона 107.5 под GENERIC и 17.5 в кармане, что и есть 180 − 107.5 − 55).

Гипотеза «буквы = коды ISO 5608, J = 93°» ОПРОВЕРГНУТА замером: с J зарез на
шестиграннике вырос с 1.37 до 2.72 мм, то есть φ стал БОЛЬШЕ, а не меньше.
Поэтому — сплошной перебор.

Параметры env NX_TURN_HOLDER (JSON): work_prt, out_json, log_path, subtypes.
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

p = json.load(open(os.environ["NX_TURN_HOLDER"], encoding="utf-8"))
LOG = p.get("log_path")
OUT = {}


def log(m):
    line = "[holder] " + str(m)
    print(line, flush=True)
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


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

SUBTYPE = p.get("subtype", "OD_55_R")
SHAPE = p.get("shape", "Diamond55")
EPS = float(p.get("eps", 55.0))          # угол при вершине выбранной пластины


def make(i, style):
    tool = setup.CAMGroupCollection.CreateToolWithUserName(
        parent, "turning", SUBTYPE,
        NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue,
        f"HLD_{i}", f"Hld{i}")
    tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
    try:
        tb.InsertShape = getattr(tb.InsertShapes, SHAPE)
    except Exception as e:
        log(f"   форма {SHAPE}: FAIL {type(e).__name__}")
    if style is not None:
        tb.HolderStyle = getattr(tb.HolderStyles, style)
    tb.Commit()
    tb.Destroy()
    tb2 = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
    phi = tb2.OrientAngleBuilder.Value
    nose = tb2.NoseAngleBuilder.Value
    tb2.Destroy()
    return phi, nose


styles = None
try:
    probe = setup.CAMGroupCollection.CreateToolWithUserName(
        parent, "turning", SUBTYPE,
        NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue, "HLD_X", "HldX")
    tbp = setup.CAMGroupCollection.CreateMillToolBuilder(probe)
    styles = sorted(n for n in dir(tbp.HolderStyles) if not n.startswith("_")
                    and not callable(getattr(tbp.HolderStyles, n)))
    tbp.Destroy()
except Exception as e:
    log(f"список стилей не получен: {type(e).__name__}: {e}")

log(f"подтип {SUBTYPE}, форма {SHAPE} (ε = {EPS:g}°); ищем φ = 93°, "
    f"то есть φ₁ = 180 − φ − ε = {180 - 93 - EPS:g}°")
log(f"{'стиль':>8} {'φ (в плане)':>12} {'ε':>7} {'φ₁ = 180−φ−ε':>14}")
for i, style in enumerate([None] + (styles or [])):
    try:
        phi, nose = make(i, style)
    except Exception as e:
        log(f"{str(style):>8}   не создался: {type(e).__name__}")
        continue
    phi1 = 180.0 - phi - nose
    OUT[str(style)] = {"phi": phi, "eps": nose, "phi1": phi1}
    mark = "  ← 93°!" if abs(phi - 93.0) < 1.0 else ""
    log(f"{str(style):>8} {phi:>12.2f} {nose:>7.2f} {phi1:>14.2f}{mark}")

with open(p["out_json"], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
log(f"OK out={p['out_json']}")
