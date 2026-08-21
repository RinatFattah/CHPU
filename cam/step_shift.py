#!/usr/bin/env python3
"""
step_shift.py — перенос STEP-тела в другую систему координат (сдвиг по осям).

Зачем: наш генератор ставит ноль детали по ВЕРХНЕЙ плоскости, у заказчика он
бывает по нижней (плоскость стола). Чтобы прогнать чужую программу в её родной
системе координат, двигать надо не программу, а геометрию — заготовку и деталь.
Сдвиг равен высоте детали и берётся из шапки G-кода («Part: … x … x <h>»).

Сдвинутый файл — вход симуляции и эталон сравнения: всё, что дальше меряется,
меряется в ТОЙ ЖЕ раме, в которой написана программа.

CLI:  python cam/step_shift.py вход.step dz [выход.step]
      python cam/step_shift.py вход.step "dx dy dz" [выход.step]
API:  step_shift.shift(in_path, (dx, dy, dz), out_path) -> str
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

# при прямом запуске файла корень репозитория добавляется в sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cam import freecad_cam

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "freecad_shift_worker.py")


def shift(in_path: str, vec, out_path: str | None = None) -> str:
    """Сдвигает тело STEP на vec = (dx, dy, dz). Возвращает путь результата."""
    dx, dy, dz = (float(v) for v in vec)
    fc = freecad_cam.find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден (укажите FREECAD_CMD в конфиге)")
    if out_path is None:
        stem, ext = os.path.splitext(os.path.abspath(in_path))
        out_path = f"{stem}_shifted{ext}"

    # вход — во временную ASCII-копию с расширением .stp (кириллица в путях +
    # 8.3-обрезка «.step» → «.STE», которую OCCT не понимает)
    tdir = tempfile.gettempdir()
    pid = os.getpid()
    in_tmp = os.path.join(tdir, f"shift_in_{pid}.stp")
    out_tmp = os.path.join(tdir, f"shift_out_{pid}.stp")
    shutil.copyfile(in_path, in_tmp)

    params = {"in_step": in_tmp, "out_step": out_tmp, "shift": [dx, dy, dz]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    worker = freecad_cam._ascii_safe(_WORKER)
    if not worker.isascii():
        worker = os.path.join(tdir, "freecad_shift_worker.py")
        shutil.copyfile(_WORKER, worker)

    try:
        proc = subprocess.run(
            [fc, worker],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "FREECAD_SHIFT_PARAMS": params_path,
                 "QT_QPA_PLATFORM": "offscreen"},
            timeout=300,
        )
    finally:
        for _tmp in (params_path, in_tmp):
            try:
                os.unlink(_tmp)
            except OSError:
                pass

    lines = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    for l in lines:
        if "[shift]" in l:
            print(l.split("[shift]", 1)[1].strip())
    ok = next((l for l in lines if "[shift] OK" in l), None)
    if not ok or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) == 0:
        tail = "\n".join(l for l in lines if "[shift]" in l)[-400:]
        raise RuntimeError(f"сдвиг не удался (код {proc.returncode}). {tail}")
    shutil.move(out_tmp, out_path)
    return out_path


def main():
    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    if len(sys.argv) < 3:
        print("использование: python cam/step_shift.py вход.step dz [выход.step]")
        print("              python cam/step_shift.py вход.step \"dx dy dz\" "
              "[выход.step]")
        sys.exit(1)
    src = sys.argv[1]
    if not os.path.exists(src):
        print(f"❌ Файл не найден: {src}")
        sys.exit(1)
    parts = sys.argv[2].replace(",", " ").split()
    vec = (0.0, 0.0, float(parts[0])) if len(parts) == 1 else tuple(parts)
    if len(vec) != 3:
        print("❌ сдвиг задаётся одним числом (dz) или тремя (dx dy dz)")
        sys.exit(1)
    out = sys.argv[3] if len(sys.argv) > 3 else None
    print(f"✅ {shift(src, vec, out)}")


if __name__ == "__main__":
    main()
