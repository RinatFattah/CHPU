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
    ap.add_argument("model", nargs="?",
                    help="деталь: .step/.stp/.iges/.brep или .prt (нужен NX)")
    ap.add_argument("gcode", nargs="?", help="куда писать G-Code")
    ap.add_argument("--config", metavar="FILE", help="YAML-конфиг")
    ap.add_argument("--tools", metavar="IDS",
                    help="АКТИВНЫЙ НАБОР инструмента: id через запятую "
                         "(см. --list-tools). Программа строится только этими "
                         "инструментами; роли, которых в наборе нет, не "
                         "выполняются и попадают в «не обработано»")
    ap.add_argument("--list-tools", action="store_true",
                    help="показать каталог инструмента и выйти")
    ap.add_argument("--report", metavar="FILE",
                    help="выгрузить итог прогона машиночитаемым JSON: активный "
                         "набор инструмента, статистика обеих программ, сверка. "
                         "Это ВХОД АГЕНТНОЙ ПЕТЛИ (auto_fix_lathe.py)")
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
    ap.add_argument("--no-drill", action="store_true",
                    help="не сверлить и не растачивать осевое отверстие")
    ap.add_argument("--no-center-drill", action="store_true",
                    help="без центровки перед сверлением")
    ap.add_argument("--two-setups", action="store_true",
                    help="разбить работу на ДВА УСТАНОВА (перехват), как на "
                         "заводе: первый точит конец детали, сверлит насквозь и "
                         "отрезает, второй берётся за обточенное и делает "
                         "второй конец. Вторая программа — <gcode>_2.gcode")
    ap.add_argument("--grip-length", type=float, metavar="MM",
                    help="сколько длины закрывают кулачки патрона, мм "
                         "(дефолт 15) — короче этого обточенный поясок не даёт "
                         "перехватить деталь")
    ap.add_argument("--split-z", type=float, metavar="Z",
                    help="вручную задать z передачи работы второму установу")
    ap.add_argument("--no-finish-tool", action="store_true",
                    help="чистовую вести тем же резцом, что и черновую (без "
                         "отдельного 35°-ромба T8): в уступы точение зайдёт "
                         "мельче, больше уйдёт канавочному")
    ap.add_argument("--no-contour-rough", action="store_true",
                    help="ОТЛАДКА: снимать всё одним чистовым проходом, без "
                         "черновых слоёв. Геометрия та же, но глубина резания "
                         "доходит до 10.7 мм по радиусу — на станке такая "
                         "программа неисполнима")
    ap.add_argument("--contour-rough", action="store_true",
                    help=argparse.SUPPRESS)   # устаревший: слои и так включены
    ap.add_argument("--no-pre-finish", action="store_true",
                    help="не выбирать уступы чистовым резцом заранее (тогда в "
                         "них чистовой снимет припуск + то, что черновой не "
                         "достал)")
    ap.add_argument("--no-semi-finish", action="store_true",
                    help="не печатать получистовой проход (тогда после уровней "
                         "чистовой встретит переменный припуск: 0.2 мм на "
                         "цилиндрах и до 0.2 + шаг на уклонах)")
    ap.add_argument("--rough-mode", choices=("contour", "levels"),
                    help="форма черновых слоёв: contour — эквидистанта чистового "
                         "пути (по умолчанию), levels — продольные проходы "
                         "ПОСТОЯННОГО ДИАМЕТРА, как у завода (каждый обрывается "
                         "на контуре)")
    ap.add_argument("--groove-contour", action="store_true",
                    help="ЭКСПЕРИМЕНТ: чистовой контур канавки углом пластины "
                         "после врезаний (чистит донья, но пока даёт зарез на "
                         "границе канавки)")
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
                         "и _nxsim.prt (как --simulate у фрезеровки). При "
                         "--two-setups гоняются ОБА установа и собирается "
                         "итоговая деталь <gcode>_full.step")
    ap.add_argument("--sim-setup", choices=("1", "2", "both"),
                    help="какие установы гнать в ISV при --two-setups: both "
                         "(по умолчанию — оба и сборка итога), 1 или 2 — "
                         "только один, без сборки")
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

    if args.list_tools:
        from lathe import lathe_tools
        cat = lathe_tools.catalog(getattr(config, "LATHE_TOOLS", None))
        print(f"{'id':<16} {'роль':<8} {'T':>2}  что это")
        for t in cat.values():
            print(f"{t['id']:<16} {t['role']:<8} {t['number']:>2}  {t['desc']}")
        print("\nпо умолчанию активны: "
              + ", ".join(lathe_tools.default_active(cat)))
        return
    if not args.model:
        ap.error("нужен файл детали (или --list-tools)")

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
        "contour_rough": (not args.no_contour_rough
                          and not args.finish_only
                          and getattr(config, "LATHE_CONTOUR_ROUGH", True)),
        "rough_mode": (args.rough_mode if args.rough_mode
                       else getattr(config, "LATHE_ROUGH_MODE", "contour")),
        "semi_finish": (not args.no_semi_finish
                        and getattr(config, "LATHE_SEMI_FINISH", True)),
        "pre_finish": (not args.no_pre_finish
                       and getattr(config, "LATHE_PRE_FINISH", True)),
        "groove_contour": args.groove_contour,
        # осевое отверстие: сверление + растачивание (сам контур
        # подставляется ниже, после съёма профиля)
        "drill": not args.no_drill and getattr(config, "LATHE_DRILL", True),
        "drill_tool_number": getattr(config, "LATHE_DRILL_TOOL_NUMBER", 4),
        "bore_tool_number": getattr(config, "LATHE_BORE_TOOL_NUMBER", 5),
        "bore_allowance": getattr(config, "LATHE_BORE_ALLOWANCE", 0.75),
        "drill_peck": getattr(config, "LATHE_DRILL_PECK", 5.0),
        "center_drill": (not args.no_center_drill
                         and getattr(config, "LATHE_CENTER_DRILL", True)),
        "center_drill_d": getattr(config, "LATHE_CENTER_DRILL_D", 3.15),
        "center_drill_depth": getattr(config, "LATHE_CENTER_DRILL_DEPTH", 1.5),
        "center_tool_number": getattr(config, "LATHE_CENTER_TOOL_NUMBER", 7),
        "center_speed": getattr(config, "LATHE_CENTER_SPEED", 600),
        "feed_per_rev_center": getattr(config, "LATHE_FEED_PER_REV_CENTER", 0.05),
        "drill_speed": getattr(config, "LATHE_DRILL_SPEED", 800),
        "bore_speed": getattr(config, "LATHE_BORE_SPEED", 1000),
        "feed_per_rev_drill": getattr(config, "LATHE_FEED_PER_REV_DRILL", 0.06),
        "feed_per_rev_bore": getattr(config, "LATHE_FEED_PER_REV_BORE", 0.05),
        # резьба: только по явно заданному обозначению (из модели не вывести)
        "threads": getattr(config, "LATHE_THREADS", None) or [],
        "thread_tool_number": getattr(config, "LATHE_THREAD_TOOL_NUMBER", 6),
        "thread_speed": getattr(config, "LATHE_THREAD_SPEED", 600),
        "clearance": getattr(config, "LATHE_CLEARANCE", 2.0),
        "feed": getattr(config, "LATHE_FEED", 150.0),
        "feed_finish": getattr(config, "LATHE_FEED_FINISH", 80.0),
        "feed_mode": "per_min" if args.feed_per_min else "per_rev",
        "feed_per_rev": (args.feed_rev if args.feed_rev is not None
                         else getattr(config, "LATHE_FEED_PER_REV", 0.15)),
        "feed_per_rev_finish": getattr(config, "LATHE_FEED_PER_REV_FINISH", 0.08),
        "feed_per_rev_partoff": getattr(config, "LATHE_FEED_PER_REV_PARTOFF", 0.07),
        "groove_speed": getattr(config, "LATHE_GROOVE_SPEED", 600),
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
        # отдельный чистовой резец (35°-ромб) — достижимость считается по нему
        "finish_tool": (not args.no_finish_tool
                        and getattr(config, "LATHE_FINISH_TOOL", True)),
        "finish_tool_number": getattr(config, "LATHE_FINISH_TOOL_NUMBER", 8),
        "finish_insert": getattr(config, "LATHE_FINISH_INSERT", "VCMT110304"),
        "finish_nose_angle": getattr(config, "LATHE_FINISH_NOSE_ANGLE", 35.0),
        "finish_insert_edge": getattr(config, "LATHE_FINISH_INSERT_SIZE", 6.35),
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

    # ── АКТИВНЫЙ НАБОР ИНСТРУМЕНТА ──
    # Геометрия резцов и наличие ролей берутся из каталога (lathe/lathe_tools.py),
    # а не из плоских ключей конфига: программа строится ТОЛЬКО тем, что выдано.
    # Роли, которых в наборе нет, не выполняются — их работа честно попадает
    # в «не обработано». Умолчание каталога совпадает с дефолтами config.py,
    # поэтому без --tools поведение прежнее.
    from lathe import lathe_tools
    active = ([s.strip() for s in args.tools.split(",") if s.strip()]
              if args.tools else getattr(config, "LATHE_ACTIVE_TOOLS", None))
    try:
        tool_params, sim_tools, tools_chosen = lathe_tools.resolve(
            active, extra=getattr(config, "LATHE_TOOLS", None),
            log=lambda m: print(f"Инструмент: {m}"))
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)
    p.update(tool_params)
    # Явные --no-* перекрывают каталог, но ТОЛЬКО в сторону выключения:
    # флаг не может добавить инструмент, которого в наборе нет.
    for flag, key in ((args.no_finish_tool, "finish_tool"),
                      (args.no_groove_tool, "groove_tool"),
                      (args.no_left_tool, "left_tool"),
                      (args.no_drill, "drill"),
                      (args.no_center_drill, "center_drill"),
                      (args.no_partoff, "partoff")):
        if flag:
            p[key] = False
    if args.groove_width is not None:          # ручная ширина пластины сильнее каталога
        p["groove_width"] = args.groove_width

    # Копилка машиночитаемого отчёта (--report). Пишется по ходу прогона и
    # выгружается в конце: агентной петле нужен ровно этот срез — что выдали
    # генератору, что он сделал и что показала сверка.
    rep = {"model": os.path.abspath(args.model), "gcode": os.path.abspath(gcode),
           "tools": {"active": sorted(active if active is not None
                                      else lathe_tools.default_active()),
                     "chosen": {r: t["id"] for r, t in tools_chosen.items()},
                     "roles_off": [r for r in lathe_tools.ROLES
                                   if r not in tools_chosen]},
           "setups": [], "sim": {}, "diff": {}}

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

    p["bore"] = prof.get("bore_raw") or []

    # ── ДВА УСТАНОВА (перехват) ──
    # Обточить то, за что держишь, нельзя, поэтому работа делится на два зажима:
    # первый точит конец детали, сверлит насквозь и отрезает; второй берётся за
    # уже обточенное и делает второй конец. Так же устроен заводской эталон.
    prof2 = p2 = gcode2 = None
    z_split = z_end = None
    # порог радиуса между стенкой отверстия и наружной поверхностью — им
    # разделяются контуры при сборке итога из двух результатов ISV
    r_bore = max((r for _, r in (prof.get("bore_raw") or [])), default=0.0)
    r_split = (r_bore + min(r for _, r in prof["profile"])) / 2.0 if r_bore else 0.0
    if args.two_setups or getattr(config, "LATHE_TWO_SETUPS", False):
        from lathe import lathe_setups
        z_end = min(z for z, _ in prof["profile"])
        grip = (args.grip_length if args.grip_length is not None
                else getattr(config, "LATHE_GRIP_LENGTH", 15.0))
        face_allow = getattr(config, "LATHE_SECOND_FACE_ALLOWANCE", 2.0)
        z_split = lathe_setups.choose_split(
            prof["profile"], z_end, grip,
            override=(args.split_z if args.split_z is not None
                      else getattr(config, "LATHE_SPLIT_Z", None)))
        overlap = getattr(config, "LATHE_SETUP_OVERLAP", 2.0)
        prof1, prof2 = lathe_setups.split(prof, z_split, face_allow, overlap)
        th1, th2, th_over = lathe_setups.split_threads(p.get("threads"),
                                                       z_split, z_end)
        # Отверстие — с двух сторон, по заводской схеме. Каждый установ идёт
        # чуть за середину детали: сверло глубже, расточной мельче (на 14-31A
        # завод даёт −28.95 и −25.50 при длине 46). Так вылет сверла падает с
        # L/D 4.9 до 2.9, а борштанги — с 4 до 2.2.
        mid = z_end / 2.0
        z_hd = mid - getattr(config, "LATHE_HOLE_OVERLAP_DRILL", 6.0)
        z_hb = mid - getattr(config, "LATHE_HOLE_OVERLAP_BORE", 2.5)
        p2 = dict(p)
        p2.update(threads=th2, partoff=False,
                  bore=(prof2.get("bore_raw") or []),
                  hole_depth_drill=z_hd, hole_depth_bore=z_hb,
                  # разметка зоны — в том же формате, что у первого установа:
                  # по ней lathe_diff режет проверку (lathe_diff.limits_from_gcode)
                  setup_note=(f"SETUP 2 of 2: part re-gripped and FLIPPED, "
                              f"Z0 = far face (was Z{z_end:.2f} in setup 1), "
                              f"turns Z 0..{z_end - z_split:.2f}, "
                              f"drills to Z{z_hd:.2f}, bores to Z{z_hb:.2f}, "
                              f"stock = result of setup 1"))
        p.update(threads=th1, partoff_z_ref=z_end - face_allow,
                 hole_depth_drill=z_hd, hole_depth_bore=z_hb,
                 setup_note=(f"SETUP 1 of 2: from bar, Z0 = right face, "
                             f"turns Z 0..{z_split:.2f}, drills to Z{z_hd:.2f}, "
                             f"bores to Z{z_hb:.2f}, "
                             f"parts off at Z{z_end - face_allow:.2f}"))
        prof = prof1
        gcode2 = os.path.splitext(gcode)[0] + "_2" + os.path.splitext(gcode)[1]
        rep["z_split"] = round(z_split, 3)
        rep["z_end"] = round(z_end, 3)
        print(f"Два установа: передача работы на z {z_split:.1f} "
              f"(зажим {grip:g} мм, припуск на подрезку {face_allow:g} мм)")
        print(f"   установ 1: z {0.0:.1f}..{z_split:.1f} + отверстие до z "
              f"{z_hd:.1f} (расточка {z_hb:.1f}) + отрезка на z "
              f"{z_end - face_allow:.1f}")
        print(f"   установ 2: z {z_end:.1f}..{z_split:.1f} после перехвата "
              f"(в своей СК z' 0.0..{z_end - z_split:.1f})")
        for th in th_over:
            print(f"   ⚠  резьба Ø{th['d']} на z {th['z_from']}..{th['z_to']} "
                  f"пересекает границу установов — оставлена первому целиком")

    def setup_row(n, path, st):
        """Строка установа для --report: что генератор сделал этим набором."""
        return {"setup": n, "gcode": os.path.abspath(path),
                "lines": st.get("lines"), "ops": st.get("ops", []),
                "arcs": st.get("arcs", 0),
                "uncut_mm3": round(st.get("uncut_mm3", 0.0), 1),
                "blade_mm": st.get("blade"),
                "blade_tight": bool(st.get("blade_tight")),
                "grooves": st.get("grooves", 0),
                "groove_volume_mm3": round(st.get("groove_volume_mm3", 0.0), 1),
                "left_passes": st.get("left_passes", 0),
                "left_volume_mm3": round(st.get("left_volume_mm3", 0.0), 1),
                "finish_tool": st.get("finish_tool") or 0,
                "pre_finish": st.get("pre_finish", 0)}

    stats = lathe_gcode.write(prof, p, gcode)
    rep["setups"].append(setup_row(1, gcode, stats))
    print(f"✅ Программа: {stats['lines']} строк → {gcode} "
          f"({os.path.getsize(gcode):,} байт)")
    print(f"   операции: {', '.join(stats['ops'][:8])}"
          + (f" … всего {len(stats['ops'])}" if len(stats['ops']) > 8 else "")
          + (f" | дуг в чистовом: {stats['arcs']}" if stats.get("arcs") else ""))
    for th in stats.get("thread_candidates", []):
        print(f"   ⓘ похоже на резьбу: Ø{th['d_model']:.2f} на z "
              f"{th['z_hi']:.1f}..{th['z_lo']:.1f} ({th['length']:.1f} мм) — "
              f"ряд ГОСТ 8724 даёт M{th['d']}; шаг из модели НЕ определить, "
              f"задайте LATHE_THREADS")
    if stats.get("threads"):
        print(f"   T6 резьбовой: {stats['threads']} резьб(ы) нарезано")
    if stats.get("finish_tool"):
        deep = (f"{stats['finish_max_depth']:.2f} мм по радиусу"
                if not stats.get("pre_finish")
                else f"{p['allowance']:g} мм — уступы выбраны заранее "
                     f"({stats['pre_finish']} проход(а) PreFinish)")
        print(f"   T{stats['finish_tool']} чистовой: {p['finish_insert']} "
              f"({p['finish_nose_angle']:g}°-ромб) — им же считается "
              f"достижимость; снимает {deep}")
        for z in stats.get("pre_finish_zones", []):
            print(f"      уступ z {z['z_hi']:.2f}..{z['z_lo']:.2f}: черновой "
                  f"оставлял бы {z['max_extra'] + p['allowance']:.2f} мм")
    if stats.get("left_passes"):
        print(f"   T3 левый проходной: {stats['left_passes']} проход(а), "
              f"{stats['left_volume_mm3']:.0f} мм³ — правому резцу за уступ "
              f"не зайти")
    if stats.get("uncut_mm3"):
        print(f"   ⚠  НЕ ОБРАБОТАНО: {stats['uncut_mm3']:.0f} мм³ "
              f"(второй установ + недобранные донья канавок)")
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

    if prof2 is not None:
        stats2 = lathe_gcode.write(prof2, p2, gcode2)
        rep["setups"].append(setup_row(2, gcode2, stats2))
        print(f"✅ Программа установа 2: {stats2['lines']} строк → {gcode2} "
              f"({os.path.getsize(gcode2):,} байт)")
        print(f"   операции: {', '.join(stats2['ops'][:8])}"
              + (f" … всего {len(stats2['ops'])}" if len(stats2['ops']) > 8 else ""))

    if args.simulate_own:
        print("Съём материала по программе...")
        from lathe import lathe_sim
        try:
            res = lathe_sim.simulate(
                gcode, prof,
                stem + ("_sim1.step" if prof2 is not None else "_sim.step"),
                diameter_mode=p["diameter_mode"])
            if prof2 is not None:
                print(f"   установ 1 → {res['step']} (объём {res['volume']:.1f} мм³)")
                # Второй установ начинает НЕ с целого прутка, а с того, что
                # осталось от первого; его программа написана в СК перевёрнутой
                # детали, поэтому разворачиваем её обратно (z' = z_end − z) —
                # тогда обе половины считаются в одной раме и результат прямо
                # сравним с моделью. Правый торец детали в z0, значит z_end = −L.
                res = lathe_sim.simulate(
                    gcode2, prof, stem + "_sim.step",
                    diameter_mode=p["diameter_mode"],
                    stock_profile=res.get("profile"),
                    stock_bore=res.get("bore"),
                    z_mirror=-float(prof["length"]))
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
        from nx import nx_lathe_sim

        def sim_kwargs(st):
            """Инструмент для ISV.

            Геометрия — из АКТИВНОГО НАБОРА (`sim_tools`), одним куском вместе
            с формой пластины в NX: угол и форма обязаны совпадать, иначе
            симулятор проверит не ту программу, которую спланировали.
            Своё каждый установ объявляет сам: ширина канавочной пластины и
            участие левого резца зависят от того, что досталось именно этой
            половине детали."""
            return dict(
                sim_tools,
                groove_width=st.get("blade") or 0.0,
                groove_tool_number=p.get("groove_tool_number", 2),
                left_tool_number=(p.get("left_tool_number", 3)
                                  if st.get("left_passes") else 0),
                finish_tool_number=st.get("finish_tool") or 0)

        def sim_tail(r):
            return ((f"  (машинное время {r['machine_time']}"
                     + (f", {r['triangles']} треуг." if r.get("triangles") else "")
                     + ")") if r.get("machine_time") else "")

        which = (args.sim_setup
                 or getattr(config, "LATHE_SIM_SETUP", None) or "both")
        both = prof2 is not None and which == "both"
        stock = stem + "_stock.stp"
        print("Симуляция на виртуальном токарном станке NX ISV "
              "(откроется окно NX, трогать не нужно)"
              + (": ДВА УСТАНОВА подряд..." if both else "..."))
        res = res1 = res2 = None
        try:
            if prof2 is None:
                res = nx_lathe_sim.simulate(gcode, stock, **sim_kwargs(stats))
            else:
                # Каждый установ гоняется по ЧИСТОМУ ПРУТКУ и обрабатывает свою
                # половину. Подавать результат первого заготовкой во второй
                # НЕЛЬЗЯ: фасетная заготовка в ISV стоит лишних 0.15 мм по
                # радиусу плюс конусность (замерено, runs/87). Итоговая деталь
                # собирается пересечением двух результатов — для тела вращения
                # это точно (lathe/lathe_assemble.py).
                # Пруток годится обоим установам: он симметричен по z и
                # накрывает деталь в любой из двух рам.
                if which in ("1", "both"):
                    res1 = nx_lathe_sim.simulate(gcode, stock,
                                                 out_stem=stem + "_setup1",
                                                 **sim_kwargs(stats))
                    print(f"   установ 1 → {res1['step']}{sim_tail(res1)}")
                    rep["sim"]["setup1"] = res1["step"]
                    rep["sim"]["machine_time_1"] = res1.get("machine_time")
                if which in ("2", "both"):
                    res2 = nx_lathe_sim.simulate(gcode2, stock,
                                                 out_stem=stem + "_setup2",
                                                 **sim_kwargs(stats2))
                    print(f"   установ 2 → {res2['step']}{sim_tail(res2)}"
                          f"   (в раме УСТАНОВА 2, не детали)")
                    rep["sim"]["setup2"] = res2["step"]
                    rep["sim"]["machine_time_2"] = res2.get("machine_time")
                res = res1 or res2
        except Exception as e:
            print(f"⚠  NX-симуляция не удалась: {e}")
            sys.exit(2)

        if not both:
            if prof2 is None:
                # результат возвращён в раму детали — ложится на out_part.step
                print(f"✅ NX ISV: обработанная заготовка → {res['step']}"
                      f"{sim_tail(res)}")
                print(f"   ▶ наложить на деталь: {res['step']}  +  "
                      f"{stem + '_part.step'}  (обе в раме детали, ось Z)")
            else:
                print(f"⚠  прогнан только установ {which} (--sim-setup): "
                      f"итоговая деталь НЕ собрана, чужая половина в сверке "
                      f"пойдёт недорезом")
            if which != "2":       # результат установа 2 — в своей раме
                try:
                    from cam import step_diff
                    d = step_diff.diff(stem + "_part.step", res["step"],
                                       stem + "_nxdiff.json")
                    rep["sim"]["result"] = res["step"]
                    rep["diff"] = {"undercut_total_mm3": d["undercut_total_mm3"],
                                   "overcut_total_mm3": d["overcut_total_mm3"]}
                    print(f"   сверка NX-результата с моделью: "
                          f"недорез {d['undercut_total_mm3']:.1f} мм³, "
                          f"зарез {d['overcut_total_mm3']:.1f} мм³")
                except Exception as e:
                    print(f"   (сверка не выполнена: {e})")
        else:
            from lathe import lathe_assemble
            full = stem + "_full.step"
            try:
                a = lathe_assemble.assemble(res1["step"], res2["step"], full,
                                            z_end=z_end, r_split=r_split)
            except Exception as e:
                print(f"⚠  итоговая деталь не собралась: {e}")
                sys.exit(2)
            z0, z1 = a["z_range"]
            rep["sim"]["full"] = full
            rep["sim"]["full_volume_mm3"] = round(a["volume"], 1)
            print(f"✅ ИТОГ двух установов → {full}  (солид, объём "
                  f"{a['volume']:.1f} мм³, z {z0:.2f}..{z1:.2f})")
            print(f"   ▶ наложить на деталь: {full}  +  {stem + '_part.step'}")
            # Сверять итог осевым анализатором, а не общим step_diff: у него
            # пояса пересчитываются в радиус и снимается плёнка моста ISV
            # (см. lathe/lathe_diff.py). Границы установа не задаём — деталь
            # обработана целиком, за неё отвечают оба.
            try:
                from lathe import lathe_diff
                d = lathe_diff.analyse(stem + "_part.step", full,
                                       stem + "_nxdiff.json")
                with open(stem + "_nxdiff.md", "w", encoding="utf-8") as f:
                    f.write(lathe_diff.report(d))
                rep["diff"] = {k: d.get(k) for k in (
                    "undercut_total_mm3", "overcut_total_mm3",
                    "undercut_fixed_mm3", "overcut_fixed_mm3", "film_mm",
                    "part_volume_mm3", "result_volume_mm3")}
                rep["diff"]["by_z"] = d.get("by_z", [])
                rep["diff"]["report_md"] = stem + "_nxdiff.md"
                print(f"   сверка итога с моделью: недорез "
                      f"{d['undercut_total_mm3']:.1f} мм³, зарез "
                      f"{d['overcut_total_mm3']:.1f} мм³ (сырое)")
                if d.get("film_mm"):
                    print(f"   с поправкой на плёнку моста {d['film_mm']:.3f} мм: "
                          f"недорез {d['undercut_fixed_mm3']:.1f} мм³, зарез "
                          f"{d['overcut_fixed_mm3']:.1f} мм³")
                print(f"   разбор по поясам → {stem + '_nxdiff.md'}  "
                      f"(граница установов z {z_split:.2f})")
            except Exception as e:
                print(f"   (сверка не выполнена: {e})")


    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".",
                    exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print(f"📋 отчёт прогона → {args.report}")


if __name__ == "__main__":
    main()
