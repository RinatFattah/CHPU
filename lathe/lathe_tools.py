#!/usr/bin/env python3
"""
lathe_tools.py — ПАРК ИНСТРУМЕНТА завода и раскладка работ по нему.

Два понятия, и они про разное:

  * ПАРК (`CATALOG`) — что физически есть в цехе. Только свойства железа:
    тип, исполнение, углы, радиус при вершине, размер пластины, ширина.
  * ДОСТУПНЫЕ — подмножество парка, выданное генератору. Программа строится
    ТОЛЬКО из них.

**Ролей в парке НЕТ.** «Черновой» и «чистовой» — это не свойство резца, а
решение о том, как его применить: один и тот же ромб может идти и начерно, и
начисто. Поэтому раскладка работ ВЫВОДИТСЯ здесь, из геометрии выданных
инструментов, а не объявляется в справочнике.

Как выводится (`plan`):

  * ЧИСТОВЫМ — и выборкой уступов — идёт проходной с НАИБОЛЬШИМ φ₁, где
    φ₁ = 180 − φ − ε (φ — угол державки, ε — угол при вершине пластины).
    Чем больше φ₁, тем дальше резец заходит в уступ, не волоча вспомогательной
    кромкой по уже обточенной стенке.
  * ЧЕРНОВЫМ — самый ЖЁСТКИЙ: наибольший ε (тупее вершина — прочнее), при
    равном ε — большая пластина.
  * Если проходной ОДИН, он делает и то, и другое. Ровно так поступил бы
    токарь, и генератор это умеет (`finish_tool = False`).
  * ЛЕВОЕ исполнение берёт участки за уступом, открытые в сторону торца.
  * КАНАВОЧНЫЕ идут рядом: генератор возьмёт самую широкую, что влезает во
    все канавки (шире = жёстче), но не уже самой узкой наличной.

НОМЕРА T — конвенция НАЛАДКИ, а не признак резца: стойка крутит револьвер по
номеру T, поэтому номер закреплён за РАБОТОЙ, а какой инструмент туда встанет,
решает раскладка.
"""

# ── ПАРК ИНСТРУМЕНТА ────────────────────────────────────────────────────────
# Только то, что РЕАЛЬНО ЕСТЬ — комплект под 14-31A (КнААЗ, DMG CTX500 beta).
# Придумывать инструмент нельзя: агент «решит» задачу тем, чего у цеха нет.
# Появился новый резец в наличии — добавляется сюда или в `LATHE_TOOLS`, но
# только по факту.
#
# Поля проходного: hand — исполнение державки (R правое, точит к патрону;
# L левое, от патрона); nose_angle — ε, угол при вершине пластины; approach —
# φ, угол в плане, даёт державка; size — размер пластины (вписанная
# окружность), от него зависит длина подреза; nx_shape — форма пластины в
# NX ISV, ОБЯЗАНА соответствовать nose_angle, иначе симулятор проверит не ту
# геометрию, под которую спланирована программа.
#
# ДВА УГЛА В ПЛАНЕ. Работает ТОЛЬКО `approach`; `factory_approach` — справка.
#
#   approach = 107.5° — угол, под который планируется ДОСТИЖИМОСТЬ. Это угол
#              ШАБЛОНА NX (подтип OD_55_R), то есть того резца, которым нас
#              будут проверять в ISV. Не цеховой.
#   factory_approach = 93° — настоящая цеховая державка (SDJCL2525M11, код J
#              по ISO 5608): UST1.NC на 14-31A печатает по своим резцам
#              `fi1=32.000` (= 180−93−55) и `fi1=52.000` (= 180−93−35).
#
# ПОЧЕМУ ПЛАНИРУЕМ ПОД ХУДШИЙ, А НЕ ПОД НАСТОЯЩИЙ — проверено дважды, обе
# попытки взять 93° кончились одинаково:
#   * runs/87: под φ₁ = 32° получистовой спускается круче, чем шаблонный резец
#     ISV отслеживает, и шестигранник срезается КОНУСОМ до 1.372 мм по радиусу;
#   * runs/112 (11.08.2026): то же самое. Приёмка по радиусу при этом выглядит
#     лучше (−0.096 против +0.177, фланок схода резьбы вычищается, канавка
#     тоньше 1 мм не нужна), НО деталь глазами — с конусом вместо цилиндра на
#     шестиграннике. Метрика этого не видит: пояса «грани под ключ» из счёта
#     исключены, и улучшение по числам скрывает порчу по виду.
#
# Вывод, зафиксированный Денисом: 107.5° остаётся. Направление консервативное —
# программу под 107.5 державка 93 исполнит, обратное неверно. Цена известна и
# принята: правому резцу доступно меньше, остаток на фаске захода/схода резьбы
# уходит канавочному, пластина оказывается тесной. Эта цена ВЫНЕСЕНА ИЗ
# ПРИЁМКИ отдельной строкой отчёта (lathe_diff, зоны «недостижимо набором»),
# а не спрятана.
# НЕ МЕНЯТЬ на 93 без прогона на шестиграннике и показа детали глазами.
CATALOG = [
    {"id": "dcmt55_r", "type": "turning", "hand": "R", "insert": "DCMT070204R",
     "nose_angle": 55.0, "nose_radius": 0.4, "size": 6.35,
     "approach": 107.5, "factory_approach": 93.0, "nx_shape": "Diamond55",
     "desc": "проходной правый, ромб 55°, R0.4, державка 107.5° (φ₁ = 17.5°) — "
             "жёсткий, но в уступы заходит мелко"},
    {"id": "vcmt35_r", "type": "turning", "hand": "R", "insert": "VCMT110304",
     "nose_angle": 35.0, "nose_radius": 0.4, "size": 6.35,
     "approach": 107.5, "factory_approach": 93.0, "nx_shape": "Diamond35",
     "desc": "проходной правый, ромб 35°, R0.4, державка 107.5° (φ₁ = 37.5°) — "
             "острый, заходит в узкие уступы, но вершина слабее"},
    {"id": "dcmt55_l", "type": "turning", "hand": "L", "insert": "DCMT070204L",
     "nose_angle": 55.0, "nose_radius": 0.4, "size": 6.35,
     "approach": 107.5, "factory_approach": 93.0, "nx_shape": "Diamond55",
     "desc": "проходной ЛЕВЫЙ, зеркальный dcmt55_r — точит от патрона, берёт "
             "участки за уступом со стороны торца"},

    {"id": "blade_3", "type": "grooving", "width": 3.0,
     "desc": "канавочная/отрезная пластина 3.0 мм — жёсткая, в узкие канавки "
             "не лезет"},
    {"id": "blade_2", "type": "grooving", "width": 2.0,
     "desc": "канавочная/отрезная пластина 2.0 мм"},
    {"id": "blade_1", "type": "grooving", "width": 1.0,
     "desc": "канавочная/отрезная пластина 1.0 мм — самая узкая в цехе"},

    {"id": "drill", "type": "drilling",
     "desc": "спиральное сверло; диаметр выводится из отверстия детали, а не "
             "из парка"},
    {"id": "boring_bar", "type": "boring",
     "desc": "расточная борштанга — доводит отверстие до размера после сверла"},
    {"id": "center_drill", "type": "centering", "diameter": 3.15,
     "desc": "центровочное сверло Ø3.15 (ГОСТ 14952) — без него сверло уводит"},
    {"id": "thread_tool", "type": "threading",
     "desc": "резьбовой резец; нарезание включается только явным объявлением "
             "LATHE_THREADS, шаг из модели НЕ выводится"},
]

TYPES = ("turning", "grooving", "drilling", "boring", "centering", "threading")

# Номера T закреплены за РАБОТОЙ (наладка револьвера), не за инструментом.
T_ROUGH, T_GROOVE, T_LEFT, T_DRILL = 1, 2, 3, 4
T_BORE, T_THREAD, T_CENTER, T_FINISH = 5, 6, 7, 8


def catalog(extra=None):
    """Парк: встроенный плюс `extra` (config.LATHE_TOOLS), по id."""
    out = {t["id"]: dict(t) for t in CATALOG}
    for t in (extra or []):
        if not t.get("id") or t.get("type") not in TYPES:
            raise ValueError(f"инструмент без id или с чужим типом: {t}. "
                             f"Типы: {TYPES}")
        out[t["id"]] = {**out.get(t["id"], {}), **t}
    return out


def all_ids(extra=None):
    """Весь парк — то, что физически есть в цехе."""
    return list(catalog(extra))


def phi1(t):
    """Вспомогательный угол в плане, °: 180 − φ − ε.

    Это и есть мера достижимости. Чем он больше, тем дальше резец врезается
    в уступ, не волоча вспомогательной кромкой по уже обточенной стенке.
    """
    return 180.0 - float(t["approach"]) - float(t["nose_angle"])


def plan(available=None, extra=None, log=None):
    """Доступные инструменты → раскладка работ и параметры генератора.

    Возвращает (params, sim, assigned):
      params   — подмешивается в `p` у run_lathe.py;
      sim      — уходит в nx_lathe_sim.simulate();
      assigned — кто какую работу получил, для отчёта агенту.

    `available=None` — доступен весь парк.
    """
    say = log or (lambda m: None)
    cat = catalog(extra)
    ids = list(available) if available is not None else list(cat)
    unknown = [i for i in ids if i not in cat]
    if unknown:
        raise ValueError(f"нет таких инструментов в парке: {unknown}. "
                         f"Есть: {sorted(cat)}")
    have = [cat[i] for i in ids]

    def of(kind, hand=None):
        return [t for t in have if t["type"] == kind
                and (hand is None or t.get("hand") == hand)]

    right = of("turning", "R")
    if not right:
        raise ValueError("программу не собрать: среди доступных нет ни одного "
                         "правого проходного резца")
    # чистовой — максимальный φ₁ (дальше всех заходит в уступ);
    # черновой — самый жёсткий: тупее вершина, затем крупнее пластина
    fin = max(right, key=phi1)
    rough = max(right, key=lambda t: (t["nose_angle"], t["size"]))
    same = fin["id"] == rough["id"]
    if same and len(right) > 1:
        # выбор совпал: значит один и тот же резец и жёстче всех, и достаёт
        # дальше всех — остальные просто хуже по обоим признакам
        say(f"проходных доступно {len(right)}, но {rough['id']} лучше прочих "
            f"и по жёсткости, и по достижимости — работает один")
    elif same:
        say(f"проходной один ({rough['id']}, φ₁ = {phi1(rough):.1f}°) — он же "
            f"ведёт и черновую, и чистовую")
    else:
        say(f"черновая — {rough['id']} (ε = {rough['nose_angle']:.0f}°), "
            f"чистовая — {fin['id']} (φ₁ = {phi1(fin):.1f}°)")

    left = of("turning", "L")
    blades = sorted(of("grooving"), key=lambda t: t["width"])
    drill, bore = of("drilling"), of("boring")
    center, thread = of("centering"), of("threading")

    params = {
        "insert": rough["insert"], "nose_radius": rough["nose_radius"],
        "nose_angle": rough["nose_angle"], "insert_edge": rough["size"],
        "approach_angle": rough["approach"], "tool_number": T_ROUGH,
        # один проходной = чистовую ведёт он же (генератор это умеет)
        "finish_tool": not same,
        "left_tool": bool(left), "groove_tool": bool(blades),
        "drill": bool(drill), "center_drill": bool(center),
    }
    sim = {"nose_radius": rough["nose_radius"], "nose_angle": rough["nose_angle"],
           "insert_size": rough["size"], "insert_shape": rough.get("nx_shape")}

    if not same:
        params.update(finish_tool_number=T_FINISH, finish_insert=fin["insert"],
                      finish_nose_angle=fin["nose_angle"],
                      finish_insert_edge=fin["size"])
        sim.update(finish_nose_angle=fin["nose_angle"],
                   finish_nose_radius=fin["nose_radius"],
                   finish_insert_size=fin["size"],
                   finish_insert_shape=fin.get("nx_shape"))
    if left:
        params["left_tool_number"] = T_LEFT
    if blades:
        # ряд пластин: с самой широкой начинаем подбор, ниже самой узкой
        # опускаться нельзя — это уже заявка на закупку
        params.update(groove_tool_number=T_GROOVE,
                      groove_width=blades[-1]["width"],
                      groove_width_min=blades[0]["width"])
    else:
        params["partoff"] = False          # отрезает канавочный, другого нет
    if drill:
        params["drill_tool_number"] = T_DRILL
    if bore:
        params["bore_tool_number"] = T_BORE
    if center:
        params.update(center_tool_number=T_CENTER,
                      center_drill_d=center[0].get("diameter", 3.15))
    if thread:
        params["thread_tool_number"] = T_THREAD

    assigned = {
        "черновая (T1)": rough["id"],
        "чистовая (T8)": ("тот же резец, что на черновой" if same
                          else fin["id"]),
        "за уступом, левый (T3)": left[0]["id"] if left else None,
        "канавки и отрезка (T2)": [b["id"] for b in blades] or None,
        "сверление (T4)": drill[0]["id"] if drill else None,
        "расточка (T5)": bore[0]["id"] if bore else None,
        "центровка (T7)": center[0]["id"] if center else None,
        "резьба (T6)": thread[0]["id"] if thread else None,
    }
    idle = [t["id"] for t in have
            if t["id"] not in {rough["id"], fin["id"]}
            and t not in left[:1] + blades + drill[:1] + bore[:1]
            + center[:1] + thread[:1]]
    if idle:
        say(f"выданы, но не пригодились: {', '.join(idle)}")
    return params, sim, assigned


def signature(available=None, extra=None):
    """Канонический слепок ТОГО, ЧТО ДОЙДЁТ ДО ГЕНЕРАТОРА.

    Сравнивать списки id мало: разные наборы могут дать одни и те же параметры
    (добавили второй проходной, который ничем не лучше — программа прежняя), и
    наоборот. Слепок берётся с `params`, поэтому равенство слепков означает
    равенство программ.
    """
    import json
    params, _, _ = plan(available, extra)
    return json.dumps(params, sort_keys=True, ensure_ascii=False)


def describe(extra=None):
    """Парк в компактном виде — уходит агенту в промпт."""
    out = []
    for t in catalog(extra).values():
        row = {"id": t["id"], "тип": t["type"]}
        if t["type"] == "turning":
            row.update({"исполнение": t["hand"],
                        "ε_угол_при_вершине": t["nose_angle"],
                        "φ_угол_державки": t["approach"],
                        "φ1_достижимость": round(phi1(t), 1),
                        "R_радиус_при_вершине": t["nose_radius"],
                        "размер_пластины": t["size"]})
        if "width" in t:
            row["ширина_мм"] = t["width"]
        if "diameter" in t:
            row["диаметр_мм"] = t["diameter"]
        row["что_это"] = t["desc"]
        out.append(row)
    return out


def main():
    import argparse
    import json
    import sys
    for s in (sys.stdout, sys.stderr):
        if (getattr(s, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(
        description="Парк токарного инструмента и раскладка работ по нему")
    ap.add_argument("--json", action="store_true", help="парк как JSON")
    ap.add_argument("--plan", metavar="IDS",
                    help="раскладка для набора доступных (id через запятую; "
                         "пусто — весь парк)")
    a = ap.parse_args()
    if a.plan is not None:
        ids = [s.strip() for s in a.plan.split(",") if s.strip()] or None
        try:
            params, sim, assigned = plan(ids, log=lambda m: print("  " + m))
        except ValueError as e:
            print(f"❌ {e}")
            raise SystemExit(1)
        print(json.dumps({"раскладка": assigned, "параметры": params},
                         ensure_ascii=False, indent=1))
        return
    if a.json:
        print(json.dumps(describe(), ensure_ascii=False, indent=1))
        return
    print(f"{'id':<14} {'тип':<10} {'φ₁':>6}  что это")
    for t in catalog().values():
        f = f"{phi1(t):.1f}°" if t["type"] == "turning" else ""
        print(f"{t['id']:<14} {t['type']:<10} {f:>6}  {t['desc']}")
    print(f"\nвсего в парке: {len(CATALOG)}")


if __name__ == "__main__":
    main()
