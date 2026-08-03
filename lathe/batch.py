#!/usr/bin/env python3
"""Прогон токарного пайплайна по ВСЕЙ серии деталей КнААЗ с нашей симуляцией.

Для каждой детали: .prt → профиль → программа → съём нашим симулятором →
воксельная сверка с моделью. Результат — таблица в summary.md и summary.json.

    python lathe/batch.py <куда_писать> [флаги run_lathe.py]
    python lathe/batch.py demo/0804/02_seriya_15_detaley --two-setups

Папка с деталями — PARTS_DIR ниже или переменная окружения LATHE_PARTS_DIR.
"""
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARTS_DIR = os.environ.get("LATHE_PARTS_DIR") or (
    r"C:\Users\denis\OneDrive\Работа\BRAInLab\Станки\Примеры деталей"
    r"\Примеры ДСЕ КнААЗ 11.06.2026\Детали_после_ВКС\Токарные")
# OUT переопределяется ниже из argv; здесь — значение по умолчанию

parts = []
for d in sorted(os.listdir(PARTS_DIR)):
    p = os.path.join(PARTS_DIR, d)
    if not os.path.isdir(p):
        continue
    for f in sorted(os.listdir(p)):
        # .cam.prt — это CAM-проект, а не деталь; в пайплайн ему нельзя
        if f.lower().endswith(".prt") and ".cam." not in f.lower():
            parts.append((d, os.path.join(p, f)))
            break

# Куда писать и с какими флагами — из argv, чтобы можно было прогнать серию и в
# один установ, и в два, не заводя копию скрипта.
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "runs/50_all_parts"
EXTRA = sys.argv[2:]
OUT = os.path.join(ROOT, OUT_DIR) if not os.path.isabs(OUT_DIR) else OUT_DIR
os.makedirs(OUT, exist_ok=True)

print(f"деталей: {len(parts)}"
      + (f", флаги: {' '.join(EXTRA)}" if EXTRA else ""), flush=True)
rows = []
for i, (name, path) in enumerate(parts, 1):
    stem = name.split(" ")[0]
    t0 = time.time()
    print(f"[{i}/{len(parts)}] {stem} ...", flush=True)
    r = subprocess.run(
        [sys.executable, "run_lathe.py", path,
         f"{OUT_DIR}/{stem}/out.gcode", "--config", "config.yaml",
         "--simulate-own"] + EXTRA,
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    out = (r.stdout or "") + (r.stderr or "")

    def grab(pat, cast=float, default=None):
        m = re.search(pat, out)
        return cast(m.group(1)) if m else default

    row = {
        "деталь": stem,
        "сек": round(time.time() - t0),
        "код": r.returncode,
        "Dmax": grab(r"Ø([\d.]+) x [\d.]+ мм"),
        "L": grab(r"Ø[\d.]+ x ([\d.]+) мм"),
        "профиль_точек": grab(r"Профиль: (\d+) точек", int),
        "прокат": (re.search(r"прокат: (.+?) —", out) or [None, ""])[1].strip()
                  if re.search(r"прокат: (.+?) —", out) else "",
        "строк": grab(r"Программа: (\d+) строк", int),
        # у двух установов вторая программа своя — без этой колонки в сводке
        # видно только первый установ
        "строк_уст2": grab(r"Программа установа 2: (\d+) строк", int, 0),
        "z_передачи": grab(r"передача работы на z (-?[\d.]+)", float, 0.0),
        "операции": (re.search(r"операции: (.+?)(?: \||$)", out, re.M) or [None, ""])[1].strip()
                    if re.search(r"операции: (.+?)(?: \||$)", out, re.M) else "",
        "пластина": grab(r"пластина ([\d.]+) мм"),
        "канавок": grab(r"(\d+) канавк", int, 0),
        "T3_проходов": grab(r"T3 левый проходной: (\d+) проход", int, 0),
        "не_обработано": grab(r"НЕ ОБРАБОТАНО: ([\d.]+) мм³", float, 0.0),
        "деталь_мм3": grab(r"деталь ([\d.]+) мм³"),
        "недорез": grab(r"недорез ([\d.]+) мм³"),
        "зарез": grab(r"зарез ([\d.]+) мм³"),
    }
    if r.returncode != 0:
        tail = [l for l in out.splitlines() if l.strip()][-3:]
        row["ошибка"] = " | ".join(tail)[:200]
    rows.append(row)
    print(f"    {row}", flush=True)

with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=1)

hdr = ["деталь", "Dmax", "L", "прокат", "строк", "строк_уст2", "z_передачи",
       "деталь_мм3", "недорез", "зарез", "не_обработано", "сек"]
# «канавок» и «T3_проходов» в таблицу не выводим: при двух установах статистика
# берётся из ПЕРВОГО, а канавки и левый резец работают во втором — в колонках
# стояли бы нули, и это читалось бы как «их нет вообще».
with open(os.path.join(OUT, "summary.md"), "w", encoding="utf-8") as f:
    f.write("| " + " | ".join(hdr) + " |\n")
    f.write("|" + "---|" * len(hdr) + "\n")
    for r0 in rows:
        f.write("| " + " | ".join(str(r0.get(k, "")) for k in hdr) + " |\n")
print("ГОТОВО", flush=True)
