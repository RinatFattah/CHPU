#!/usr/bin/env python3
"""
run_cam.py — CAD-модель детали → G-Code (FreeCAD Path, 3D-обработка по поверхности).

Вход: .step/.stp/.iges/.igs/.brep (точное тело, рекомендуется) или .stl/.obj (меш).
.prt (Siemens NX) FreeCAD не читает — экспортируйте из NX: File → Export → STEP.

Примеры:
  python run_cam.py detal.step
  python run_cam.py detal.step out.gcode --config config.yaml
  python run_cam.py scan.stl --mm            # меш в миллиметрах

Параметры фрезы/режимов: config.yaml (см. README_CAM.md).
"""

import os
import sys
import argparse

import config
from cam import freecad_cam

# Windows: stdout по умолчанию cp1251 — печать Ø/кириллицы иначе падает с
# UnicodeEncodeError. Переключаем на UTF-8 ТОЛЬКО когда консоль не UTF-8; где stdout
# уже UTF-8 (Linux/macOS), ничего не трогаем — поведение старого кода сохраняется.
for _stream in (sys.stdout, sys.stderr):
    if (getattr(_stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

SOLID_EXTS = {".step", ".stp", ".iges", ".igs", ".brep", ".brp"}


def convert_prt(path: str, what: str) -> str:
    """.prt → STEP через установленный NX; без NX — понятная ошибка."""
    from nx import nx_export
    if not nx_export.available():
        print(f"❌ {path}: .prt (Siemens NX) — закрытый формат, FreeCAD его не читает,")
        print("   а Siemens NX на этой машине не найден.")
        print("   Либо укажите NX_BASE_DIR в конфиге, либо экспортируйте вручную:")
        print("   NX: File → Export → STEP (AP242), затем подайте .step файл.")
        sys.exit(1)
    print(f"NX:       {what} {os.path.basename(path)} → STEP AP{config.NX_STEP_AP} "
          f"(headless-экспорт)...")
    try:
        step = nx_export.prt_to_step(path)
    except Exception as e:
        print(f"❌ Ошибка экспорта из NX: {e}")
        sys.exit(1)
    print(f"NX:       готово → {step}")
    return step


def emit_heidenhain(gcode: str) -> str:
    """G-код → диалог Heidenhain рядом, <out>.h. Возвращает путь.

    Стойка заказчика — iTNC 530 на DMC 635V, диалект снят с их же программ
    (`nx/grbl_to_heidenhain.py`). Перевод идёт БЕЗ сдвига по Z, когда ноль уже
    внизу: `z_datum` у переводчика означает «где ноль ДОЛЖЕН оказаться», поэтому
    при ORIGIN = *-bottom мы просим «top», то есть «оставить как есть», а при
    ORIGIN = *-top — «bottom», и он сам сдвигает на высоту детали из шапки.
    Так .gcode и .h всегда выходят в ОДНОЙ системе координат.
    """
    import re
    from nx import grbl_to_heidenhain as gh

    origin = str(getattr(config, "ORIGIN", "corner-bottom"))
    z_datum = "bottom" if origin.endswith("-top") else "top"
    dst = os.path.splitext(gcode)[0] + ".h"
    # Имя программы у стойки — идентификатор, а не путь: буквы, цифры, подчёркивание.
    name = (getattr(config, "HEIDENHAIN_NAME", "") or
            re.sub(r"\W", "_", os.path.splitext(os.path.basename(dst))[0])[:16])

    with open(gcode, encoding="utf-8", errors="replace") as f:
        src = f.read().splitlines()
    body, warn = gh.convert(src, name, float(getattr(config, "HEIDENHAIN_TOL", 0.01)),
                            z_datum)
    with open(dst, "w", encoding="utf-8", newline="\r\n") as f:
        for i, ln in enumerate(body, 1):
            f.write(f"{i} {ln}\n")
    for w in dict.fromkeys(warn):
        print(f"   ⚠  {w}")
    return f"{dst}  ({len(body)} блоков, программа {name})"


def main():
    ap = argparse.ArgumentParser(
        description="CAD-модель (.step/.iges/.stl) → G-Code через FreeCAD CAM (3D по поверхности)",
        epilog="Параметры фрезы и режимов — в config.yaml, список в README_CAM.md",
    )
    ap.add_argument("model", help="модель: .step/.stp/.iges/.brep (тело) или .stl/.obj (меш)")
    ap.add_argument("gcode", nargs="?", help="куда писать G-Code (по умолчанию рядом с моделью)")
    ap.add_argument("--config", metavar="FILE", help="YAML-конфиг")
    ap.add_argument("--mm", action="store_true", help="меш в миллиметрах (scale=1.0)")
    ap.add_argument("--meters", action="store_true", help="меш в метрах (scale=1000)")
    ap.add_argument("--scale", type=float, help="произвольный множитель меша → мм")
    ap.add_argument("--origin", choices=["corner-bottom", "center-bottom",
                                         "corner-top", "center-top", "model"],
                    help="ноль программы: corner-bottom = угол детали + НИЗ (дефолт, "
                         "ноль заказчика), center-bottom = центр + низ, "
                         "corner-top / center-top = то же с нулём по верху, "
                         "model = как в CAD-файле")
    ap.add_argument("--rough", type=float, metavar="MM",
                    help="величина припуска, мм (0 = без черновой; дефолт из конфига)")
    ap.add_argument("--allowance-xy", action="store_true",
                    help="оставлять припуск по стенкам (XY); полы/поверхности — начисто")
    ap.add_argument("--allowance-all", action="store_true",
                    help="оставлять припуск везде (стенки + полы); без флагов — начисто")
    ap.add_argument("--finish", action="store_true",
                    help="чистовой проход после каждой черновой (пара "
                         "«черновая с припуском → чистовая», как в заводских "
                         "программах); требует припуска — см. --allowance-*")
    ap.add_argument("--no-finish", action="store_true",
                    help="без чистового прохода (перекрывает конфиг)")
    ap.add_argument("--rough-mode", choices=["stages", "layers"],
                    help="черновая: stages = по типам фич (дефолт), "
                         "layers = послойно, как Cavity Mill (эксперимент)")
    ap.add_argument("--stock-margin", type=float, metavar="MM",
                    help="поля заготовки вокруг детали по X/Y, мм (дефолт из конфига)")
    ap.add_argument("--plan", metavar="FILE",
                    help="строить программу ПО техплану: состав операций, "
                         "инструмент, слой и шаг строчек берутся из него")
    ap.add_argument("--stock", metavar="FILE",
                    help="заготовка из файла (.step/.iges/.brep/.stl) в той же "
                         "системе координат, что и модель; поля игнорируются")
    ap.add_argument("--stock-align", action="store_true",
                    help="выровнять заготовку из файла по детали (XY центр в "
                         "центр, дно в дно), игнорируя координаты файла")
    ap.add_argument("--no-orient", action="store_true",
                    help="не поворачивать деталь (по умолчанию она кладётся "
                         "самой большой плоской гранью вниз)")
    ap.add_argument("--simulate", action="store_true",
                    help="после генерации прогнать G-Code на виртуальном станке "
                         "NX ISV (нужен установленный NX); результат — "
                         "обработанная заготовка <gcode>_sim.stp и _sim.prt")
    ap.add_argument("--nx-export", action="store_true",
                    help="доп. сохранить деталь и заготовку в STEP в системе координат "
                         "G-кода (для ручной симуляции в NX): рядом лягут "
                         "<out>_part.step / _stock.step")
    ap.add_argument("--no-heidenhain", action="store_true",
                    help="не переводить программу в диалог Heidenhain (.h); по "
                         "умолчанию рядом с G-кодом кладётся <out>.h для iTNC 530")
    ap.add_argument("--h-name", metavar="ИМЯ",
                    help="имя программы у стойки в .h (дефолт — из имени файла)")
    args = ap.parse_args()

    if args.config:
        config.load(args.config)
        print(f"[config] {args.config}")
    if args.mm:
        config.STL_SCALE_TO_MM = 1.0
    if args.meters:
        config.STL_SCALE_TO_MM = 1000.0
    if args.scale is not None:
        config.STL_SCALE_TO_MM = args.scale
    if args.origin:
        config.ORIGIN = args.origin
    if args.no_heidenhain:
        config.EMIT_HEIDENHAIN = False
    if args.h_name:
        config.HEIDENHAIN_NAME = args.h_name
    if args.rough is not None:
        config.ROUGH_ALLOWANCE = args.rough
    if args.allowance_xy:
        config.ROUGH_ALLOWANCE_MODE = "xy"
    if args.allowance_all:
        config.ROUGH_ALLOWANCE_MODE = "all"
    if args.finish:
        config.FINISH = True
    if args.no_finish:
        config.FINISH = False
    if args.rough_mode:
        config.ROUGH_MODE = args.rough_mode
    if args.stock_margin is not None:
        config.STOCK_MARGIN = args.stock_margin
    if args.plan:
        config.PLAN_IN = args.plan
    if args.stock:
        config.STOCK_FILE = args.stock
    if args.stock_align:
        config.STOCK_ALIGN = True
    if args.no_orient:
        config.AUTO_ORIENT = False
    if args.simulate:
        config.SIMULATE = True
    if args.nx_export:
        config.NX_EXPORT = True

    if not os.path.exists(args.model):
        print(f"❌ Файл не найден: {args.model}")
        sys.exit(1)
    if config.STOCK_FILE and not os.path.exists(config.STOCK_FILE):
        print(f"❌ Файл заготовки не найден: {config.STOCK_FILE}")
        sys.exit(1)

    # .prt (Siemens NX) FreeCAD не читает; если NX установлен — конвертируем
    # автоматически его штатным транслятором (headless), иначе просим экспорт
    model_for_cam = args.model
    if os.path.splitext(args.model)[1].lower() == ".prt":
        model_for_cam = convert_prt(args.model, "деталь")
    if config.STOCK_FILE and os.path.splitext(config.STOCK_FILE)[1].lower() == ".prt":
        config.STOCK_FILE = convert_prt(config.STOCK_FILE, "заготовка")
    ext = os.path.splitext(model_for_cam)[1].lower()

    fc = freecad_cam.find_freecadcmd()
    if not fc:
        print("❌ FreeCAD (freecadcmd) не найден — укажите FREECAD_CMD в конфиге")
        sys.exit(1)

    gcode = args.gcode or (os.path.splitext(args.model)[0] + ".gcode")

    print(f"FreeCAD:  {fc}")
    print(f"Модель:   {args.model}  "
          f"({'точное тело BREP' if ext in SOLID_EXTS else f'меш, scale→мм = {config.STL_SCALE_TO_MM}'})")
    print(f"Фреза:    концевая плоская (endmill) Ø{config.TOOL_DIAMETER}мм | "
          f"подача {config.FEED_RATE}мм/мин | шпиндель {config.SPINDLE_SPEED}об/мин")
    stock = (f"из файла {config.STOCK_FILE}" if config.STOCK_FILE
             else f"деталь + {config.STOCK_MARGIN}мм по XY"
                  + (f" + {config.STOCK_MARGIN_TOP}мм сверху" if config.STOCK_MARGIN_TOP else ""))
    print(f"Заготовка: {stock}")
    mode = ("послойно, как Cavity Mill (эксперимент)" if config.ROUGH_MODE == "layers"
            else "вырезы → грани сверху вниз → внешний контур")
    alw = {"none": "начисто (без припуска)",
           "xy": f"припуск {config.ROUGH_ALLOWANCE}мм по XY (стенки)",
           "all": f"припуск {config.ROUGH_ALLOWANCE}мм везде"}.get(
              getattr(config, "ROUGH_ALLOWANCE_MODE", "none"), "начисто")
    rough = (f"{alw} | слой {config.ROUGH_STEPDOWN}мм | "
             f"шаг {config.ROUGH_STEPOVER}% Ø ({mode})"
             if config.ROUGH_ALLOWANCE > 0 else "выключена")
    print(f"Черновая: {rough}")
    print(f"Ноль:     {config.ORIGIN}")
    print(f"Пост:     {config.POSTPROCESSOR}")
    print("Обработка...")

    try:
        n = freecad_cam.generate_gcode_freecad(model_for_cam, gcode)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        sys.exit(1)

    print(f"✅ Готово: {n} строк → {gcode}  ({os.path.getsize(gcode):,} байт)")

    if getattr(config, "EMIT_HEIDENHAIN", True):
        try:
            print(f"   → диалог Heidenhain: {emit_heidenhain(gcode)}")
        except Exception as e:      # G-код при этом валиден, перевод — довесок
            print(f"⚠  Перевод в Heidenhain не удался: {e}")

    if config.SIMULATE:
        print("Симуляция на виртуальном станке NX (ISV) — на время прогона "
              "откроется окно NX, трогать его не нужно...")
        from nx import nx_sim
        stock_step = os.path.splitext(os.path.abspath(gcode))[0] + "_stock.stp"
        try:
            res = nx_sim.simulate(gcode, stock_step)
        except Exception as e:
            print(f"⚠  Симуляция не удалась: {e}")
            print("   G-Code при этом сгенерирован — проверьте его в симуляторе вручную.")
            sys.exit(2)
        extra = []
        if res.get("machine_time"):
            extra.append(f"машинное время: {res['machine_time']}")
        if res.get("triangles"):
            extra.append(f"{res['triangles']} треуг.")
        tail = f"  ({', '.join(extra)})" if extra else ""
        print(f"✅ Симуляция: обработанная заготовка → {res['step']}{tail}")
        if res.get("prt"):
            print(f"   она же в формате NX (.prt) → {res['prt']}")
    else:
        print("⚠  Перед станком проверьте программу в симуляторе "
              "(--simulate — виртуальный станок NX, либо CAMotics / ncviewer.com)")


if __name__ == "__main__":
    main()
