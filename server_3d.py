#!/usr/bin/env python3
"""
server_3d.py — API сервер для 3D pipeline
Принимает описание детали → возвращает STL + G-Code файлы
Запуск: python3 server_3d.py
"""

import os
import re
import json
import struct
import socket
import datetime
import argparse
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from openai import OpenAI
import uvicorn

import config

# Клиент создаётся лениво — после того как config может быть переопределён через --config
_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
    return _client

app = FastAPI(title="3D Pipeline API", version="2.0")


class DetailRequest(BaseModel):
    description: str
    name: str | None = None


# ── Вспомогательные функции ────────────────────────────────────────────────────

def build_blender_prompt(description: str, stl_path: str) -> str:
    return f"""Напиши Python-скрипт для Blender (bpy) для создания 3D детали.
Задача: {description}

ТОЧНЫЕ правила:
1. Единицы МЕТРЫ: 1мм=0.001, 10мм=0.01, 100мм=0.1, 200мм=0.2
2. Очистка сцены:
   for obj in bpy.data.objects:
       bpy.data.objects.remove(obj, do_unlink=True)
3. Куб/пластина:
   bpy.ops.mesh.primitive_cube_add(location=(0,0,0))
   body = bpy.context.active_object
   body.scale = (длина/2, ширина/2, высота/2)
   bpy.ops.object.transform_apply(scale=True)
4. Отверстие — ОБЯЗАТЕЛЬНО так (3 шага):
   # Шаг A: создать цилиндр-резак
   bpy.ops.mesh.primitive_cylinder_add(radius=радиус, depth=глубина+0.002, location=(x, y, z))
   cutter = bpy.context.active_object
   # Шаг B: добавить Boolean модификатор к основному телу
   bpy.context.view_layer.objects.active = body
   mod = body.modifiers.new("hole", "BOOLEAN")
   mod.operation = "DIFFERENCE"
   mod.object = cutter
   # Шаг C: применить модификатор (ОБЯЗАТЕЛЬНО!)
   bpy.ops.object.modifier_apply(modifier="hole")
   # Шаг D: удалить резак
   bpy.data.objects.remove(cutter, do_unlink=True)
5. Экспорт (совместимо с Blender 3.x и 4.1+):
   bpy.ops.object.select_all(action="SELECT")
   try:
       bpy.ops.wm.stl_export(filepath="{stl_path}", export_selected_objects=True)
   except Exception:
       bpy.ops.export_mesh.stl(filepath="{stl_path}", use_selection=True)

Только чистый Python без markdown.
Начни с: import bpy
         import math
"""


def ask_llm(description: str, stl_path: str) -> str:
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
            {"role": "user", "content": build_blender_prompt(description, stl_path)},
        ],
    )
    return response.choices[0].message.content


def clean_code(code: str) -> str:
    # Если модель обернула код в ```...``` — берём ТОЛЬКО содержимое блока,
    # отбрасывая любую прозу до и после (частая причина SyntaxError в Blender).
    m = re.search(r"```(?:python)?\s*\n(.*?)```", code, re.DOTALL)
    if m:
        code = m.group(1)
    else:
        code = re.sub(r"```(?:python)?", "", code)
    return code.strip()


def generate_valid_code(description: str, stl_path: str, attempts: int = 2) -> str:
    """Генерирует Blender-код и проверяет его синтаксис перед отправкой в Blender.
    При SyntaxError повторяет запрос (модель недетерминирована)."""
    last_err = "неизвестная ошибка"
    for _ in range(attempts):
        code = clean_code(ask_llm(description, stl_path))
        if not code.strip():
            last_err = "пустой ответ модели (не хватило бюджета OPENAI_MAX_TOKENS?)"
            continue
        try:
            compile(code, "<generated>", "exec")
            return code
        except SyntaxError as e:
            last_err = f"синтаксическая ошибка, строка {e.lineno}: {e.msg}"
    raise RuntimeError(
        f"LLM не вернул корректный код после {attempts} попыток ({last_err})"
    )


def run_in_blender(code: str) -> dict:
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


def generate_gcode(stl_path: str, gcode_path: str) -> int:
    xmin, xmax, ymin, ymax, zmin, zmax = read_stl_bounds(stl_path)
    width  = xmax - xmin
    height = ymax - ymin
    depth  = zmax - zmin

    lines = [
        f"; G-Code: {os.path.basename(stl_path)}",
        f"; Деталь: {width:.1f} x {height:.1f} x {depth:.1f} мм",
        f"; Фреза: d={config.TOOL_DIAMETER}мм, подача={config.FEED_RATE}мм/мин",
        "",
        "G21 G90 G17 G94",
        f"G0 Z{config.SAFE_HEIGHT:.1f}",
        f"M3 S{config.SPINDLE_SPEED}",
        "G4 P2",
        "",
        "; === Контурная обработка ===",
    ]

    z = 0.0
    pass_num = 0
    while z > -depth:
        z = max(-depth, z - config.DEPTH_OF_CUT)
        pass_num += 1
        lines += [
            f"; Проход {pass_num} Z={z:.2f}",
            f"G0 X{xmin:.3f} Y{ymin:.3f}",
            f"G1 Z{z:.3f} F{config.FEED_RATE//4}",
            f"G1 X{xmax:.3f} Y{ymin:.3f} F{config.FEED_RATE}",
            f"G1 X{xmax:.3f} Y{ymax:.3f}",
            f"G1 X{xmin:.3f} Y{ymax:.3f}",
            f"G1 X{xmin:.3f} Y{ymin:.3f}",
            f"G0 Z{config.SAFE_HEIGHT:.1f}",
        ]

    lines += ["M5", "G0 X0 Y0", "M30"]

    with open(gcode_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(lines)


# ── API endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Проверка работоспособности сервера."""
    try:
        s = socket.socket()
        s.settimeout(3)
        s.connect((config.BLENDER_HOST, config.BLENDER_PORT))
        s.close()
        blender_ok = True
    except Exception:
        blender_ok = False

    openai_ok = bool(config.OPENAI_API_KEY)

    return {
        "status":  "ok",
        "blender": "✅" if blender_ok else "❌",
        "openai":  "✅" if openai_ok  else "❌ (OPENAI_API_KEY не задан)",
        "model":   config.OPENAI_MODEL,
    }


@app.post("/make")
def make_detail(req: DetailRequest):
    """
    Создаёт 3D деталь по текстовому описанию.
    Возвращает пути к STL и G-Code файлам.
    """
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = req.name or f"detail_{ts}"
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)

    stl_path   = os.path.join(config.OUTPUT_DIR, f"{name}.stl")
    gcode_path = os.path.join(config.OUTPUT_DIR, f"{name}.gcode")

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    # Шаг 1: LLM → код (с проверкой синтаксиса и повтором при ошибке)
    try:
        code = generate_valid_code(req.description, stl_path)
    except Exception as e:
        raise HTTPException(500, f"Ошибка генерации кода: {e}")

    # Шаг 2: Blender → STL
    try:
        result = run_in_blender(code)
        if result.get("status") != "success":
            raise HTTPException(500, f"Ошибка Blender: {result.get('message')}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Ошибка Blender MCP: {e}")

    if not os.path.exists(stl_path):
        raise HTTPException(500, "STL файл не создан")

    # Шаг 3: STL → G-Code
    try:
        gcode_lines = generate_gcode(stl_path, gcode_path)
    except Exception as e:
        raise HTTPException(500, f"Ошибка G-Code: {e}")

    return {
        "status":           "ok",
        "name":             name,
        "stl_path":         stl_path,
        "gcode_path":       gcode_path,
        "stl_size":         os.path.getsize(stl_path),
        "gcode_lines":      gcode_lines,
        "gcode_size":       os.path.getsize(gcode_path),
        "blender_code_len": len(code),
    }


@app.get("/file/stl/{name}")
def get_stl(name: str):
    """Скачать STL файл."""
    path = os.path.join(config.OUTPUT_DIR, f"{name}.stl")
    if not os.path.exists(path):
        raise HTTPException(404, "STL не найден")
    return FileResponse(path, filename=f"{name}.stl",
                        media_type="application/octet-stream")


@app.get("/file/gcode/{name}")
def get_gcode(name: str):
    """Скачать G-Code файл."""
    path = os.path.join(config.OUTPUT_DIR, f"{name}.gcode")
    if not os.path.exists(path):
        raise HTTPException(404, "G-Code не найден")
    return FileResponse(path, filename=f"{name}.gcode",
                        media_type="text/plain")


@app.get("/list")
def list_details():
    """Список всех созданных деталей."""
    files = []
    for f in sorted(Path(config.OUTPUT_DIR).glob("*.stl")):
        gcode = f.with_suffix(".gcode")
        files.append({
            "name":       f.stem,
            "stl_size":   f.stat().st_size,
            "gcode_size": gcode.stat().st_size if gcode.exists() else 0,
            "created":    datetime.datetime.fromtimestamp(
                              f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return {"details": files, "count": len(files)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="3D Pipeline API Server")
    parser.add_argument(
        "--config", metavar="FILE",
        help="Путь к JSON-файлу конфигурации (переопределяет дефолты и env-переменные)"
    )
    args = parser.parse_args()

    if args.config:
        config.load(args.config)
        print(f"[config] Загружен файл: {args.config}")

    if not config.OPENAI_API_KEY:
        print("⚠  OPENAI_API_KEY не задан — укажите в конфиг-файле или переменной окружения")

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    print(f"🚀 3D Pipeline API Server")
    print(f"   Model:   {config.OPENAI_MODEL} @ {config.OPENAI_BASE_URL}")
    print(f"   Blender: {config.BLENDER_HOST}:{config.BLENDER_PORT}")
    print(f"   Output:  {config.OUTPUT_DIR}")
    print(f"   API:     http://{config.SERVER_HOST}:{config.SERVER_PORT}")
    print(f"   Docs:    http://localhost:{config.SERVER_PORT}/docs")
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)
