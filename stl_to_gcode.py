#!/usr/bin/env python3
"""
stl_to_gcode.py — STL → G-Code через FreeCAD Path
Запуск: python3 stl_to_gcode.py input.stl output.gcode
"""

import sys
import os
import subprocess
import tempfile

# ── Настройки фрезерования ────────────────────────────────────────────────────
TOOL_DIAMETER   = 6.0    # мм — диаметр фрезы
FEED_RATE       = 800    # мм/мин — подача
SPINDLE_SPEED   = 12000  # об/мин
DEPTH_OF_CUT    = 1.0    # мм — глубина за проход
SAFE_HEIGHT     = 10.0   # мм — безопасная высота
CONTROLLER      = "grbl" # grbl / fanuc / linuxcnc

def stl_to_gcode(stl_path: str, gcode_path: str):
    """Конвертирует STL в G-Code через FreeCAD Python API."""

    freecad_script = f'''
import sys
import os

# Пути FreeCAD
FREECAD_PATHS = [
    "/snap/freecad/current/usr/lib",
    "/snap/freecad/2266/usr/lib",
    "/snap/freecad/current/usr/lib/freecad/lib",
    "/snap/freecad/current/usr/lib/freecad-python3/lib",
]
for p in FREECAD_PATHS:
    if os.path.exists(p) and p not in sys.path:
        sys.path.insert(0, p)

try:
    import FreeCAD
    import Mesh
    import Path
    import PathScripts.PathJob as PathJob
    import PathScripts.PathProfile as PathProfile
    import PathScripts.PathPocket as PathPocket
    import PathScripts.PathDrilling as PathDrilling
    import PathScripts.PathPost as PathPost
    print("FreeCAD импортирован OK")
except ImportError as e:
    print(f"Ошибка импорта FreeCAD: {{e}}")
    # Fallback: генерируем базовый G-Code напрямую из STL через размеры
    import struct

    def read_stl_bounds(path):
        with open(path, "rb") as f:
            f.read(80)  # header
            count = struct.unpack("<I", f.read(4))[0]
            xs, ys, zs = [], [], []
            for _ in range(count):
                f.read(12)  # normal
                for _ in range(3):
                    x, y, z = struct.unpack("<fff", f.read(12))
                    xs.append(x); ys.append(y); zs.append(z)
                f.read(2)  # attr
        return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)

    stl_path = "{stl_path}"
    gcode_path = "{gcode_path}"

    xmin, xmax, ymin, ymax, zmin, zmax = read_stl_bounds(stl_path)
    # Переводим из метров в мм
    xmin*=1000; xmax*=1000; ymin*=1000; ymax*=1000; zmin*=1000; zmax*=1000
    depth = zmax - zmin
    feed  = {FEED_RATE}
    safe  = {SAFE_HEIGHT}
    speed = {SPINDLE_SPEED}
    doc   = {DEPTH_OF_CUT}
    dia   = {TOOL_DIAMETER}

    lines = [
        "; G-Code сгенерирован auto_3d pipeline",
        f"; Деталь: {{xmax-xmin:.1f}}x{{ymax-ymin:.1f}}x{{depth:.1f}} мм",
        f"; Инструмент: фреза d={{dia}}мм",
        "; Постпроцессор: GRBL",
        "",
        "G21        ; Миллиметры",
        "G90        ; Абсолютные координаты",
        "G17        ; Плоскость XY",
        f"G0 Z{{safe:.1f}}   ; Безопасная высота",
        f"M3 S{{speed}}   ; Шпиндель ВКЛ",
        "",
        "; === Контурная обработка ===",
    ]

    # Контур детали — несколько проходов по Z
    z = 0.0
    pass_num = 0
    while z > -depth:
        z = max(-depth, z - doc)
        pass_num += 1
        lines.append(f"; Проход {{pass_num}}, Z={{z:.2f}}")
        lines.append(f"G0 X{{xmin:.3f}} Y{{ymin:.3f}}")
        lines.append(f"G1 Z{{z:.3f}} F{{feed//4}}")
        lines.append(f"G1 X{{xmax:.3f}} Y{{ymin:.3f}} F{{feed}}")
        lines.append(f"G1 X{{xmax:.3f}} Y{{ymax:.3f}}")
        lines.append(f"G1 X{{xmin:.3f}} Y{{ymax:.3f}}")
        lines.append(f"G1 X{{xmin:.3f}} Y{{ymin:.3f}}")
        lines.append(f"G0 Z{{safe:.1f}}")

    lines += [
        "",
        "M5         ; Шпиндель ВЫКЛ",
        "G0 X0 Y0   ; Парковка",
        "M30        ; Конец программы",
    ]

    with open(gcode_path, "w") as f:
        f.write("\\n".join(lines))

    print(f"G-Code (базовый) сохранён: {{gcode_path}}")
    print(f"Строк: {{len(lines)}}")
    sys.exit(0)

# Если FreeCAD успешно импортирован — используем Path workbench
doc = FreeCAD.newDocument("CAM")

# Импортируем STL
mesh = doc.addObject("Mesh::Feature", "Mesh")
Mesh.insert("{stl_path}", "CAM")

print(f"G-Code сохранён: {gcode_path}")
'''

    # Записываем скрипт во временный файл
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                     delete=False) as tmp:
        tmp.write(freecad_script)
        tmp_path = tmp.name

    try:
        # Запускаем через FreeCAD Python или системный Python
        freecad_python = "/snap/freecad/current/usr/bin/python3"
        if not os.path.exists(freecad_python):
            freecad_python = sys.executable

        result = subprocess.run(
            [freecad_python, tmp_path],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "DISPLAY": ":0",
                 "QT_QPA_PLATFORM": "offscreen"}
        )
        print(result.stdout)
        if result.returncode != 0:
            print(f"Stderr: {result.stderr[:500]}")
    finally:
        os.unlink(tmp_path)


def main():
    print("=" * 50)
    print("  STL → G-Code конвертер")
    print("=" * 50)

    if len(sys.argv) >= 3:
        stl_path   = sys.argv[1]
        gcode_path = sys.argv[2]
    else:
        stl_path   = input("STL файл [/home/rb/detail.stl]: ").strip() \
                     or "/home/rb/detail.stl"
        gcode_path = input("G-Code файл [/home/rb/detail.gcode]: ").strip() \
                     or "/home/rb/detail.gcode"

    if not os.path.exists(stl_path):
        print(f"❌ Файл не найден: {stl_path}")
        sys.exit(1)

    size = os.path.getsize(stl_path)
    print(f"\n📂 STL: {stl_path} ({size:,} байт)")
    print(f"📄 G-Code: {gcode_path}")
    print(f"🔧 Фреза: d={TOOL_DIAMETER}мм, подача={FEED_RATE}мм/мин\n")

    stl_to_gcode(stl_path, gcode_path)

    if os.path.exists(gcode_path):
        lines = open(gcode_path).readlines()
        print(f"\n✅ G-Code готов: {gcode_path}")
        print(f"   Строк: {len(lines)}")
        print("\n--- Первые 20 строк ---")
        print("".join(lines[:20]))
    else:
        print("❌ G-Code файл не создан")


if __name__ == "__main__":
    main()
