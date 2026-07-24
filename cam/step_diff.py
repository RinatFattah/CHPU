#!/usr/bin/env python3
"""
step_diff.py — булево сравнение детали и результата симуляции → JSON для LLM.

Считает недорез (материал остался, где не должен) и зарез (снято лишнее) между
CAD-моделью детали и вырезанной заготовкой из симуляции (`_sim.stp`). Оба файла
должны быть В ОДНОЙ системе координат (координаты программы: `_part.step` из
`--nx-export` и `_sim.stp` из `--simulate` подходят как есть).

CLI:  python step_diff.py деталь.step результат_sim.stp [выход.json]
API:  step_diff.diff(part_path, result_path) -> dict
"""

import json
import os
import subprocess
import sys
import tempfile

import sys

# при прямом запуске файла корень репозитория добавляется в sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
from cam import freecad_cam

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "freecad_diff_worker.py")


def diff(part_path: str, result_path: str, json_path: str | None = None,
         min_volume: float = 2.0) -> dict:
    """Возвращает dict: недорезы/зарезы с объёмами и координатами."""
    fc = freecad_cam.find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден (укажите FREECAD_CMD в конфиге)")

    # входы — во временные ASCII-копии с расширением .stp: 8.3-имя файла
    # «.step» обрезает расширение в «.STE», и OCCT не узнаёт формат
    import shutil
    tdir = tempfile.gettempdir()
    part_tmp = os.path.join(tdir, "step_diff_part.stp")
    result_tmp = os.path.join(tdir, "step_diff_result.stp")
    shutil.copyfile(part_path, part_tmp)
    shutil.copyfile(result_path, result_tmp)

    out_json = json_path or os.path.join(tempfile.gettempdir(), "step_diff.json")
    params = {
        "part": part_tmp,
        "result": result_tmp,
        "json_path": freecad_cam._ascii_safe(
            os.path.dirname(os.path.abspath(out_json)) or ".") + os.sep
            + os.path.basename(out_json),
        "floor_clearance": float(getattr(config, "FLOOR_CLEARANCE", 0.5)),
        "min_volume": min_volume,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    worker = freecad_cam._ascii_safe(_WORKER)
    if not worker.isascii():
        worker = os.path.join(tempfile.gettempdir(), "freecad_diff_worker.py")
        import shutil
        shutil.copyfile(_WORKER, worker)

    try:
        proc = subprocess.run(
            [fc, worker],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "FREECAD_DIFF_PARAMS": params_path,
                 "QT_QPA_PLATFORM": "offscreen"},
            timeout=600,
        )
    finally:
        for _tmp in (params_path, part_tmp, result_tmp):
            try:
                os.unlink(_tmp)
            except OSError:
                pass

    lines = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    ok = next((l for l in lines if "[diff] OK" in l), None)
    if not ok or not os.path.exists(out_json):
        tail = "\n".join(l for l in lines if "[diff]" in l or "rror" in l)[-500:]
        raise RuntimeError(f"diff не построился (код {proc.returncode}). {tail}")
    with open(out_json, encoding="utf-8") as f:
        data = json.load(f)
    if json_path is None:
        try:
            os.unlink(out_json)
        except OSError:
            pass
    return data


def main():
    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    if len(sys.argv) < 3:
        print("использование: python step_diff.py деталь.step результат_sim.stp [выход.json]")
        sys.exit(1)
    part, result = sys.argv[1], sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else None
    for f in (part, result):
        if not os.path.exists(f):
            print(f"❌ Файл не найден: {f}")
            sys.exit(1)
    data = diff(part, result, out)
    print(json.dumps(data, ensure_ascii=False, indent=1))
    if out:
        print(f"\n[diff] сохранено: {out}")


if __name__ == "__main__":
    main()
