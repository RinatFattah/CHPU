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
             verbose=True, stock_profile=None, stock_bore=None, z_mirror=None):
    """Съём материала по программе.

    stock_profile / stock_bore — что осталось от ПРЕДЫДУЩЕГО установа (иначе
    исходный пруток); z_mirror — программа написана в СК перевёрнутой детали
    (второй установ), считать её надо в исходной: z' = z_mirror − z.

    Возвращает {"step", "volume", "profile", "bore"} — профили результата, их и
    передают следующему установу.
    """
    fc = freecad_cam.find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден")

    # ASCII-путь для OCCT + PID в имени: без него два прогона (пакетный и
    # ручной) пишут в одни и те же файлы и молча портят друг другу результат.
    tdir = tempfile.gettempdir()
    pid = os.getpid()
    tmp_g = os.path.join(tdir, f"lathe_sim_{pid}.gcode")
    tmp_step = os.path.join(tdir, f"lathe_sim_out_{pid}.stp")
    tmp_json = os.path.join(tdir, f"lathe_sim_out_{pid}.json")
    shutil.copyfile(gcode_path, tmp_g)
    for f_old in (tmp_step, tmp_json):
        if os.path.exists(f_old):
            os.unlink(f_old)

    params = {
        "gcode": tmp_g,
        "out_step": tmp_step,
        "out_json": tmp_json,
        "stock_radius": prof_data["stock_radius"],
        "stock_z_top": prof_data["stock_z_top"],
        "stock_z_bottom": prof_data["stock_z_bottom"],
        "step": step,
        "diameter_mode": diameter_mode,
        "stock_profile": stock_profile,
        "stock_bore": stock_bore,
        "z_mirror": z_mirror,
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
    res = {"step": out_step, "volume": vol}
    if os.path.exists(tmp_json):
        with open(tmp_json, encoding="utf-8") as f:
            res.update(json.load(f))
        os.unlink(tmp_json)
    return res
