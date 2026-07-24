#!/usr/bin/env python3
"""
freecad_combine_worker.py — исполняется ВНУТРИ FreeCAD (freecadcmd).

Складывает НЕСКОЛЬКО тел в ОДИН STEP (все в той же системе координат, что
пришли): эталонная деталь + результат симуляции → файл сравнения, в котором
оба тела лежат друг в друге и расхождение видно глазами.

Параметры (env FREECAD_COMBINE_PARAMS, JSON):
  inputs: [{"path": ..., "label": "PART_REF"}, {"path": ..., "label": "SIM_RESULT"}]
  out_step: путь результата (ASCII)
"""

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

import FreeCAD
import Part


def log(msg):
    print(f"[combine] {msg}", flush=True)


def main():
    with open(os.environ["FREECAD_COMBINE_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)
    doc = FreeCAD.newDocument("Combine")
    feats = []
    for item in p["inputs"]:
        shape = Part.Shape()
        shape.read(item["path"])
        feat = doc.addObject("Part::Feature", item.get("label", "Body"))
        feat.Shape = shape
        feat.Label = item.get("label", "Body")
        feats.append(feat)
        log(f"{item.get('label')}: {os.path.basename(item['path'])} "
            f"({len(shape.Solids)} тел)")
    doc.recompute()
    Part.export(feats, p["out_step"])
    log(f"OK bodies={len(feats)} step={p['out_step']}")


if os.environ.get("FREECAD_COMBINE_PARAMS"):
    try:
        main()
    except Exception:
        import traceback
        for _line in traceback.format_exc().splitlines():
            log(_line)
        raise
