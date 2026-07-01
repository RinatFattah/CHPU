#!/usr/bin/env python3
"""
auto_3d.py — Текст → 3D деталь → STL
Использует OpenAI API для генерации Blender Python кода.
Запуск: python3 auto_3d.py
"""

import socket
import json
import re
import sys
import os
from openai import OpenAI

import config

client = OpenAI(
    api_key=config.OPENAI_API_KEY,
    base_url=config.OPENAI_BASE_URL,
)

DEFAULT_STL = os.path.join(os.path.expanduser("~"), "detail.stl")


# ── LLM ───────────────────────────────────────────────────────────────────────
def ask_llm(description: str, stl_path: str) -> str:
    prompt = f"""Напиши Python-скрипт для Blender (bpy) который создаёт 3D деталь.
Задача: {description}

СТРОГИЕ правила:
1. Единицы МЕТРЫ: 1мм = 0.001, 100мм = 0.1, 200мм = 0.2
2. Очисти сцену первым делом:
   for obj in bpy.data.objects:
       bpy.data.objects.remove(obj, do_unlink=True)
3. Основной объект создавай в координатах (0, 0, 0)
4. Отверстия создавай через Boolean DIFFERENCE модификатор:
   - Сначала создай основное тело
   - Потом создай цилиндры-резаки в ПРАВИЛЬНЫХ координатах относительно тела
   - Примени модификатор: bpy.ops.object.modifier_apply(modifier=mod.name)
   - Удали резаки: bpy.data.objects.remove(cutter, do_unlink=True)
5. Для пластины используй primitive_cube_add с нужными размерами через scale:
   bpy.ops.mesh.primitive_cube_add(location=(0,0,0))
   obj = bpy.context.active_object
   obj.scale = (длина/2, ширина/2, толщина/2)
   bpy.ops.object.transform_apply(scale=True)
6. В конце экспортируй (совместимо с Blender 3.x и 4.1+):
   bpy.ops.object.select_all(action="SELECT")
   try:
       bpy.ops.wm.stl_export(filepath="{stl_path}", export_selected_objects=True)
   except Exception:
       bpy.ops.export_mesh.stl(filepath="{stl_path}", use_selection=True)

Только чистый Python код без markdown и объяснений.
Импорты в начале: import bpy, import math
"""
    try:
        response = client.chat.completions.create(
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


# ── Blender MCP ───────────────────────────────────────────────────────────────
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


# ── Главная программа ─────────────────────────────────────────────────────────
def main():
    if not config.OPENAI_API_KEY:
        print("❌ OPENAI_API_KEY не задан — установите переменную окружения")
        sys.exit(1)

    print("=" * 60)
    print("  AUTO 3D — Текст → Blender → STL")
    print(f"  Модель: {config.OPENAI_MODEL} @ {config.OPENAI_BASE_URL}")
    print("=" * 60)

    description = input("\n📝 Опишите деталь: ").strip()
    if not description:
        print("❌ Описание не может быть пустым")
        sys.exit(1)

    stl_input = input(f"📁 Путь для STL [{DEFAULT_STL}]: ").strip()
    stl_path  = stl_input if stl_input else DEFAULT_STL

    print(f"\n⏳ Генерирую код через {config.OPENAI_MODEL}...")
    raw_code = ask_llm(description, stl_path)
    code     = clean_code(raw_code)

    print("\n─── Сгенерированный код ─────────────────────────────────")
    print(code[:800] + ("..." if len(code) > 800 else ""))
    print("─────────────────────────────────────────────────────────")

    confirm = input("\n▶ Выполнить в Blender? [Y/n]: ").strip().lower()
    if confirm == "n":
        print("Отменено.")
        sys.exit(0)

    print("\n⚙ Выполняю в Blender...")
    result = run_in_blender(code)

    if result.get("status") == "success":
        print(f"\n✅ Готово! STL сохранён: {stl_path}")
        try:
            size = os.path.getsize(stl_path)
            print(f"   Размер файла: {size:,} байт")
        except Exception:
            pass
    else:
        print(f"\n❌ Ошибка Blender: {result.get('message', 'неизвестно')}")
        print("   Попробуйте переформулировать описание детали")
        sys.exit(1)


if __name__ == "__main__":
    main()
