#!/usr/bin/env python3
"""
step_describe.py — CAD-файл (STEP/IGES/BREP, в т.ч. заготовка `_stock.stp`)
→ JSON-описание геометрии, удобное для обработки LLM.

Сырые STEP плохо пригодны для LLM: это граф сущностей с перекрёстными ссылками
(#123=CARTESIAN_POINT...), а числовых координат — тысячи. Здесь геометрия
читается FreeCAD (headless) и сводится к компактному структурированному JSON:
габарит, объём, грани по типам с размерами и ориентацией, отверстия
(сквозные/глухие, Ø и глубина), плюс однострочная текстовая сводка на русском.

CLI:  python step_describe.py файл.stp [выход.json]
API:  step_describe.describe(path) -> dict
.prt (Siemens NX) конвертируется автоматически при установленном NX.
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
                       "freecad_describe_worker.py")


def describe(model_path: str, json_path: str | None = None,
             max_faces: int = 40) -> dict:
    """Читает CAD-файл и возвращает dict с описанием геометрии.
    json_path — дополнительно сохранить в файл (None = не сохранять)."""
    fc = freecad_cam.find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден (укажите FREECAD_CMD в конфиге)")

    src_name = os.path.basename(model_path)
    if os.path.splitext(model_path)[1].lower() == ".prt":
        from nx import nx_export
        model_path = nx_export.prt_to_step(model_path)

    out_json = json_path or os.path.join(tempfile.gettempdir(), "step_describe.json")
    params = {
        "model_path": freecad_cam._ascii_safe(os.path.abspath(model_path)),
        "json_path": freecad_cam._ascii_safe(
            os.path.dirname(os.path.abspath(out_json)) or ".") + os.sep
            + os.path.basename(out_json),
        "source_name": src_name,
        "max_faces": max_faces,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    # worker для freecadcmd должен лежать по ASCII-пути (та же грабля, что у CAM)
    worker = freecad_cam._ascii_safe(_WORKER)
    if not worker.isascii():
        worker = os.path.join(tempfile.gettempdir(), "freecad_describe_worker.py")
        import shutil
        shutil.copyfile(_WORKER, worker)

    try:
        proc = subprocess.run(
            [fc, worker],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "FREECAD_DESCRIBE_PARAMS": params_path,
                 "QT_QPA_PLATFORM": "offscreen"},
            timeout=300,
        )
    finally:
        try:
            os.unlink(params_path)
        except OSError:
            pass

    lines = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    ok = next((l for l in lines if "[describe] OK" in l), None)
    if not ok or not os.path.exists(out_json):
        tail = "\n".join(l for l in lines if l.strip())[-400:]
        raise RuntimeError(f"описание не построилось (код {proc.returncode}). {tail}")
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
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print("использование: python step_describe.py файл.stp [выход.json]")
        sys.exit(1)
    model = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    if not os.path.exists(model):
        print(f"❌ Файл не найден: {model}")
        sys.exit(1)
    if len(sys.argv) > 3 and sys.argv[3] == "--config":
        config.load(sys.argv[4])
    data = describe(model, out)
    print(json.dumps(data, ensure_ascii=False, indent=1))
    if out:
        print(f"\n[describe] сохранено: {out}")


if __name__ == "__main__":
    main()
