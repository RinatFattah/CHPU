#!/usr/bin/env python3
"""
nx_compare.py — эталонная деталь + результат симуляции в ОДНОМ .prt со слоями:
слой 1 = деталь (PART_REF), слой 2 = вырез (SIM_RESULT). Оба тела в координатах
программы. Сравнение в NX — штатными галочками слоёв (Layer Settings).

Требует установленный NX (batch-журнал run_journal, лицензия занимается на время).

CLI (одна пара):
  python nx/nx_compare.py деталь_part.step результат_sim.stp [выход_compare.prt]
API (пакетно, один запуск NX на все пары):
  nx_compare.compare_many([{"part": ..., "sim": ..., "out_prt": ...}, ...])
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
from nx import nx_export

_JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "nx_compare_journal.py")


def compare_many(jobs: list, timeout: int | None = None) -> list:
    """jobs: [{"part","sim","out_prt"}]. Возвращает список готовых .prt.
    Один запуск NX на все пары (лицензия берётся один раз)."""
    base = nx_export.find_nx_base()
    if not base:
        raise RuntimeError("Siemens NX не найден (NX_BASE_DIR) — .prt-сравнение недоступно")
    tdir = tempfile.gettempdir()

    # все пути — через временные ASCII-файлы (кириллица + 8.3-обрезка .step)
    prepared, moves = [], []
    for i, job in enumerate(jobs, 1):
        part_tmp = os.path.join(tdir, f"nxcmp_{i}_part.stp")
        sim_tmp = os.path.join(tdir, f"nxcmp_{i}_sim.stp")
        out_tmp = os.path.join(tdir, f"nxcmp_{i}_compare.prt")
        shutil.copyfile(job["part"], part_tmp)
        shutil.copyfile(job["sim"], sim_tmp)
        if os.path.exists(out_tmp):
            os.unlink(out_tmp)
        prepared.append({"part": part_tmp, "sim": sim_tmp, "out_prt": out_tmp})
        moves.append((out_tmp, job["out_prt"]))

    log_path = os.path.join(tdir, "nx_compare_journal.log")
    if os.path.exists(log_path):
        os.unlink(log_path)
    params = {"jobs": prepared, "log_path": log_path}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    rj = os.path.join(base, "NXBIN", "run_journal.exe")
    try:
        proc = subprocess.run(
            [rj, _JOURNAL],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "NX_COMPARE_PARAMS": params_path},
            timeout=timeout or max(600, 120 * len(jobs)),
        )
    finally:
        try:
            os.unlink(params_path)
        except OSError:
            pass

    lines = []
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    lines += (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    if not any("DONE" in l for l in lines if "[nxcmp]" in l):
        tail = "\n".join(l for l in lines if "[nxcmp]" in l)[-500:]
        raise RuntimeError(f"журнал сравнения не завершился (код {proc.returncode}). {tail}")

    out = []
    for src, dst in moves:
        if os.path.exists(src) and os.path.getsize(src) > 0:
            shutil.move(src, dst)
            out.append(dst)
    for job in prepared:   # подчистить временные входы
        for k in ("part", "sim"):
            try:
                os.unlink(job[k])
            except OSError:
                pass
    return out


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 3:
        print("использование: python nx/nx_compare.py деталь_part.step "
              "результат_sim.stp [выход_compare.prt]")
        sys.exit(1)
    part, sim = sys.argv[1], sys.argv[2]
    out = (sys.argv[3] if len(sys.argv) > 3
           else os.path.splitext(os.path.abspath(sim))[0] + "_compare.prt")
    done = compare_many([{"part": part, "sim": sim, "out_prt": out}])
    if not done:
        print("❌ файл сравнения не построился (см. лог журнала)")
        sys.exit(1)
    print(f"✅ .prt со слоями (1 = деталь, 2 = вырез): {done[0]}")


if __name__ == "__main__":
    main()
