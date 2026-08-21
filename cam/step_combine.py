#!/usr/bin/env python3
"""
step_combine.py — эталонная деталь + результат симуляции в ОДНОМ STEP-файле.

Оба тела уже в координатах программы (`_part.step` из генерации и `_sim.stp`
из симуляции), поэтому в файле сравнения они лежат друг в друге: тело
PART_REF — какой деталь должна быть, SIM_RESULT — что реально вырезано.
Открой в NX/любом вьюере и включай/выключай тела — расхождение видно глазами.

CLI:  python cam/step_combine.py деталь_part.step результат_sim.stp [выход_compare.stp]
API:  step_combine.combine(part_path, sim_path, out_path) -> str
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
                       "freecad_combine_worker.py")


def combine(part_path: str, sim_path: str, out_path: str | None = None) -> str:
    """Склеивает деталь и результат в один STEP. Возвращает путь результата."""
    if out_path is None:
        out_path = os.path.splitext(os.path.abspath(sim_path))[0] + "_compare.stp"
    return combine_many([(part_path, "PART_REF"), (sim_path, "SIM_RESULT")],
                        out_path)


def combine_many(inputs, out_path: str) -> str:
    """Складывает ЛЮБОЕ число тел в один STEP. inputs: [(путь, метка), ...].

    Больше двух нужно, когда в одной сцене сравниваются РАЗНЫЕ ПРОГРАММЫ на
    одной детали (эталон + заводская + наша): метки видны в дереве вьюера,
    тела лежат друг в друге, лишнее гасится галочкой."""
    fc = freecad_cam.find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден (укажите FREECAD_CMD в конфиге)")

    # входы — во временные ASCII-копии с расширением .stp (кириллица в путях +
    # 8.3-обрезка «.step» → «.STE», которую OCCT не понимает).
    # PID в имени: без него пакетный и ручной прогон писали в одни файлы.
    tdir = tempfile.gettempdir()
    pid = os.getpid()
    out_tmp = os.path.join(tdir, f"combine_out_{pid}.stp")
    items, tmps = [], []
    for i, (src, label) in enumerate(inputs):
        if not os.path.exists(src):
            raise RuntimeError(f"файл не найден: {src}")
        tmp = os.path.join(tdir, f"combine_{pid}_{i}.stp")
        shutil.copyfile(src, tmp)
        tmps.append(tmp)
        items.append({"path": tmp, "label": label})

    params = {"inputs": items, "out_step": out_tmp}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    worker = freecad_cam._ascii_safe(_WORKER)
    if not worker.isascii():
        worker = os.path.join(tdir, "freecad_combine_worker.py")
        shutil.copyfile(_WORKER, worker)

    try:
        proc = subprocess.run(
            [fc, worker],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "FREECAD_COMBINE_PARAMS": params_path,
                 "QT_QPA_PLATFORM": "offscreen"},
            timeout=300,
        )
    finally:
        for _tmp in [params_path] + tmps:
            try:
                os.unlink(_tmp)
            except OSError:
                pass

    lines = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    ok = next((l for l in lines if "[combine] OK" in l), None)
    if not ok or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) == 0:
        tail = "\n".join(l for l in lines if "[combine]" in l)[-400:]
        raise RuntimeError(f"склейка не удалась (код {proc.returncode}). {tail}")
    shutil.move(out_tmp, out_path)
    return out_path


def main():
    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    args = sys.argv[1:]
    if len(args) < 3:
        print("использование: python cam/step_combine.py деталь_part.step "
              "результат_sim.stp [выход_compare.stp]")
        print("               python cam/step_combine.py выход.stp "
              "тело1.stp=МЕТКА1 тело2.stp=МЕТКА2 ...")
        sys.exit(1)
    if "=" in "".join(args[1:]):          # форма с метками: первый файл — выход
        out, inputs = args[0], []
        for a in args[1:]:
            src, _, label = a.partition("=")
            inputs.append((src, label or os.path.splitext(
                os.path.basename(src))[0].upper()))
        for src, _ in inputs:
            if not os.path.exists(src):
                print(f"❌ Файл не найден: {src}")
                sys.exit(1)
        print(f"✅ файл сравнения: {combine_many(inputs, out)}")
        return
    part, sim = args[0], args[1]
    out = args[2] if len(args) > 2 else None
    for f in (part, sim):
        if not os.path.exists(f):
            print(f"❌ Файл не найден: {f}")
            sys.exit(1)
    print(f"✅ файл сравнения: {combine(part, sim, out)}")


if __name__ == "__main__":
    main()
