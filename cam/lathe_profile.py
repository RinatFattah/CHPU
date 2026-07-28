#!/usr/bin/env python3
"""
lathe_profile.py — хост-часть извлечения осевого профиля: запускает
lathe_worker.py внутри freecadcmd и возвращает профиль как dict.

Устройство то же, что у freecad_cam.py: FreeCAD — отдельный headless-процесс,
параметры через временный JSON в env-переменной (argv нельзя — freecadcmd
исполняет каждый файловый аргумент как скрипт), пути к моделям приводятся к
ASCII 8.3 (OCCT на Windows не открывает кириллицу).
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

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lathe_worker.py")


def extract(model_path, out_json=None, part_out=None, stock_out=None,
            profile_step=0.1, simplify_tol=0.01, stock_radial=1.0,
            stock_face=1.0, stock_tail=5.0, verbose=True):
    """STEP/IGES/BREP → dict с профилем. Бросает RuntimeError."""
    fc = freecad_cam.find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден (укажите FREECAD_CMD в конфиге)")

    tdir = tempfile.gettempdir()
    out_json = out_json or os.path.join(tdir, "lathe_profile.json")
    # OCCT пишет STEP только по ASCII-пути: работаем во временной папке
    tmp_part = os.path.join(tdir, "lathe_part.stp") if part_out else ""
    tmp_stock = os.path.join(tdir, "lathe_stock.stp") if stock_out else ""
    tmp_json = os.path.join(tdir, "lathe_profile_out.json")
    for f in (tmp_part, tmp_stock, tmp_json):
        if f and os.path.exists(f):
            os.unlink(f)

    params = {
        "model_path": freecad_cam._ascii_safe(os.path.abspath(model_path)),
        "out_json": tmp_json,
        "part_out": tmp_part,
        "stock_out": tmp_stock,
        "profile_step": profile_step,
        "simplify_tol": simplify_tol,
        "stock_radial": stock_radial,
        "stock_face": stock_face,
        "stock_tail": stock_tail,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as t:
        json.dump(params, t)
        params_path = t.name

    worker = freecad_cam._worker_path.__wrapped__ if False else _WORKER
    w = freecad_cam._ascii_safe(worker)
    if not w.isascii():
        w = os.path.join(tdir, "lathe_worker.py")
        shutil.copyfile(_WORKER, w)

    try:
        proc = subprocess.run(
            [fc, w], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env={**os.environ, "FREECAD_LATHE_PARAMS": params_path,
                 "QT_QPA_PLATFORM": "offscreen"},
            timeout=getattr(config, "FREECAD_TIMEOUT", 600),
        )
    finally:
        try:
            os.unlink(params_path)
        except OSError:
            pass

    lines = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    if verbose:
        for l in lines:
            if "[worker]" in l:
                print(l.rstrip())
    ok = next((l for l in lines if "[worker] OK" in l), None)
    if not ok or not os.path.exists(tmp_json):
        tail = "\n".join(l for l in lines if "[worker]" in l or "rror" in l
                         or "Traceback" in l)[-800:]
        raise RuntimeError(f"профиль не извлечён (код {proc.returncode}).\n{tail}")

    with open(tmp_json, encoding="utf-8") as f:
        data = json.load(f)
    if out_json:
        shutil.copyfile(tmp_json, out_json)
    if part_out and os.path.exists(tmp_part):
        shutil.move(tmp_part, part_out)
    if stock_out and os.path.exists(tmp_stock):
        shutil.move(tmp_stock, stock_out)
    return data


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(sys.argv) < 2:
        print("использование: python cam/lathe_profile.py деталь.step [профиль.json]")
        sys.exit(1)
    d = extract(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps(d, ensure_ascii=False, indent=1)[:2000])
