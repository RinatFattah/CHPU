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
    ap.add_argument("--allowance", type=float,
                    help="припуск на чистовую по радиусу, мм (дефолт 0.2)")
    ap.add_argument("--stock-radial", type=float,
                    help="припуск заготовки-прутка по радиусу, мм "
                         "(только при --no-standard-stock)")
    ap.add_argument("--allowance-per-side", type=float,
                    help="припуск на обточку по радиусу для подбора проката, мм "
                         "(дефолт 3.15 — по заводскому эталону 14-31A)")
    ap.add_argument("--no-standard-stock", action="store_true",
                    help="не подбирать прокат по ГОСТ, взять деталь + припуск")
    ap.add_argument("--hex-stock", action="store_true",
                    help="брать ШЕСТИГРАННЫЙ прокат для деталей с гранями под "
                         "ключ (тогда грани достаются от проката). По умолчанию "
                         "круг — как на заводе, грани отдельной операцией")
    ap.add_argument("--no-hex-stock", action="store_true",
                    help=argparse.SUPPRESS)   # устаревший: круг и так по умолчанию
    ap.add_argument("--no-left-tool", action="store_true",
                    help="не использовать левый проходной резец T3 — участки за "
                         "уступом отдать канавочному (даёт гребёнку) ")
    ap.add_argument("--finish-only", action="store_true",
                    help="ОТЛАДКА: без черновых проходов и без припуска — по "
                         "одной траектории на инструмент, чтобы дефект в "
                         "результате однозначно относился к своему проходу")
    ap.add_argument("--no-nose-comp", action="store_true",
                    help="не компенсировать радиус при вершине (чистовой пойдёт "
                         "прямо по профилю — оставит зарез r·tg(угол уклона))")
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
    ap.add_argument("--no-groove-tool", action="store_true",
                    help="не выделять канавки отдельному резцу T2 — всё одним "
                         "проходным (прежнее поведение; проходной резец в канавку "
                         "физически не лезет и подрезает стенку позади)")
    ap.add_argument("--groove-width", type=float, metavar="MM",
                    help="ширина канавочной пластины, мм (дефолт 3.0; под более "
                         "узкую канавку подбирается автоматически)")
    ap.add_argument("--simulate", action="store_true",
                    help="после генерации прогнать G-Code на виртуальном "
                         "токарном станке NX ISV (нужен установленный NX); "
                         "результат — обработанная заготовка <gcode>_nxsim.stp "
                         "и _nxsim.prt (как --simulate у фрезеровки)")
    ap.add_argument("--simulate-own", action="store_true",
                    help="съём нашей собственной моделью (быстро, но это "
                         "самопроверка: генератор и симулятор писались вместе, "
                         "не независимая проверка) → <gcode>_sim.step")
    ap.add_argument("--nx-simulate", dest="simulate", action="store_true",
                    help=argparse.SUPPRESS)  # устаревший алиас для --simulate
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
        "allowance": 0.0 if args.finish_only else (
            args.allowance if args.allowance is not None
            else getattr(config, "LATHE_ALLOWANCE", 0.2)),
        "finish_only": args.finish_only,
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
        # проходной резец: чем он достаёт, считает lathe_reach
        "nose_angle": getattr(config, "LATHE_NOSE_ANGLE", 55.0),
        "approach_angle": getattr(config, "LATHE_APPROACH_ANGLE", 107.5),
        "insert_edge": getattr(config, "LATHE_INSERT_SIZE", 6.35),
        # канавочный резец T2
        "groove_tool": (not args.no_groove_tool
                        and getattr(config, "LATHE_GROOVE_TOOL", True)),
        "groove_width": (args.groove_width if args.groove_width is not None
                         else getattr(config, "LATHE_GROOVE_WIDTH", 3.0)),
        "groove_width_min": getattr(config, "LATHE_GROOVE_WIDTH_MIN", 1.0),
        "groove_tool_number": getattr(config, "LATHE_GROOVE_TOOL_NUMBER", 2),
        "left_tool": (not args.no_left_tool
                      and getattr(config, "LATHE_LEFT_TOOL", True)),
        "left_tool_number": getattr(config, "LATHE_LEFT_TOOL_NUMBER", 3),
        # компенсация радиуса при вершине (эквидистанта в чистовом проходе)
        "nose_comp": (not args.no_nose_comp
                      and getattr(config, "LATHE_NOSE_COMP", True)),
        "tip_offset": tuple(getattr(config, "LATHE_TIP_OFFSET", (1.0, -1.0))),
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
              + (" (шестигранный прокат)" if args.hex_stock else " (круг)"))
    print("Извлечение осевого профиля...")

    try:
        prof = lathe_profile.extract(
            model, out_json=stem + "_profile.json",
            part_out=stem + "_part.step", stock_out=stem + "_stock.stp",
            stock_radial=stock_radial,
            allowance_per_side=allowance_side,
            prefer_hex=(args.hex_stock
                        or (getattr(config, "LATHE_PREFER_HEX", False)
                            and not args.no_hex_stock)),
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
    if stats.get("left_passes"):
        print(f"   T3 левый проходной: {stats['left_passes']} проход(а), "
              f"{stats['left_volume_mm3']:.0f} мм³ — правому резцу за уступ "
              f"не зайти")
    for z_hi, z_lo, vol in stats.get("second_setup", []):
        print(f"   ↦ ВТОРОЙ УСТАНОВ: z {z_hi:.1f}..{z_lo:.1f}, {vol:.0f} мм³ — "
              f"упирается в торец детали, в этом установе не обработать")
    if stats.get("blade"):
        print(f"   T2 канавочный: пластина {stats['blade']:.2f} мм, "
              f"{stats['grooves']} канавк(и) объёмом "
              f"{stats['groove_volume_mm3']:.0f} мм³ — проходным резцом они "
              f"недостижимы")
        if stats.get("blade_tight"):
            print(f"   ⚠  есть канавка уже пластины {stats['blade']:.2f} мм — "
                  f"её дно останется недорезанным (нужна более тонкая пластина)")
    elif not p["groove_tool"]:
        print("   T2 отключён (--no-groove-tool): канавки режет проходной резец, "
              "он подрежет стенку позади")

    if args.simulate_own:
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

    if args.simulate:
        print("Симуляция на виртуальном токарном станке NX ISV "
              "(откроется окно NX, трогать не нужно)...")
        from nx import nx_lathe_sim
        try:
            res = nx_lathe_sim.simulate(gcode, stem + "_stock.stp",
                                        nose_radius=p["nose_radius"],
                                        nose_angle=p["nose_angle"],
                                        insert_size=p["insert_edge"],
                                        groove_width=stats.get("blade") or 0.0,
                                        groove_tool_number=p["groove_tool_number"],
                                        left_tool_number=(p["left_tool_number"]
                                                          if stats.get("left_passes")
                                                          else 0))
        except Exception as e:
            print(f"⚠  NX-симуляция не удалась: {e}")
            sys.exit(2)
        tail = (f"  (машинное время {res['machine_time']}"
                + (f", {res['triangles']} треуг." if res.get("triangles") else "")
                + ")") if res.get("machine_time") else ""
        print(f"✅ NX ISV: обработанная заготовка → {res['step']}{tail}")
        # результат возвращён в раму детали (ось Z) — ложится на out_part.step
        print(f"   ▶ наложить на деталь: {res['step']}  +  "
              f"{stem + '_part.step'}  (обе в раме детали, ось Z)")
        try:
            from cam import step_diff
            d = step_diff.diff(stem + "_part.step", res["step"],
                               stem + "_nxdiff.json")
            print(f"   сверка NX-результата с моделью: "
                  f"недорез {d['undercut_total_mm3']:.1f} мм³, "
                  f"зарез {d['overcut_total_mm3']:.1f} мм³")
        except Exception as e:
            print(f"   (сверка не выполнена: {e})")


if __name__ == "__main__":
    main()
