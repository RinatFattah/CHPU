#!/usr/bin/env python3
"""
pipeline_3d.py — Полный pipeline: Текст → STL → G-Code
Использует: Ollama (qwen2.5-coder:14b) + Blender MCP + FreeCAD/базовый G-Code
Запуск: python3 pipeline_3d.py
"""

import requests
import socket
import json
import re
import os
import sys
import struct
import tempfile
import subprocess

# ── Настройки ──────────────────────────────────────────────────────────────────
OLLAMA_URL      = "http://192.168.88.50:11435"
OLLAMA_MODEL    = "qwen2.5-coder:14b"
BLENDER_HOST    = "localhost"
BLENDER_PORT    = 9876
OUTPUT_DIR      = "/home/rb/details"

# Параметры фрезерования
TOOL_DIAMETER   = 6.0
FEED_RATE       = 800
SPINDLE_SPEED   = 12000
DEPTH_OF_CUT    = 1.0
SAFE_HEIGHT     = 10.0

FREECAD_LIB     = "/snap/freecad/2266/usr/lib"

# ── Ollama ─────────────────────────────────────────────────────────────────────
def ask_ollama(description: str, stl_path: str) -> str:
    prompt = f"""Напиши Python-скрипт для Blender (bpy) для создания 3D детали.
Задача: {description}

ТОЧНЫЕ правила (соблюдать строго):
1. Единицы МЕТРЫ: 1мм=0.001, 10мм=0.01, 100мм=0.1, 200мм=0.2
2. Очистка сцены в самом начале:
   for obj in bpy.data.objects:
       bpy.data.objects.remove(obj, do_unlink=True)
3. Прямоугольная пластина через cube:
   bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
   plate = bpy.context.active_object
   plate.scale = (длина_м/2, ширина_м/2, толщина_м/2)
   bpy.ops.object.transform_apply(scale=True)
4. Отверстия через Boolean (ВАЖНО — координаты относительно центра пластины):
   - Угловые отверстия для пластины L x W: x=±(L/2-отступ), y=±(W/2-отступ)
   - Цилиндр-резак должен быть ВЫШЕ и НИЖЕ пластины: depth = толщина+0.01
   - location=(x, y, 0) — по центру пластины по Z
   - Применить модификатор и удалить резак
5. Экспорт в конце:
   bpy.ops.object.select_all(action='SELECT')
   bpy.ops.export_mesh.stl(filepath="{stl_path}", use_selection=True)

Только чистый Python, без markdown, без объяснений.
Начни с: import bpy
         import math
"""
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.1, "num_predict": 1500}},
            timeout=300,
        )
        r.raise_for_status()
        return r.json()["response"]
    except Exception as e:
        print(f"❌ Ошибка Ollama: {e}")
        sys.exit(1)


def clean_code(code: str) -> str:
    code = re.sub(r"```python\s*", "", code)
    code = re.sub(r"```\s*", "", code)
    code = re.sub(r"<think>.*?</think>", "", code, flags=re.DOTALL)
    return code.strip()


# ── Blender MCP ────────────────────────────────────────────────────────────────
def run_in_blender(code: str) -> dict:
    try:
        s = socket.socket()
        s.settimeout(60)
        s.connect((BLENDER_HOST, BLENDER_PORT))
        cmd = json.dumps({"type": "execute_code", "params": {"code": code}})
        s.send(cmd.encode())
        chunks = []
        while True:
            try:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                try:
                    json.loads(b"".join(chunks))
                    break
                except json.JSONDecodeError:
                    continue
            except socket.timeout:
                break
        s.close()
        return json.loads(b"".join(chunks))
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── STL → G-Code ───────────────────────────────────────────────────────────────
def read_stl_bounds(path: str) -> tuple:
    """Читаем размеры детали из бинарного STL."""
    with open(path, "rb") as f:
        f.read(80)
        count = struct.unpack("<I", f.read(4))[0]
        xs, ys, zs = [], [], []
        for _ in range(count):
            f.read(12)
            for _ in range(3):
                x, y, z = struct.unpack("<fff", f.read(12))
                xs.append(x); ys.append(y); zs.append(z)
            f.read(2)
    # Переводим метры → мм
    return (min(xs)*1000, max(xs)*1000,
            min(ys)*1000, max(ys)*1000,
            min(zs)*1000, max(zs)*1000)


def generate_gcode(stl_path: str, gcode_path: str):
    """Генерирует G-Code из STL."""
    xmin, xmax, ymin, ymax, zmin, zmax = read_stl_bounds(stl_path)
    width  = xmax - xmin
    height = ymax - ymin
    depth  = zmax - zmin

    lines = [
        "; ════════════════════════════════════════",
        f"; G-Code: {os.path.basename(stl_path)}",
        f"; Деталь: {width:.1f} x {height:.1f} x {depth:.1f} мм",
        f"; Фреза:  d={TOOL_DIAMETER}мм",
        f"; Подача: {FEED_RATE} мм/мин",
        f"; Шпиндель: {SPINDLE_SPEED} об/мин",
        "; ════════════════════════════════════════",
        "",
        "G21        ; Миллиметры",
        "G90        ; Абсолютные координаты",
        "G17        ; Плоскость XY",
        "G94        ; Подача мм/мин",
        f"G0 Z{SAFE_HEIGHT:.1f}    ; Безопасная высота",
        f"M3 S{SPINDLE_SPEED}  ; Шпиндель ВКЛ",
        "G4 P2      ; Пауза 2 сек (разгон шпинделя)",
        "",
        "; === Контурная обработка ===",
    ]

    # Многопроходная контурная обработка
    z = 0.0
    pass_num = 0
    while z > -depth:
        z = max(-depth, z - DEPTH_OF_CUT)
        pass_num += 1
        lines.append(f"")
        lines.append(f"; --- Проход {pass_num} (Z={z:.2f}мм) ---")
        # Подход к детали
        lines.append(f"G0 X{xmin:.3f} Y{ymin:.3f}")
        lines.append(f"G1 Z{z:.3f} F{FEED_RATE//4}")
        # Контур
        lines.append(f"G1 X{xmax:.3f} Y{ymin:.3f} F{FEED_RATE}")
        lines.append(f"G1 X{xmax:.3f} Y{ymax:.3f}")
        lines.append(f"G1 X{xmin:.3f} Y{ymax:.3f}")
        lines.append(f"G1 X{xmin:.3f} Y{ymin:.3f}")
        # Подъём
        lines.append(f"G0 Z{SAFE_HEIGHT:.1f}")

    lines += [
        "",
        "; === Завершение ===",
        "M5         ; Шпиндель ВЫКЛ",
        f"G0 Z{SAFE_HEIGHT:.1f}",
        "G0 X0 Y0   ; Возврат в ноль",
        "M30        ; Конец программы",
    ]

    with open(gcode_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return len(lines), width, height, depth


# ── Главная программа ──────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  PIPELINE 3D: Текст → Blender → STL → G-Code")
    print(f"  Модель: {OLLAMA_MODEL}")
    print("=" * 60)

    # Создаём папку для деталей
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    description = input("\n📝 Опишите деталь: ").strip()
    if not description:
        print("❌ Описание пустое")
        sys.exit(1)

    # Имя файла из описания
    name = input("📛 Имя детали (латиницей, без пробелов) [detail]: ").strip() or "detail"
    stl_path   = f"{OUTPUT_DIR}/{name}.stl"
    gcode_path = f"{OUTPUT_DIR}/{name}.gcode"

    # ── Шаг 1: Генерация кода ──────────────────────────────────────────────────
    print(f"\n⏳ Шаг 1/3: Генерирую Blender-код через {OLLAMA_MODEL}...")
    raw_code = ask_ollama(description, stl_path)
    code = clean_code(raw_code)
    print(f"✅ Код получен ({len(code)} символов)")

    # Показываем код
    print("\n─── Код ──────────────────────────────────────────────────")
    print(code[:600] + ("..." if len(code) > 600 else ""))
    print("──────────────────────────────────────────────────────────")

    confirm = input("\n▶ Выполнить в Blender? [Y/n]: ").strip().lower()
    if confirm == "n":
        print("Отменено.")
        sys.exit(0)

    # ── Шаг 2: Выполнение в Blender ────────────────────────────────────────────
    print("\n⏳ Шаг 2/3: Создаю 3D деталь в Blender...")
    result = run_in_blender(code)

    if result.get("status") != "success":
        print(f"❌ Ошибка Blender: {result.get('message')}")
        sys.exit(1)

    if not os.path.exists(stl_path):
        print(f"❌ STL не создан: {stl_path}")
        sys.exit(1)

    stl_size = os.path.getsize(stl_path)
    print(f"✅ STL создан: {stl_path} ({stl_size:,} байт)")

    # ── Шаг 3: Генерация G-Code ────────────────────────────────────────────────
    print("\n⏳ Шаг 3/3: Генерирую G-Code...")
    lines, w, h, d = generate_gcode(stl_path, gcode_path)
    print(f"✅ G-Code готов: {gcode_path}")

    # ── Итог ───────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  ✅ ГОТОВО!")
    print("=" * 60)
    print(f"  Деталь:  {w:.1f} x {h:.1f} x {d:.1f} мм")
    print(f"  STL:     {stl_path}")
    print(f"  G-Code:  {gcode_path} ({lines} строк)")
    print(f"  Фреза:   d={TOOL_DIAMETER}мм, подача={FEED_RATE}мм/мин")
    print("=" * 60)

    # Показываем начало G-Code
    with open(gcode_path) as f:
        head = [next(f) for _ in range(15)]
    print("\n--- G-Code (начало) ---")
    print("".join(head))


if __name__ == "__main__":
    main()
