#!/usr/bin/env python3
"""
server_3d.py — API сервер для 3D pipeline на WS-BLENDER
Принимает описание детали → возвращает STL + G-Code файлы
Запуск: python3 server_3d.py
"""

import os
import re
import sys
import json
import struct
import socket
import requests
import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

# ── Настройки ──────────────────────────────────────────────────────────────────
OLLAMA_URL    = "http://192.168.88.50:11435"
OLLAMA_MODEL  = "qwen2.5-coder:14b"
BLENDER_HOST  = "localhost"
BLENDER_PORT  = 9876
OUTPUT_DIR    = "/home/rb/details"
SERVER_PORT   = 8765

# Параметры фрезерования
TOOL_DIAMETER  = 6.0
FEED_RATE      = 800
SPINDLE_SPEED  = 12000
DEPTH_OF_CUT   = 1.0
SAFE_HEIGHT    = 10.0

os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="3D Pipeline API", version="1.0")


class DetailRequest(BaseModel):
    description: str
    name: str | None = None


# ── Вспомогательные функции ────────────────────────────────────────────────────

def ask_ollama(description: str, stl_path: str) -> str:
    prompt = f"""Напиши Python-скрипт для Blender (bpy) для создания 3D детали.
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
5. Экспорт:
   bpy.ops.object.select_all(action="SELECT")
   bpy.ops.export_mesh.stl(filepath="{stl_path}", use_selection=True)

Только чистый Python без markdown.
Начни с: import bpy
         import math
"""
    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": 0.1, "num_predict": 1500}},
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["response"]


def clean_code(code: str) -> str:
    code = re.sub(r"```python\s*", "", code)
    code = re.sub(r"```\s*", "", code)
    code = re.sub(r"<think>.*?</think>", "", code, flags=re.DOTALL)
    return code.strip()


def run_in_blender(code: str) -> dict:
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
        f"; Фреза: d={TOOL_DIAMETER}мм, подача={FEED_RATE}мм/мин",
        "",
        "G21 G90 G17 G94",
        f"G0 Z{SAFE_HEIGHT:.1f}",
        f"M3 S{SPINDLE_SPEED}",
        "G4 P2",
        "",
        "; === Контурная обработка ===",
    ]

    z = 0.0
    pass_num = 0
    while z > -depth:
        z = max(-depth, z - DEPTH_OF_CUT)
        pass_num += 1
        lines += [
            f"; Проход {pass_num} Z={z:.2f}",
            f"G0 X{xmin:.3f} Y{ymin:.3f}",
            f"G1 Z{z:.3f} F{FEED_RATE//4}",
            f"G1 X{xmax:.3f} Y{ymin:.3f} F{FEED_RATE}",
            f"G1 X{xmax:.3f} Y{ymax:.3f}",
            f"G1 X{xmin:.3f} Y{ymax:.3f}",
            f"G1 X{xmin:.3f} Y{ymin:.3f}",
            f"G0 Z{SAFE_HEIGHT:.1f}",
        ]

    lines += ["M5", "G0 X0 Y0", "M30"]

    with open(gcode_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(lines)


# ── API endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Проверка работоспособности сервера."""
    # Проверяем Blender
    try:
        s = socket.socket()
        s.settimeout(3)
        s.connect((BLENDER_HOST, BLENDER_PORT))
        s.close()
        blender_ok = True
    except Exception:
        blender_ok = False

    # Проверяем Ollama
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        ollama_ok = r.status_code == 200
    except Exception:
        ollama_ok = False

    return {
        "status": "ok",
        "blender": "✅" if blender_ok else "❌",
        "ollama":  "✅" if ollama_ok  else "❌",
        "model":   OLLAMA_MODEL,
    }


@app.post("/make")
def make_detail(req: DetailRequest):
    """
    Создаёт 3D деталь по текстовому описанию.
    Возвращает пути к STL и G-Code файлам.
    """
    # Генерируем имя файла
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = req.name or f"detail_{ts}"
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)

    stl_path   = f"{OUTPUT_DIR}/{name}.stl"
    gcode_path = f"{OUTPUT_DIR}/{name}.gcode"

    # Шаг 1: Ollama → код
    try:
        raw_code = ask_ollama(req.description, stl_path)
        code     = clean_code(raw_code)
    except Exception as e:
        raise HTTPException(500, f"Ошибка Ollama: {e}")

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

    stl_size   = os.path.getsize(stl_path)
    gcode_size = os.path.getsize(gcode_path)

    return {
        "status":     "ok",
        "name":       name,
        "stl_path":   stl_path,
        "gcode_path": gcode_path,
        "stl_size":   stl_size,
        "gcode_lines": gcode_lines,
        "gcode_size": gcode_size,
        "blender_code_len": len(code),
    }


@app.get("/file/stl/{name}")
def get_stl(name: str):
    """Скачать STL файл."""
    path = f"{OUTPUT_DIR}/{name}.stl"
    if not os.path.exists(path):
        raise HTTPException(404, "STL не найден")
    return FileResponse(path, filename=f"{name}.stl",
                        media_type="application/octet-stream")


@app.get("/file/gcode/{name}")
def get_gcode(name: str):
    """Скачать G-Code файл."""
    path = f"{OUTPUT_DIR}/{name}.gcode"
    if not os.path.exists(path):
        raise HTTPException(404, "G-Code не найден")
    return FileResponse(path, filename=f"{name}.gcode",
                        media_type="text/plain")


@app.get("/list")
def list_details():
    """Список всех созданных деталей."""
    files = []
    for f in sorted(Path(OUTPUT_DIR).glob("*.stl")):
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
    print(f"🚀 3D Pipeline API Server")
    print(f"   Ollama:  {OLLAMA_URL} ({OLLAMA_MODEL})")
    print(f"   Blender: {BLENDER_HOST}:{BLENDER_PORT}")
    print(f"   Output:  {OUTPUT_DIR}")
    print(f"   API:     http://192.168.88.18:{SERVER_PORT}")
    print(f"   Docs:    http://192.168.88.18:{SERVER_PORT}/docs")
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
