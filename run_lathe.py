#!/usr/bin/env python3
"""
run_lathe.py — ТОКАРНАЯ ветка пайплайна: CAD-модель тела вращения → G-Code.

Отдельная точка входа рядом с run_cam.py (фрезеровка): у токарной обработки
другая геометрическая постановка (профиль R(z) вместо 3D-модели), другой
инструмент (резец) и другой станок, поэтому смешивать их в одном CLI нечего.

  деталь.stp ──► профиль R(z) ──► токарная программа ──► [симуляция NX]
                 cam/lathe_worker  lathe/lathe_gcode      nx/nx_lathe_sim

Пример:
  python run_lathe.py деталь.stp runs/8_lathe/out.gcode --config config.yaml
"""

import argparse
import json
import os
import sys

import config
from cam import lathe_profile
from lathe import lathe_gcode

for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    ap = argparse.ArgumentParser(
        description="Тело вращения → токарная управляющая программа (G-Code)")
    ap.add_argument("model", help="деталь: .step/.stp/.iges/.brep или .prt (нужен NX)")
    ap.add_argument("gcode", nargs="?", help="куда писать G-Code")
    ap.add_argument("--config", metavar="FILE", help="YAML-конфиг")
    ap.add_argument("--depth-of-cut", type=float, help="глубина резания за проход, мм")
    ap.add_argument("--allowance", type=float, help="припуск на чистовую, мм")
    ap.add_argument("--stock-radial", type=float,
                    help="припуск заготовки-прутка по радиусу, мм "
                         "(только при --no-standard-stock)")
    ap.add_argument("--allowance-per-side", type=float,
                    help="припуск на обточку по радиусу для подбора проката, мм "
                         "(дефолт 3.15 — по заводскому эталону 14-31A)")
    ap.add_argument("--no-standard-stock", action="store_true",
                    help="не подбирать прокат по ГОСТ, взять деталь + припуск")
    ap.add_argument("--no-hex-stock", action="store_true",
                    help="не предлагать шестигранный прокат для деталей "
                         "с гранями под ключ — только круг")
    ap.add_argument("--feed-per-min", action="store_true",
                    help="подача в мм/мин (G94); по умолчанию — на оборот "
                         "(G95), как принято в точении")
    ap.add_argument("--feed-rev", type=float, metavar="MM",
                    help="черновая подача, мм/об (дефолт 0.15 — как у завода)")
    ap.add_argument("--radius-mode", action="store_true",
                    help="X в радиусах (по умолчанию — в диаметрах, как ждёт стойка)")
    ap.add_argument("--no-arcs", action="store_true",
                    help="чистовой проход только отрезками (без G2/G3)")
    ap.add_argument("--no-partoff", action="store_true", help="без отрезки")
    ap.add_argument("--simulate", action="store_true",
                    help="прогнать на токарном станке NX ISV")
    args = ap.parse_args()

    if args.config:
        config.load(args.config)
        print(f"[config] {args.config}")

    if not os.path.exists(args.model):
        print(f"❌ Файл не найден: {args.model}")
        sys.exit(1)

    model = args.model
    if os.path.splitext(model)[1].lower() == ".prt":
        from nx import nx_export
        if not nx_export.available():
            print("❌ .prt требует установленного Siemens NX")
            sys.exit(1)
        print(f"NX:       {os.path.basename(model)} → STEP (headless)...")
        model = nx_export.prt_to_step(model)

    gcode = args.gcode or (os.path.splitext(args.model)[0] + ".gcode")
    os.makedirs(os.path.dirname(os.path.abspath(gcode)) or ".", exist_ok=True)
    stem = os.path.splitext(os.path.abspath(gcode))[0]

    p = {
        "depth_of_cut": args.depth_of_cut if args.depth_of_cut is not None
            else getattr(config, "LATHE_DEPTH_OF_CUT", 1.0),
        "allowance": args.allowance if args.allowance is not None
            else getattr(config, "LATHE_ALLOWANCE", 0.3),
        "clearance": getattr(config, "LATHE_CLEARANCE", 2.0),
        "feed": getattr(config, "LATHE_FEED", 150.0),
        "feed_finish": getattr(config, "LATHE_FEED_FINISH", 80.0),
        "feed_mode": "per_min" if args.feed_per_min else "per_rev",
        "feed_per_rev": (args.feed_rev if args.feed_rev is not None
                         else getattr(config, "LATHE_FEED_PER_REV", 0.15)),
        "feed_per_rev_finish": getattr(config, "LATHE_FEED_PER_REV_FINISH", 0.08),
        "spindle_speed": getattr(config, "LATHE_SPINDLE_SPEED", 1500),
        "scan_step": getattr(config, "LATHE_SCAN_STEP", 0.2),
        "diameter_mode": not args.radius_mode,
        "partoff": not args.no_partoff,
        "partoff_width": getattr(config, "LATHE_PARTOFF_WIDTH", 3.0),
        "insert": getattr(config, "LATHE_INSERT", "DCMT070204R"),
        "nose_radius": getattr(config, "LATHE_NOSE_RADIUS", 0.4),
        "arcs": not args.no_arcs,
        "arc_tol": getattr(config, "LATHE_ARC_TOL", 0.005),
    }
    stock_radial = (args.stock_radial if args.stock_radial is not None
                    else getattr(config, "LATHE_STOCK_RADIAL", 1.0))

    print(f"Модель:    {args.model}")
    print(f"Резец:     {p['insert']} | радиус при вершине {p['nose_radius']} мм")
    feed_txt = (f"подача {p['feed_per_rev']}/{p['feed_per_rev_finish']} мм/об (G95)"
                if p["feed_mode"] == "per_rev"
                else f"подача {p['feed']}/{p['feed_finish']} мм/мин (G94)")
    print(f"Режимы:    глубина {p['depth_of_cut']} мм | припуск {p['allowance']} мм | "
          f"{feed_txt} | {p['spindle_speed']} об/мин")
    allowance_side = (args.allowance_per_side if args.allowance_per_side is not None
                      else getattr(config, "LATHE_ALLOWANCE_PER_SIDE", 3.15))
    if args.no_standard_stock:
        print(f"Заготовка: пруток, +{stock_radial} мм по радиусу (без сортамента)")
    else:
        print(f"Заготовка: прокат по ГОСТ, припуск {allowance_side} мм на сторону"
              + (" (шестигранник не предлагать)" if args.no_hex_stock else ""))
    print("Извлечение осевого профиля...")

    try:
        prof = lathe_profile.extract(
            model, out_json=stem + "_profile.json",
            part_out=stem + "_part.step", stock_out=stem + "_stock.stp",
            stock_radial=stock_radial,
            allowance_per_side=allowance_side,
            prefer_hex=not args.no_hex_stock,
            use_standard=not args.no_standard_stock,
            profile_step=getattr(config, "LATHE_PROFILE_STEP", 0.1),
            simplify_tol=getattr(config, "LATHE_SIMPLIFY_TOL", 0.01))
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    print(f"✅ Профиль: {len(prof['profile'])} точек, "
          f"Ø{2 * prof['max_radius']:.2f} x {prof['length']:.2f} мм")

    stats = lathe_gcode.write(prof, p, gcode)
    print(f"✅ Программа: {stats['lines']} строк → {gcode} "
          f"({os.path.getsize(gcode):,} байт)")
    print(f"   операции: {', '.join(stats['ops'][:8])}"
          + (f" … всего {len(stats['ops'])}" if len(stats['ops']) > 8 else "")
          + (f" | дуг в чистовом: {stats['arcs']}" if stats.get("arcs") else ""))

    if args.simulate:
        print("Съём материала по программе...")
        from lathe import lathe_sim
        try:
            res = lathe_sim.simulate(gcode, prof, stem + "_sim.step",
                                     diameter_mode=p["diameter_mode"])
        except Exception as e:
            print(f"⚠  Симуляция не удалась: {e}")
            sys.exit(2)
        print(f"✅ Обработанная деталь → {res['step']}  "
              f"(объём {res['volume']:.1f} мм³)")
        # сверка с эталоном: объём детали из того же профиля
        try:
            from cam import step_diff
            d = step_diff.diff(stem + "_part.step", res["step"],
                               stem + "_diff.json")
            print(f"   сверка с моделью: недорез {d['undercut_total_mm3']:.1f} мм³, "
                  f"зарез {d['overcut_total_mm3']:.1f} мм³ "
                  f"(деталь {d['part_volume_mm3']:.1f} мм³)")
        except Exception as e:
            print(f"   (сверка не выполнена: {e})")


if __name__ == "__main__":
    main()
