#!/usr/bin/env python3
"""
lathe_tools.py — КАТАЛОГ токарного инструмента и выбор АКТИВНОГО набора.

Зачем. До этого инструмент был вшит в конфиг плоскими ключами: один черновой
резец, один чистовой, один канавочный, и каждый включался своим флагом
(`--no-finish-tool`, `--no-groove-tool`, …). Работать так с агентной петлёй
нельзя: агенту надо видеть, ЧТО вообще есть в наличии, ЧТО из этого выдано
генератору, и уметь выдать другой набор.

Поэтому здесь два понятия:

  * КАТАЛОГ — всё, что у нас существует физически (резцы разной геометрии,
    канавочные пластины разной ширины, свёрла, расточная борштанга…);
  * АКТИВНЫЙ НАБОР — подмножество каталога, выданное генератору. Программа
    строится ТОЛЬКО этими инструментами.

Почему это меняет программу, а не только шапку G-кода. Достижимость считается
по геометрии резца: φ₁ = 180 − φ − ε, где φ даёт державка, ε — угол при вершине
пластины. Что не достаёт проходной — уходит канавочному и левому; чего в наборе
нет — не делается вовсе и попадает в «не обработано». Убери из набора чистовой
35°-ромб, и узкие уступы у шестигранника станет некому выбрать: 55°-ромб туда
заходит мельче. Ровно это и должен нащупывать агент.

РОЛИ фиксированы, потому что за ними закреплены номера T в программе и карманы
револьвера (стойка крутит револьвер по номеру T, см. .claude/CLAUDE.md I9):

    rough  T1   черновой проходной        groove T2   канавочный / отрезка
    left   T3   левый проходной           drill  T4   сверло
    bore   T5   расточная борштанга       thread T6   резьбовой
    center T7   центровочное              finish T8   чистовой проходной

В активном наборе на роль берётся ОДИН инструмент. Если активных на роль
несколько — берётся первый по каталогу (для канавочных — самый узкий, он лезет
во все канавки, куда лезет широкий), остальные игнорируются с записью в лог.

ГРАНИЦА ОТВЕТСТВЕННОСТИ. Каталог задаёт НАЛИЧИЕ роли и ГЕОМЕТРИЮ проходных
резцов — то, что меняет достижимость. Диаметр сверла по-прежнему выводится из
отверстия детали (Ø отверстия − 2·припуск на расточку), а не берётся из
каталога: подбор сверла из ряда — отдельная задача, здесь её нет.
"""

# ── КАТАЛОГ ─────────────────────────────────────────────────────────────────
# Здесь только то, что РЕАЛЬНО ЕСТЬ — заводской комплект 14-31A (КнААЗ, станок
# DMG CTX500 beta). Придумывать инструмент нельзя: агент «решит» задачу тем,
# чего у цеха нет, и результат будет ложным. Появится новый резец в наличии —
# он добавляется в `LATHE_TOOLS` конфига или сюда, но только по факту.
# Поля резца: nose_angle — ε, угол при вершине пластины; approach — φ, угол в
# плане (даёт державка); size — размер пластины (вписанная окружность), от него
# зависит длина подреза; nx_shape — форма пластины в NX ISV, ОБЯЗАНА совпадать
# с nose_angle, иначе симулятор проверяет не ту программу, которую спланировали.
CATALOG = [
    # ── проходные резцы: их геометрия решает, что достижимо ──
    {"id": "rough_dcmt55", "role": "rough", "number": 1,
     "insert": "DCMT070204R", "nose_angle": 55.0, "nose_radius": 0.4,
     "size": 6.35, "approach": 107.5, "nx_shape": "Diamond55",
     "desc": "черновой проходной, ромб 55°, R0.4, державка 107.5° — "
             "заводской; φ₁ = 17.5°"},
    {"id": "finish_vcmt35", "role": "finish", "number": 8,
     "insert": "VCMT110304", "nose_angle": 35.0, "nose_radius": 0.4,
     "size": 6.35, "approach": 107.5, "nx_shape": "Diamond35",
     "desc": "чистовой проходной, ромб 35°, R0.4 — заводской; φ₁ = 37.5°, "
             "заходит в узкие уступы"},
    {"id": "left_dcmt55", "role": "left", "number": 3,
     "insert": "DCMT070204L", "nose_angle": 55.0, "nose_radius": 0.4,
     "size": 6.35, "approach": 107.5, "nx_shape": "Diamond55",
     "desc": "ЛЕВЫЙ проходной, зеркальный чернового — берёт участки за уступом "
             "со стороны торца, куда правый не заходит"},

    # ── канавочные: ширина пластины решает, в какую канавку она влезет ──
    {"id": "groove_3", "role": "groove", "number": 2, "width": 3.0,
     "desc": "канавочный/отрезной, пластина 3.0 мм — жёсткий, но в узкие "
             "канавки не лезет"},
    {"id": "groove_2", "role": "groove", "number": 2, "width": 2.0,
     "desc": "канавочный/отрезной, пластина 2.0 мм"},
    {"id": "groove_1", "role": "groove", "number": 2, "width": 1.0,
     "desc": "канавочный/отрезной, пластина 1.0 мм — самая узкая в ходу"},

    # ── осевой инструмент: каталог задаёт только НАЛИЧИЕ ──
    {"id": "drill", "role": "drill", "number": 4,
     "desc": "спиральное сверло; диаметр выводится из отверстия детали, "
             "а не из каталога"},
    {"id": "bore", "role": "bore", "number": 5,
     "desc": "расточная борштанга — доводит отверстие до размера после сверла"},
    {"id": "center", "role": "center", "number": 7, "diameter": 3.15,
     "desc": "центровочное сверло Ø3.15 (ГОСТ 14952) — без него сверло уводит"},
    {"id": "thread", "role": "thread", "number": 6,
     "desc": "резьбовой резец; нарезание включается только явным объявлением "
             "LATHE_THREADS, шаг из модели НЕ выводится"},
]

ROLES = ("rough", "finish", "left", "groove", "drill", "bore", "center", "thread")

# Роли, без которых программу не собрать вовсе.
REQUIRED = ("rough",)


def catalog(extra=None):
    """Каталог: встроенный плюс `extra` (config.LATHE_TOOLS), по id."""
    out = {t["id"]: dict(t) for t in CATALOG}
    for t in (extra or []):
        if not t.get("id") or t.get("role") not in ROLES:
            raise ValueError(f"инструмент без id или с чужой ролью: {t}")
        out[t["id"]] = {**out.get(t["id"], {}), **t}
    return out


def default_active(cat=None):
    """Набор по умолчанию — заводской комплект 14-31A: 55°-черновой,
    35°-чистовой, левый, самая узкая канавочная, весь осевой инструмент."""
    cat = cat or catalog()
    want = ("rough_dcmt55", "finish_vcmt35", "left_dcmt55",
            "groove_3", "groove_2", "groove_1",
            "drill", "bore", "center", "thread")   # весь пул
    return [i for i in want if i in cat]


def resolve(active_ids=None, extra=None, log=None):
    """Активный набор → параметры генератора и симуляции.

    Возвращает (params, sim, chosen), где
      params — то, что подмешивается в `p` у run_lathe.py;
      sim    — то, что уходит в nx_lathe_sim.simulate();
      chosen — {роль: инструмент} для отчёта агенту.

    Роли без активного инструмента ВЫКЛЮЧАЮТСЯ: программа их работу не делает,
    и она честно попадает в «не обработано».
    """
    say = log or (lambda m: None)
    cat = catalog(extra)
    ids = list(active_ids) if active_ids is not None else default_active(cat)

    unknown = [i for i in ids if i not in cat]
    if unknown:
        raise ValueError(f"нет таких инструментов в каталоге: {unknown}. "
                         f"Доступны: {sorted(cat)}")

    by_role = {}
    for i in ids:
        by_role.setdefault(cat[i]["role"], []).append(cat[i])
    for role, lst in by_role.items():
        if role == "groove":
            # канавочные — НЕ «или-или»: генератор берёт самую широкую, что
            # влезает во все канавки, а узкие остаются как запас. Поэтому в
            # набор идёт весь ряд, от узкой к широкой.
            lst.sort(key=lambda t: t.get("width", 0.0))
            continue
        if len(lst) > 1:
            say(f"на роль {role} активны несколько "
                f"({', '.join(t['id'] for t in lst)}), беру {lst[0]['id']}")
    chosen = {role: lst[0] for role, lst in by_role.items()}

    missing = [r for r in REQUIRED if r not in chosen]
    if missing:
        raise ValueError(f"в активном наборе нет обязательной роли: {missing}")

    r = chosen["rough"]
    params = {
        "insert": r["insert"], "nose_radius": r["nose_radius"],
        "nose_angle": r["nose_angle"], "insert_edge": r["size"],
        "approach_angle": r["approach"],
        # роли, которых нет в наборе, выключаются
        "finish_tool": "finish" in chosen,
        "groove_tool": "groove" in chosen,
        "left_tool": "left" in chosen,
        "drill": "drill" in chosen,
        "center_drill": "center" in chosen,
    }
    sim = {"nose_radius": r["nose_radius"], "nose_angle": r["nose_angle"],
           "insert_size": r["size"], "insert_shape": r.get("nx_shape")}

    if "finish" in chosen:
        f = chosen["finish"]
        params.update(finish_tool_number=f["number"], finish_insert=f["insert"],
                      finish_nose_angle=f["nose_angle"],
                      finish_insert_edge=f["size"])
        sim.update(finish_nose_angle=f["nose_angle"],
                   finish_nose_radius=f["nose_radius"],
                   finish_insert_size=f["size"],
                   finish_insert_shape=f.get("nx_shape"))
    if "groove" in chosen:
        # Ширины из каталога — это ряд пластин, которые ЕСТЬ. Генератор возьмёт
        # самую широкую, что влезает во все канавки (шире = жёстче), но уже
        # самой узкой наличной взять не может: ниже этого — заявка на закупку.
        gl = by_role["groove"]
        params.update(groove_tool_number=gl[0]["number"],
                      groove_width=max(g["width"] for g in gl),
                      groove_width_min=min(g["width"] for g in gl))
    else:
        params["partoff"] = False        # отрезает канавочный, другого нет
    if "left" in chosen:
        params["left_tool_number"] = chosen["left"]["number"]
    if "drill" in chosen:
        params["drill_tool_number"] = chosen["drill"]["number"]
    if "bore" in chosen:
        params["bore_tool_number"] = chosen["bore"]["number"]
    if "center" in chosen:
        params.update(center_tool_number=chosen["center"]["number"],
                      center_drill_d=chosen["center"].get("diameter", 3.15))
    if "thread" in chosen:
        params["thread_tool_number"] = chosen["thread"]["number"]

    say("активный набор: " + ", ".join(
        f"T{t['number']} {t['id']}" for t in
        sorted(chosen.values(), key=lambda t: t["number"])))
    off = [r for r in ROLES if r not in chosen]
    if off:
        say(f"ролей нет в наборе (работа не делается): {', '.join(off)}")
    return params, sim, chosen


def describe(extra=None):
    """Каталог в компактном виде — уходит агенту в промпт."""
    return [{"id": t["id"], "role": t["role"], "T": t["number"],
             **({"nose_angle": t["nose_angle"], "approach": t["approach"],
                 "nose_radius": t["nose_radius"], "size": t["size"]}
                if "nose_angle" in t else {}),
             **({"width": t["width"]} if "width" in t else {}),
             "desc": t["desc"]}
            for t in catalog(extra).values()]


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
    ap = argparse.ArgumentParser(description="Каталог токарного инструмента")
    ap.add_argument("--json", action="store_true", help="выдать каталог как JSON")
    ap.add_argument("--resolve", metavar="IDS",
                    help="проверить активный набор (id через запятую)")
    a = ap.parse_args()
    if a.resolve is not None:
        ids = [s.strip() for s in a.resolve.split(",") if s.strip()]
        params, sim, chosen = resolve(ids, log=lambda m: print("  " + m))
        print(json.dumps({"params": params, "sim": sim}, ensure_ascii=False,
                         indent=1))
        return
    if a.json:
        print(json.dumps(describe(), ensure_ascii=False, indent=1))
        return
    print(f"{'id':<16} {'роль':<8} {'T':>2}  что это")
    for t in catalog().values():
        print(f"{t['id']:<16} {t['role']:<8} {t['number']:>2}  {t['desc']}")
    print("\nпо умолчанию активны: " + ", ".join(default_active()))


if __name__ == "__main__":
    main()
