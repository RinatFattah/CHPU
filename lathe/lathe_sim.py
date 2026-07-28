#!/usr/bin/env python3
"""
lathe_sim.py — хост съёма материала по токарной программе (см. cam/lathe_sim_worker.py).
Возвращает {"step": путь, "volume": мм³}.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
from cam import freecad_cam

_WORKER = os.path.join(_ROOT, "cam", "lathe_sim_worker.py")


def simulate(gcode_path, prof_data, out_step, step=0.05, diameter_mode=True,
             verbose=True):
    fc = freecad_cam.find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден")

    tdir = tempfile.gettempdir()
    tmp_g = os.path.join(tdir, "lathe_sim.gcode")     # OCCT/py — ASCII-путь
    tmp_step = os.path.join(tdir, "lathe_sim_out.stp")
    shutil.copyfile(gcode_path, tmp_g)
    if os.path.exists(tmp_step):
        os.unlink(tmp_step)

    params = {
        "gcode": tmp_g,
        "out_step": tmp_step,
        "stock_radius": prof_data["stock_radius"],
        "stock_z_top": prof_data["stock_z_top"],
        "stock_z_bottom": prof_data["stock_z_bottom"],
        "step": step,
        "diameter_mode": diameter_mode,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as t:
        json.dump(params, t)
        params_path = t.name

    w = freecad_cam._ascii_safe(_WORKER)
    if not w.isascii():
        w = os.path.join(tdir, "lathe_sim_worker.py")
        shutil.copyfile(_WORKER, w)

    try:
        proc = subprocess.run(
            [fc, w], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env={**os.environ, "FREECAD_LATHE_SIM_PARAMS": params_path,
                 "QT_QPA_PLATFORM": "offscreen"},
            timeout=getattr(config, "FREECAD_TIMEOUT", 600))
    finally:
        try:
            os.unlink(params_path)
        except OSError:
            pass

    lines = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    if verbose:
        for l in lines:
            if "[sim]" in l:
                print(l.rstrip())
    ok = next((l for l in lines if "[sim] OK" in l), None)
    if not ok or not os.path.exists(tmp_step):
        tail = "\n".join(l for l in lines if "[sim]" in l or "rror" in l
                         or "Traceback" in l)[-800:]
        raise RuntimeError(f"съём не смоделирован (код {proc.returncode}).\n{tail}")

    shutil.move(tmp_step, out_step)
    vol = float(ok.split("volume=")[1].split()[0])
    return {"step": out_step, "volume": vol}
