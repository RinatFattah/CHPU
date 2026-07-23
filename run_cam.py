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
                    help="припуск черновой, мм (0 = черновая ДО НОМИНАЛА, без припуска; "
                         "дефолт из конфига). Выключить черновую совсем — --no-rough")
    ap.add_argument("--rough-mode", choices=["stages", "layers"],
                    help="черновая: stages = по типам фич (дефолт), "
                         "layers = послойно, как Cavity Mill (эксперимент)")
    ap.add_argument("--no-rough", action="store_true",
                    help="без черновой обработки (только чистовая)")
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
    ap.add_argument("--keepout-box", nargs=6, type=float, action="append",
                    metavar=("X0", "Y0", "Z0", "X1", "Y1", "Z1"),
                    help="запретный бокс (НЕ ВХОДИТЬ) в СК программы; можно повторять. "
                         "Инструмент не зайдёт и ПОД бокс (сверху не подъехать)")
    ap.add_argument("--work-box", nargs=6, type=float, action="append",
                    metavar=("X0", "Y0", "Z0", "X1", "Y1", "Z1"),
                    help="рабочий бокс (НЕ ВЫХОДИТЬ) в СК программы; несколько — "
                         "работа в их пересечении")
    ap.add_argument("--keepout-halfspace", nargs=3, action="append",
                    metavar=("AXIS", "CMP", "VALUE"),
                    help="запретное полупространство: ось X/Y/Z, lt/gt, отсечка; "
                         "пример: --keepout-halfspace Z lt 5 (запрещено z<5)")
    ap.add_argument("--keepout-margin", type=float, metavar="MM",
                    help="страховочный зазор вокруг зон сверх радиуса фрезы "
                         "(дефолт из конфига: 0.5 мм)")
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
    if args.no_rough:
        config.ROUGH_ENABLED = False
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
    if args.keepout_margin is not None:
        config.KEEPOUT_MARGIN = args.keepout_margin
    # зоны из CLI ДОБАВЛЯЮТСЯ к зонам из YAML: ограничения накапливаются
    if args.keepout_box:
        config.KEEPOUT_BOXES = list(config.KEEPOUT_BOXES) + [list(b) for b in args.keepout_box]
    if args.work_box:
        config.WORK_BOXES = list(config.WORK_BOXES) + [list(b) for b in args.work_box]
    if args.keepout_halfspace:
        config.KEEPOUT_HALFSPACES = (list(config.KEEPOUT_HALFSPACES)
                                     + [list(h) for h in args.keepout_halfspace])

    try:
        zones_present = config.normalize_zones()
    except ValueError as e:
        print(f"❌ Мёртвые зоны: {e}")
        sys.exit(1)
    if zones_present and config.FINISH:
        print("❌ Мёртвые зоны пока поддерживаются только для черновой обработки:")
        print("   чистовой проход (Path Surface) не умеет их объезжать — добавьте --no-finish.")
        sys.exit(1)

    if not config.ROUGH_ENABLED and not config.FINISH:
        print("❌ Нечего делать: --no-rough и --no-finish вместе отключают все операции.")
        print("   Оставьте хотя бы одну: черновую (убрать --no-rough) или чистовую (убрать --no-finish).")
        sys.exit(1)

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
    if config.ROUGH_ENABLED:
        prip = (f"припуск {config.ROUGH_ALLOWANCE}мм" if config.ROUGH_ALLOWANCE > 0
                else "до номинала (припуск 0)")
        rough = f"{prip} | слой {config.ROUGH_STEPDOWN}мм | шаг {config.ROUGH_STEPOVER}% Ø ({mode})"
    else:
        rough = "выключена (--no-rough)"
    print(f"Черновая: {rough}")
    finish = (f"шаг {config.SURFACE_STEPOVER}% Ø | сэмплинг {config.SURFACE_SAMPLE_INTERVAL}мм | "
              f"рисунок {config.SURFACE_CUT_PATTERN}"
              if config.FINISH else "выключена (включить: --finish или FINISH: true)")
    print(f"Чистовая: {finish}")
    print(f"Ноль:     {config.ORIGIN}")
    if zones_present:
        print(f"Зоны:     отступ = радиус фрезы {config.TOOL_DIAMETER / 2.0:g} мм "
              f"+ зазор {config.KEEPOUT_MARGIN:g} мм; СК зон = СК ПРОГРАММЫ "
              f"(шапка G-кода)")
        for b in config.KEEPOUT_BOXES:
            print(f"  запрет: бокс X {b[0]:g}..{b[3]:g}  Y {b[1]:g}..{b[4]:g}  "
                  f"Z {b[2]:g}..{b[5]:g} (не входить; под бокс — тоже нельзя)")
        for b in config.WORK_BOXES:
            print(f"  работа: бокс X {b[0]:g}..{b[3]:g}  Y {b[1]:g}..{b[4]:g}  "
                  f"Z {b[2]:g}..{b[5]:g} (не выходить)")
        for h in config.KEEPOUT_HALFSPACES:
            print(f"  запрет: {h[0]} {'<' if h[1] == 'lt' else '>'} {h[2]:g}")
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
