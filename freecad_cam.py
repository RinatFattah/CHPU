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
import shutil
import tempfile
import subprocess

import config

# Кандидаты на бинарник freecadcmd (headless FreeCAD), в порядке приоритета.
# config.FREECAD_CMD (если задан) проверяется первым.
_CANDIDATES = [
    os.path.expanduser("~/freecad-appimage/squashfs-root/usr/bin/freecadcmd"),
    "freecadcmd",     # системный / PATH
    "FreeCADCmd",     # имя в некоторых сборках
    "freecad.cmd",    # snap-версия
    # Windows: стандартные установщики (per-user и системный)
    os.path.expanduser(r"~\AppData\Local\Programs\FreeCAD 1.1\bin\freecadcmd.exe"),
    os.path.expanduser(r"~\AppData\Local\Programs\FreeCAD 1.0\bin\freecadcmd.exe"),
    r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe",
    r"C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe",
]

# Windows: другие версии FreeCAD — ищем по маске (берём самую свежую).
_WIN_GLOBS = [
    os.path.expanduser(r"~\AppData\Local\Programs\FreeCAD*\bin\freecadcmd.exe"),
    r"C:\Program Files\FreeCAD*\bin\freecadcmd.exe",
]

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "freecad_worker.py")


def find_freecadcmd() -> str | None:
    """Возвращает путь к рабочему freecadcmd или None, если FreeCAD не найден."""
    candidates = ([config.FREECAD_CMD] if getattr(config, "FREECAD_CMD", "") else []) + _CANDIDATES
    for c in candidates:
        if os.path.isabs(c) or "/" in c or "\\" in c:
            if os.path.exists(c) and os.access(c, os.X_OK):
                return c
        elif shutil.which(c):
            return shutil.which(c)
    import glob
    for pat in _WIN_GLOBS:                       # запасной поиск по маске (Windows)
        hits = sorted(glob.glob(pat), reverse=True)
        if hits:
            return hits[0]
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
        "model_path": os.path.abspath(model_path),
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
        "stock_file": (os.path.abspath(config.STOCK_FILE)
                       if config.STOCK_FILE else ""),  # заготовка из файла

        "rough_mode": config.ROUGH_MODE,
        "rough_allowance": config.ROUGH_ALLOWANCE,
        "rough_stepdown": config.ROUGH_STEPDOWN,
        "rough_stepover": config.ROUGH_STEPOVER,
        "rough_tolerance": config.ROUGH_TOLERANCE,
        "finish": config.FINISH,
        "cut_pattern": config.SURFACE_CUT_PATTERN,
        "stepover": config.SURFACE_STEPOVER,
        "sample_interval": config.SURFACE_SAMPLE_INTERVAL,
        "postprocessor": config.POSTPROCESSOR,
        "nx_export": config.NX_EXPORT,   # экспорт STEP деталь/заготовка в СК G-кода (для NX)
        "verify_export": config.VERIFY_EXPORT,  # эталон+маски (STEP) в СК G-кода (для verify.py)
    }

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    # freecadcmd не исполняет скрипт по пути с не-ASCII символами (Windows: узкие
    # char* в OCCT/Qt → "Unknown exception while processing file"). Если путь к
    # worker'у ASCII (Linux, обычные пути) — запускаем его НАПРЯМУЮ, как раньше;
    # временную ASCII-копию в %TEMP% делаем только когда путь не-ASCII (…/Работа/…).
    worker_arg, worker_tmp = _WORKER, None
    if not _WORKER.isascii():
        fd, worker_tmp = tempfile.mkstemp(suffix="_worker.py")
        os.close(fd)
        shutil.copyfile(_WORKER, worker_tmp)
        worker_arg = worker_tmp

    try:
        proc = subprocess.Popen(
            [fc, worker_arg],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            # Windows: C++-слой FreeCAD (OCCT/Qt) пишет в консольной кодировке (не UTF-8);
            # errors="replace", чтобы поток чтения не падал на чужих байтах и не терял
            # маркер "OK gcode_lines=". На Linux вывод и так UTF-8 — поведение то же.
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ,
                 "FREECAD_WORKER_PARAMS": params_path,
                 "QT_QPA_PLATFORM": "offscreen",   # headless: без дисплея
                 "PYTHONUTF8": "1"},               # worker печатает Ø/кириллицу → форсируем UTF-8
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
        for _tmp in (params_path, worker_tmp):
            if not _tmp:                 # worker_tmp = None, если ASCII-копию не делали
                continue
            try:
                os.unlink(_tmp)
            except OSError:
                pass

    # worker печатает строки "[worker] ..."; маркер успеха — "OK gcode_lines=N"
    lines_out = "".join(captured).splitlines()
    ok = next((l for l in lines_out if "[worker] OK gcode_lines=" in l), None)
    if ok and os.path.exists(gcode_path) and os.path.getsize(gcode_path) > 0:
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
