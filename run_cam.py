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
import freecad_cam

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
    ap.add_argument("--origin", choices=["corner-top", "center-top", "model"],
                    help="ноль программы: corner-top = угол детали + верх (дефолт), "
                         "center-top = центр + верх, model = как в CAD-файле")
    ap.add_argument("--rough", type=float, metavar="MM",
                    help="припуск черновой обработки, мм (0 = без черновой; "
                         "дефолт из конфига)")
    ap.add_argument("--rough-mode", choices=["stages", "layers"],
                    help="черновая: stages = по типам фич (дефолт), "
                         "layers = послойно, как Cavity Mill (эксперимент)")
    ap.add_argument("--finish", action="store_true",
                    help="включить чистовой проход (по умолчанию уже включён)")
    ap.add_argument("--no-finish", action="store_true",
                    help="без чистового прохода (только черновая)")
    ap.add_argument("--stock-margin", type=float, metavar="MM",
                    help="поля заготовки вокруг детали по X/Y, мм (дефолт из конфига)")
    ap.add_argument("--stock", metavar="FILE",
                    help="заготовка из файла (.step/.iges/.brep/.stl) в той же "
                         "системе координат, что и модель; поля игнорируются")
    ap.add_argument("--no-orient", action="store_true",
                    help="не поворачивать деталь (по умолчанию она кладётся "
                         "самой большой плоской гранью вниз)")
    ap.add_argument("--nx-export", action="store_true",
                    help="доп. сохранить деталь и заготовку в STEP в системе координат "
                         "G-кода (для симуляции в NX): рядом лягут <out>_part.step / _stock.step")
    ap.add_argument("--verify-export", action="store_true",
                    help="доп. сохранить эталон и маски достижимости граней в STEP (в СК "
                         "G-кода) для verify.py: <out>_part.step / _reachable.step / _unreachable.step")
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
    if args.rough is not None:
        config.ROUGH_ALLOWANCE = args.rough
    if args.rough_mode:
        config.ROUGH_MODE = args.rough_mode
    if args.finish:
        config.FINISH = True
    if args.no_finish:
        config.FINISH = False
    if args.stock_margin is not None:
        config.STOCK_MARGIN = args.stock_margin
    if args.stock:
        config.STOCK_FILE = args.stock
    if args.no_orient:
        config.AUTO_ORIENT = False
    if args.nx_export:
        config.NX_EXPORT = True
    if args.verify_export:
        config.VERIFY_EXPORT = True

    ext = os.path.splitext(args.model)[1].lower()
    if ext == ".prt":
        print("❌ .prt (Siemens NX) — закрытый формат, FreeCAD его не читает.")
        print("   Экспортируйте деталь из NX в STEP: File → Export → STEP (AP214/AP242),")
        print("   затем: python run_cam.py деталь.step")
        sys.exit(1)
    if not os.path.exists(args.model):
        print(f"❌ Файл не найден: {args.model}")
        sys.exit(1)
    if config.STOCK_FILE:
        if os.path.splitext(config.STOCK_FILE)[1].lower() == ".prt":
            print("❌ Заготовка .prt не читается — экспортируйте её из NX в STEP.")
            sys.exit(1)
        if not os.path.exists(config.STOCK_FILE):
            print(f"❌ Файл заготовки не найден: {config.STOCK_FILE}")
            sys.exit(1)

    fc = freecad_cam.find_freecadcmd()
    if not fc:
        print("❌ FreeCAD (freecadcmd) не найден — укажите FREECAD_CMD в конфиге")
        sys.exit(1)

    gcode = args.gcode or (os.path.splitext(args.model)[0] + ".gcode")
    out_dir = os.path.dirname(os.path.abspath(gcode))
    os.makedirs(out_dir, exist_ok=True)      # создаём папку вывода (напр. runs/stages/)

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
            else "контур → отверстия → остальное")
    rough = (f"припуск {config.ROUGH_ALLOWANCE}мм | слой {config.ROUGH_STEPDOWN}мм | "
             f"шаг {config.ROUGH_STEPOVER}% Ø ({mode})"
             if config.ROUGH_ALLOWANCE > 0 else "выключена")
    print(f"Черновая: {rough}")
    finish = (f"шаг {config.SURFACE_STEPOVER}% Ø | сэмплинг {config.SURFACE_SAMPLE_INTERVAL}мм | "
              f"рисунок {config.SURFACE_CUT_PATTERN}"
              if config.FINISH else "выключена (включить: --finish или FINISH: true)")
    print(f"Чистовая: {finish}")
    print(f"Ноль:     {config.ORIGIN}")
    print(f"Пост:     {config.POSTPROCESSOR}")
    print("Обработка...")

    try:
        n = freecad_cam.generate_gcode_freecad(args.model, gcode)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        sys.exit(1)

    print(f"✅ Готово: {n} строк → {gcode}  ({os.path.getsize(gcode):,} байт)")
    print("⚠  Перед станком проверьте программу в симуляторе (CAMotics / ncviewer.com)")


if __name__ == "__main__":
    main()
