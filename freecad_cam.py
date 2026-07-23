"""
freecad_cam.py — генерация G-Code из CAD-модели через FreeCAD Path (CAM).

Вход — файл модели детали:
  .step/.stp/.iges/.igs/.brep — точное твёрдое тело (рекомендуется; из Siemens NX:
      File → Export → STEP). Единицы берутся из файла.
  .stl/.obj — меш (аппроксимация); масштабируется через STL_SCALE_TO_MM.
  .prt — не читается FreeCAD (закрытый формат NX), нужен экспорт в STEP.

Стратегия — 3D-обработка по поверхности (Path Surface): фреза следует за фактической
геометрией модели (наклоны, конусы, купола, рельеф).

Этот модуль — «хостовая» часть: находит бинарник freecadcmd, передаёт параметры и
разбирает результат. Сама CAM-логика — в freecad_worker.py, который исполняется
интерпретатором FreeCAD в отдельном процессе (его Qt/OpenCASCADE не грузятся в наш Python).
"""

import os
import json
import time
import shutil
import tempfile
import subprocess

import config

# Кандидаты на бинарник freecadcmd (headless FreeCAD), в порядке приоритета.
# config.FREECAD_CMD (если задан) проверяется первым.
def _windows_candidates():
    """Типовые пути установки FreeCAD на Windows (инсталлятор/winget/portable)."""
    import glob
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
    found = []
    for root in roots:
        if root:
            found += glob.glob(os.path.join(root, "FreeCAD*", "bin", "freecadcmd.exe"))
    return sorted(found, reverse=True)  # свежая версия первой

_CANDIDATES = (
    (_windows_candidates() if os.name == "nt" else
     [os.path.expanduser("~/freecad-appimage/squashfs-root/usr/bin/freecadcmd")])
    + [
        "freecadcmd",     # системный / PATH
        "FreeCADCmd",     # имя в некоторых сборках
        "freecad.cmd",    # snap-версия (Linux)
    ]
)

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "freecad_worker.py")


def _ascii_safe(path: str) -> str:
    """FreeCAD/OCCT на Windows не открывают файлы по путям с не-ASCII символами
    (кириллица в C:\\Users\\<имя> → «Unknown exception while processing file»).
    Для существующего пути возвращает его короткое 8.3-имя (чистый ASCII)."""
    if not path or os.name != "nt" or path.isascii():
        return path
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024) and buf.value.isascii():
        return buf.value
    return path


def _worker_path() -> str:
    """Путь к worker-скрипту, который freecadcmd сможет открыть: не-ASCII путь
    конвертируется в 8.3, а если коротких имён нет — worker копируется во
    временную папку (она ASCII: %TEMP% отдаётся коротким путём)."""
    p = _ascii_safe(_WORKER)
    if p.isascii():
        return p
    dst = os.path.join(tempfile.gettempdir(), "freecad_worker.py")
    shutil.copyfile(_WORKER, dst)
    return dst


def find_freecadcmd() -> str | None:
    """Возвращает путь к рабочему freecadcmd или None, если FreeCAD не найден."""
    candidates = ([config.FREECAD_CMD] if getattr(config, "FREECAD_CMD", "") else []) + _CANDIDATES
    for c in candidates:
        if os.path.isabs(c) or "/" in c:
            if os.path.exists(c) and os.access(c, os.X_OK):
                return c
        elif shutil.which(c):
            return shutil.which(c)
    return None


def available() -> bool:
    return find_freecadcmd() is not None


def generate_gcode_freecad(model_path: str, gcode_path: str) -> int:
    """CAD-модель → G-Code (3D по поверхности). Возвращает число строк.
    Бросает RuntimeError, если FreeCAD недоступен, формат не поддерживается
    или обработка не удалась."""
    fc = find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден (укажите FREECAD_CMD в конфиге)")

    params = {
        # модель/заготовку читает OCCT — на Windows пути должны быть ASCII (8.3)
        "model_path": _ascii_safe(os.path.abspath(model_path)),
        "gcode_path": os.path.abspath(gcode_path),
        "scale_to_mm": config.STL_SCALE_TO_MM,      # только для мешей
        "origin": config.ORIGIN,                    # нормализация нуля программы
        "auto_orient": config.AUTO_ORIENT,          # положить деталь плашмя
        "tool_diameter": config.TOOL_DIAMETER,
        "feed_rate": config.FEED_RATE,
        "spindle_speed": config.SPINDLE_SPEED,
        "safe_height": config.SAFE_HEIGHT,
        "stock_margin": config.STOCK_MARGIN,
        "stock_margin_top": config.STOCK_MARGIN_TOP,
        "stock_file": (_ascii_safe(os.path.abspath(config.STOCK_FILE))
                       if config.STOCK_FILE else ""),  # заготовка из файла
        "stock_align": bool(getattr(config, "STOCK_ALIGN", False)),
        # заготовка в координатах программы (повёрнутая/сдвинутая вместе с
        # деталью) выгружается рядом с G-Code — для симуляции и наладки
        "stock_out": os.path.splitext(os.path.abspath(gcode_path))[0] + "_stock.stp",

        "rough_mode": config.ROUGH_MODE,
        "rough_allowance": config.ROUGH_ALLOWANCE,
        "rough_allowance_mode": config.ROUGH_ALLOWANCE_MODE,
        "rough_stepdown": config.ROUGH_STEPDOWN,
        "rough_stepover": config.ROUGH_STEPOVER,
        "rough_tolerance": config.ROUGH_TOLERANCE,
        "finish": config.FINISH,
        "cut_pattern": config.SURFACE_CUT_PATTERN,
        "stepover": config.SURFACE_STEPOVER,
        "sample_interval": config.SURFACE_SAMPLE_INTERVAL,
        "postprocessor": config.POSTPROCESSOR,
    }

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    t0 = time.perf_counter()
    try:
        proc = subprocess.Popen(
            [fc, _worker_path()],
            # worker переводит свой stdout в UTF-8 (Windows-консоль по умолчанию
            # cp1251); errors="replace" — чтобы битый байт не убил поток чтения
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
            env={**os.environ,
                 "FREECAD_WORKER_PARAMS": params_path,
                 "QT_QPA_PLATFORM": "offscreen"},   # headless: без дисплея
        )
        captured = _stream_output(proc)
        try:
            code = proc.wait(timeout=config.FREECAD_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError(f"FreeCAD превысил таймаут {config.FREECAD_TIMEOUT} с "
                               f"(поднимите FREECAD_TIMEOUT или ROUGH_TOLERANCE)")
        proc._pump_thread.join(timeout=5)   # дочитать хвост вывода
        print(flush=True)                   # закрыть строку прогресса
    finally:
        try:
            os.unlink(params_path)
        except OSError:
            pass

    # worker печатает строки "[worker] ..."; маркер успеха — "OK gcode_lines=N"
    lines_out = "".join(captured).splitlines()
    ok = next((l for l in lines_out if "[worker] OK gcode_lines=" in l), None)
    if ok and os.path.exists(gcode_path) and os.path.getsize(gcode_path) > 0:
        print(f"[cam] генерация G-Code: {time.perf_counter() - t0:.0f} с "
              f"реального времени", flush=True)
        return int(ok.split("gcode_lines=")[1].split()[0])

    worker_msgs = [l for l in lines_out if "[worker]" in l]
    tail = "\n".join((worker_msgs or lines_out)[-5:])
    raise RuntimeError(
        f"FreeCAD не сгенерировал G-Code (код {code}). {tail.strip()[:500]}"
    )


_NOISE = ("Schema", "gnome", "deprecated", "QStandardPaths", "kf.")


def _stream_output(proc):
    """Транслирует вывод FreeCAD в консоль по мере расчёта (проценты Adaptive —
    в одну обновляемую строку) и параллельно копит его для разбора маркеров."""
    import re
    import threading

    captured = []

    def pump():
        buf = ""
        for chunk in iter(lambda: proc.stdout.read(256), ""):
            captured.append(chunk)
            buf += chunk
            # вывод FreeCAD делится и \n, и \r (прогресс-проценты)
            while True:
                m = re.search(r"[\r\n]", buf)
                if not m:
                    break
                token, buf = buf[:m.start()].strip(), buf[m.end():]
                if not token or any(n in token for n in _NOISE):
                    continue
                if re.fullmatch(r"\(?\d+ ?%\)?", token):
                    print(f"\r  расчёт: {token}   ", end="", flush=True)
                elif "[worker]" in token:
                    print(f"\r{token}", flush=True)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    # поток живёт, пока субпроцесс пишет; join после wait() в вызывающем коде
    proc._pump_thread = t
    return captured
