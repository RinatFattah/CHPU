#!/usr/bin/env python3
"""
nx_compare_journal.py — исполняется ВНУТРИ NX (run_journal.exe, headless).

Собирает файлы сравнения: для каждой пары «эталонная деталь + результат
симуляции» создаёт ОДИН .prt, где деталь лежит на СЛОЕ 1, а вырез — на СЛОЕ 2
(оба в одной системе координат). В NX сравнение штатное: Layer Settings,
галочки слоёв 1/2.

Параметры (env NX_COMPARE_PARAMS, JSON):
  jobs: [{"part": "...stp", "sim": "...stp", "out_prt": "....prt"}, ...]
        либо {"bodies": [{"path": "...stp", "layer": 1}, ...], "out_prt": ...}
  log_path: файл маркеров (run_journal может глотать stdout)
Все пути — ASCII (кириллицу NX/транслятор не переваривает).
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

_LOG = None


def log(msg):
    line = f"[nxcmp] {msg}"
    print(line, flush=True)
    if _LOG:
        try:
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def import_step(session, work_part, path):
    """Импорт STEP в ТЕКУЩИЙ парт; возвращает список добавленных тел."""
    before = {b.Tag for b in work_part.Bodies}
    imp = session.DexManager.CreateStep242Importer()
    imp.ImportTo = NXOpen.Step242Importer.ImportToOption.WorkPart
    imp.SetMode(NXOpen.BaseImporter.Mode.NativeFileSystem)
    imp.SewSurfaces = True
    imp.Optimize = True
    imp.ObjectTypes.Solids = True
    imp.ObjectTypes.Surfaces = True
    base = session.GetEnvironmentVariableValue("UGII_BASE_DIR")
    imp.SettingsFile = os.path.join(base, "translators", "step242", "step242ug.def")
    imp.InputFile = path
    imp.FileOpenFlag = False
    imp.ProcessHoldFlag = True
    imp.Commit()
    imp.Destroy()
    return [b for b in work_part.Bodies if b.Tag not in before]


def to_layer(work_part, bodies, layer):
    moved = 0
    for b in bodies:
        try:
            work_part.Layers.MoveDisplayableObjects(layer, [b])
            moved += 1
        except Exception as e:
            log(f"warn: тело на слой {layer} не переехало: {e}")
    return moved


def main():
    global _LOG
    with open(os.environ["NX_COMPARE_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)
    _LOG = p.get("log_path")
    session = NXOpen.Session.GetSession()

    done = 0
    for job in p["jobs"]:
        try:
            fs = session.Parts.FileNew()
            fs.NewFileName = job["out_prt"]
            fs.UseBlankTemplate = True
            fs.Units = NXOpen.Part.Units.Millimeters
            fs.MakeDisplayedPart = True
            fs.Commit()
            fs.Destroy()
            work = session.Parts.Work

            # пара «эталон + вырез» — частный случай; общая форма это список
            # тел со своими слоями (нужна, когда в одной сцене сравниваются
            # РАЗНЫЕ ПРОГРАММЫ на одной детали: 1 модель, 2 заводская, 3 наша)
            items = job.get("bodies") or [{"path": job["part"], "layer": 1},
                                          {"path": job["sim"], "layer": 2}]
            counts = []
            for it in items:
                bodies = import_step(session, work, it["path"])
                counts.append((it["layer"], to_layer(work, bodies, it["layer"])))
            try:  # все слои видимы, рабочий — первый
                for layer, _ in counts[1:]:
                    work.Layers.SetState(layer, NXOpen.Layer.State.Selectable)
                work.Layers.SetState(counts[0][0], NXOpen.Layer.State.WorkLayer)
            except Exception as e:
                log(f"warn: состояние слоёв: {e}")

            sv = work.Save(NXOpen.BasePart.SaveComponents.TrueValue,
                           NXOpen.BasePart.CloseAfterSave.TrueValue)
            sv.Dispose()
            done += 1
            log(f"OKJOB {os.path.basename(job['out_prt'])}: "
                + ", ".join(f"слой{l}={n} тел" for l, n in counts))
        except Exception as e:
            log(f"FAILJOB {os.path.basename(job.get('out_prt', '?'))}: {e}")
    log(f"DONE jobs={done}/{len(p['jobs'])}")


if os.environ.get("NX_COMPARE_PARAMS"):
    try:
        main()
    except Exception:
        import traceback
        for _line in traceback.format_exc().splitlines():
            log(_line)
