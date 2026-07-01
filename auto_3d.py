#!/usr/bin/env python3
"""
auto_3d.py — Текст → 3D деталь → STL
Использует Ollama (qwen3:14b) для генерации Blender Python кода.
Запуск: python3 auto_3d.py
"""

import requests
import socket
import json
import re
import sys

# ── Настройки ─────────────────────────────────────────────────────────────────
OLLAMA_URL   = "http://192.168.88.50:11435"
OLLAMA_MODEL = "qwen2.5-coder:14b"
BLENDER_HOST = "localhost"
BLENDER_PORT = 9876
DEFAULT_STL  = "/home/rb/detail.stl"

# ── Ollama ────────────────────────────────────────────────────────────────────
def ask_ollama(description: str, stl_path: str) -> str:
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
6. В конце экспортируй:
   bpy.ops.export_mesh.stl(filepath="{stl_path}")

Только чистый Python код без markdown и объяснений.
Импорты в начале: import bpy, import math
"""

    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 1024},
            },
            timeout=300,
        )
        r.raise_for_status()
        return r.json()["response"]
    except Exception as e:
        print(f"❌ Ошибка Ollama: {e}")
        sys.exit(1)


def clean_code(code: str) -> str:
    """Убираем markdown-обёртку если модель добавила."""
    code = re.sub(r"```python\s*", "", code)
    code = re.sub(r"```\s*", "", code)
    # Убираем <think>...</think> (qwen3 thinking mode)
    code = re.sub(r"<think>.*?</think>", "", code, flags=re.DOTALL)
    return code.strip()


# ── Blender MCP ───────────────────────────────────────────────────────────────
def run_in_blender(code: str) -> dict:
    try:
        s = socket.socket()
        s.settimeout(60)
        s.connect((BLENDER_HOST, BLENDER_PORT))
        cmd = json.dumps({"type": "execute_code", "params": {"code": code}})
        s.send(cmd.encode())
        # Читаем ответ (может быть большим)
        chunks = []
        while True:
            try:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                # Пробуем распарсить — если успешно, выходим
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
    print("=" * 60)
    print("  AUTO 3D — Текст → Blender → STL")
    print(f"  Модель: {OLLAMA_MODEL} @ {OLLAMA_URL}")
    print("=" * 60)

    description = input("\n📝 Опишите деталь: ").strip()
    if not description:
        print("❌ Описание не может быть пустым")
        sys.exit(1)

    stl_input = input(f"📁 Путь для STL [{DEFAULT_STL}]: ").strip()
    stl_path  = stl_input if stl_input else DEFAULT_STL

    print(f"\n⏳ Генерирую код через {OLLAMA_MODEL}...")
    raw_code = ask_ollama(description, stl_path)
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
        # Показываем размер файла
        try:
            import os
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
