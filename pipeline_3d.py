#!/usr/bin/env python3
"""
pipeline_3d.py — Полный pipeline: Текст → STL → G-Code
Использует: OpenAI API + Blender MCP
Запуск: python3 pipeline_3d.py
"""

import socket
import json
import re
import os
import sys
import struct
import argparse
from openai import OpenAI

import config

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
    return _client


# ── LLM ────────────────────────────────────────────────────────────────────────
def ask_llm(description: str, stl_path: str) -> str:
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
5. Экспорт в конце (совместимо с Blender 3.x и 4.1+):
   bpy.ops.object.select_all(action='SELECT')
   try:
       bpy.ops.wm.stl_export(filepath="{stl_path}", export_selected_objects=True)
   except Exception:
       bpy.ops.export_mesh.stl(filepath="{stl_path}", use_selection=True)

Только чистый Python, без markdown, без объяснений.
Начни с: import bpy
         import math
"""
    try:
        response = get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            temperature=config.OPENAI_TEMPERATURE,
            max_tokens=config.OPENAI_MAX_TOKENS,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты эксперт по Blender Python API (bpy). "
                        "Пиши только чистый Python-код без markdown и объяснений."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ Ошибка OpenAI: {e}")
        sys.exit(1)


def clean_code(code: str) -> str:
    # Берём только тело код-блока ```...```, отбрасывая прозу до и после.
    m = re.search(r"```(?:python)?\s*\n(.*?)```", code, re.DOTALL)
    if m:
        code = m.group(1)
    else:
        code = re.sub(r"```(?:python)?", "", code)
    return code.strip()


# ── Blender MCP ────────────────────────────────────────────────────────────────
def run_in_blender(code: str) -> dict:
    try:
        s = socket.socket()
        s.settimeout(60)
        s.connect((config.BLENDER_HOST, config.BLENDER_PORT))
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
    return (min(xs)*1000, max(xs)*1000,
            min(ys)*1000, max(ys)*1000,
            min(zs)*1000, max(zs)*1000)


def generate_gcode(stl_path: str, gcode_path: str):
    xmin, xmax, ymin, ymax, zmin, zmax = read_stl_bounds(stl_path)
    width  = xmax - xmin
    height = ymax - ymin
    depth  = zmax - zmin

    lines = [
        "; ════════════════════════════════════════",
        f"; G-Code: {os.path.basename(stl_path)}",
        f"; Деталь: {width:.1f} x {height:.1f} x {depth:.1f} мм",
        f"; Фреза:  d={config.TOOL_DIAMETER}мм",
        f"; Подача: {config.FEED_RATE} мм/мин",
        f"; Шпиндель: {config.SPINDLE_SPEED} об/мин",
        "; ════════════════════════════════════════",
        "",
        "G21        ; Миллиметры",
        "G90        ; Абсолютные координаты",
        "G17        ; Плоскость XY",
        "G94        ; Подача мм/мин",
        f"G0 Z{config.SAFE_HEIGHT:.1f}    ; Безопасная высота",
        f"M3 S{config.SPINDLE_SPEED}  ; Шпиндель ВКЛ",
        "G4 P2      ; Пауза 2 сек (разгон шпинделя)",
        "",
        "; === Контурная обработка ===",
    ]

    z = 0.0
    pass_num = 0
    while z > -depth:
        z = max(-depth, z - config.DEPTH_OF_CUT)
        pass_num += 1
        lines.append(f"")
        lines.append(f"; --- Проход {pass_num} (Z={z:.2f}мм) ---")
        lines.append(f"G0 X{xmin:.3f} Y{ymin:.3f}")
        lines.append(f"G1 Z{z:.3f} F{config.FEED_RATE//4}")
        lines.append(f"G1 X{xmax:.3f} Y{ymin:.3f} F{config.FEED_RATE}")
        lines.append(f"G1 X{xmax:.3f} Y{ymax:.3f}")
        lines.append(f"G1 X{xmin:.3f} Y{ymax:.3f}")
        lines.append(f"G1 X{xmin:.3f} Y{ymin:.3f}")
        lines.append(f"G0 Z{config.SAFE_HEIGHT:.1f}")

    lines += [
        "",
        "; === Завершение ===",
        "M5         ; Шпиндель ВЫКЛ",
        f"G0 Z{config.SAFE_HEIGHT:.1f}",
        "G0 X0 Y0   ; Возврат в ноль",
        "M30        ; Конец программы",
    ]

    with open(gcode_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return len(lines), width, height, depth


# ── Главная программа ──────────────────────────────────────────────────────────
def main():
    if not config.OPENAI_API_KEY:
        print("❌ OPENAI_API_KEY не задан — установите переменную окружения")
        sys.exit(1)

    print("=" * 60)
    print("  PIPELINE 3D: Текст → Blender → STL → G-Code")
    print(f"  Модель: {config.OPENAI_MODEL} @ {config.OPENAI_BASE_URL}")
    print("=" * 60)

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    description = input("\n📝 Опишите деталь: ").strip()
    if not description:
        print("❌ Описание пустое")
        sys.exit(1)

    name       = input("📛 Имя детали (латиницей, без пробелов) [detail]: ").strip() or "detail"
    stl_path   = os.path.join(config.OUTPUT_DIR, f"{name}.stl")
    gcode_path = os.path.join(config.OUTPUT_DIR, f"{name}.gcode")

    # ── Шаг 1: Генерация кода ──────────────────────────────────────────────────
    print(f"\n⏳ Шаг 1/3: Генерирую Blender-код через {config.OPENAI_MODEL}...")
    raw_code = ask_llm(description, stl_path)
    code = clean_code(raw_code)
    print(f"✅ Код получен ({len(code)} символов)")

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

    print("\n" + "=" * 60)
    print("  ✅ ГОТОВО!")
    print("=" * 60)
    print(f"  Деталь:  {w:.1f} x {h:.1f} x {d:.1f} мм")
    print(f"  STL:     {stl_path}")
    print(f"  G-Code:  {gcode_path} ({lines} строк)")
    print(f"  Фреза:   d={config.TOOL_DIAMETER}мм, подача={config.FEED_RATE}мм/мин")
    print("=" * 60)

    with open(gcode_path) as f:
        head = [next(f) for _ in range(15)]
    print("\n--- G-Code (начало) ---")
    print("".join(head))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Text to STL and G-Code pipeline")
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="Путь к YAML-файлу конфигурации",
    )
    args = parser.parse_args()

    if args.config:
        config.load(args.config)
        print(f"[config] Загружен файл: {args.config}")

    main()
