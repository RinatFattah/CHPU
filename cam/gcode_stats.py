# -*- coding: utf-8 -*-
"""Числа о программе, по которым принимаются доработки (замечания ОЭЦМ КнААЗ).

Сравнивать файлы построчно нельзя: `Path.Op.Adaptive` даёт между прогонами
ОДНОГО И ТОГО ЖЕ кода разброс ~0.7 % рабочего хода. Поэтому принимаем по
величинам, на которые этот разброс не влияет, и печатаем их одинаково для
любой программы:

  * длина рабочего и холостого хода, оценка машинного времени;
  * смены инструмента и есть ли у каждой паспорт;
  * ОТВЕСНЫЕ врезания в материал (замечание «врезание без рампы»);
  * холостые «вниз по Z, потом вбок» (замечание «сначала Z, потом XY»).

Оценка времени — не замена ISV: стойка считает разгоны и торможения, здесь их
нет. Её назначение — ловить изменение на десятки процентов между прогонами.

    python cam/gcode_stats.py <файл.gcode> [ещё файлы...] [--md] [--list]
"""

import argparse
import json
import math
import os
import re

RAPID_RATE = 10000.0     # мм/мин — холостой ход; у DMC-635V около этого


def parse(path):
    """Разбор программы в модальном состоянии. Возвращает словарь чисел."""
    x = y = z = 0.0
    have_pos = False
    feed = 0.0
    mode = None                       # 0 = G0, 1 = G1, 2/3 = дуга
    op = "(до операций)"

    st = {
        "path": path,
        "lines": 0,
        "feed_len": 0.0,
        "rapid_len": 0.0,
        "feed_time": 0.0,             # мин
        "rapid_time": 0.0,
        "tool_changes": [],           # [{"t": 1, "passport": "..."|None}]
        "plunges": [],                # отвесные G1 вниз
        "z_then_xy": [],              # G0 вниз по Z, следом G0 вбок
        "ops": {},                    # имя → {"feed_len","rapid_len"}
        "z_min": None,
        "z_max": None,
    }

    # Верх заготовки — из шапки, чтобы отличить врезание В МЕТАЛЛ от подхода
    # по воздуху. Нет шапки → считаем врезанием любое отвесное движение вниз.
    stock_top = None

    prev_rapid_down = None            # предыдущий «G0 только вниз по Z»
    pending_tc = None                 # смена инструмента, ждущая паспорта
    # Замечание ОЭЦМ про порядок касается ПЕРВОГО перемещения после смены
    # инструмента, а там прежней позиции нет (фреза в точке смены), поэтому
    # разностями его не поймать — смотрим, какие адреса стоят в кадре.
    armed = True                      # начало программы = та же ситуация
    armed_z = None                    # кадр «G0 только по Z», ждущий пары

    src = open(path, encoding="utf-8", errors="replace").read().splitlines()
    for ln, raw in enumerate(src, 1):
        st["lines"] += 1
        line = raw.strip()
        if not line:
            continue

        m = re.match(r"\(Stock box:.*Z\s*(-?[\d.]+)\.\.(-?[\d.]+)\)", line)
        if m:
            stock_top = float(m.group(2))

        m = re.match(r"\(Begin operation:\s*(.*?)\)", line)
        if m:
            op = m.group(1)
            st["ops"].setdefault(op, {"feed_len": 0.0, "rapid_len": 0.0})

        m = re.search(r"\(\s*M0?6\s+T(\d+)\s*\)", line)
        if m:
            pending_tc = {"t": int(m.group(1)), "line": ln, "passport": None}
            st["tool_changes"].append(pending_tc)
            armed, armed_z = True, None

        m = re.match(r"\(Tool T(\d+):\s*(.*?)\)\s*$", line)
        if m and pending_tc is not None and pending_tc["t"] == int(m.group(1)):
            pending_tc["passport"] = m.group(2).strip()
            pending_tc = None

        if line.startswith("("):      # остальные комментарии движений не несут
            continue
        code = line.split("(")[0].strip()
        if not code:
            continue

        g = re.search(r"\bG(0|1|2|3)\b(?!\d)", code)
        if g:
            mode = int(g.group(1))
        f = re.search(r"\bF(-?[\d.]+)", code)
        if f:
            feed = float(f.group(1))

        nx, ny, nz = x, y, z
        words = {}
        for ax, val in re.findall(r"\b([XYZ])(-?[\d.]+)", code):
            v = float(val)
            words[ax] = v
            if ax == "X":
                nx = v
            elif ax == "Y":
                ny = v
            else:
                nz = v
        if not words or mode is None:
            continue

        # начало программы / первый ход после смены инструмента
        if armed:
            if mode == 0 and set(words) == {"Z"}:
                armed_z = {"line": ln, "z_to": words["Z"]}
            elif armed_z is not None and mode == 0 and {"X", "Y"} <= set(words):
                st["z_then_xy"].append({
                    "op": op, "line": armed_z["line"], "after_toolchange": True,
                    "z_from": None, "z_to": round(armed_z["z_to"], 3),
                    "len_xy": None,
                })
                armed, armed_z = False, None
            else:
                armed, armed_z = False, None

        dx, dy, dz = nx - x, ny - y, nz - z
        if not have_pos:              # первая координата — постановка, не ход
            x, y, z, have_pos = nx, ny, nz, True
            st["z_min"] = st["z_max"] = nz
            continue

        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        flat = math.hypot(dx, dy)
        st["ops"].setdefault(op, {"feed_len": 0.0, "rapid_len": 0.0})

        if mode == 0:
            st["rapid_len"] += dist
            st["rapid_time"] += dist / RAPID_RATE
            st["ops"][op]["rapid_len"] += dist
            # «сначала вниз по Z, потом вбок» — то, на что указал завод:
            # холостой ход поперёк на уже опущенной фрезе.
            if flat < 1e-6 and dz < -1e-6:
                prev_rapid_down = {"line": ln, "z_from": z, "z_to": nz}
            elif flat > 1e-6 and prev_rapid_down is not None:
                st["z_then_xy"].append({
                    "op": op,
                    "line": prev_rapid_down["line"],
                    "z_from": round(prev_rapid_down["z_from"], 3),
                    "z_to": round(prev_rapid_down["z_to"], 3),
                    "len_xy": round(flat, 3),
                })
                prev_rapid_down = None
            else:
                prev_rapid_down = None
        else:
            prev_rapid_down = None
            st["feed_len"] += dist
            if feed > 0:
                st["feed_time"] += dist / feed
            st["ops"][op]["feed_len"] += dist
            # отвесное врезание: рабочая подача, XY стоит, Z вниз
            if flat < 1e-6 and dz < -1e-6:
                in_metal = stock_top is None or nz < stock_top
                st["plunges"].append({
                    "op": op, "line": ln,
                    "z_from": round(z, 3), "z_to": round(nz, 3),
                    "depth": round(-dz, 3), "feed": feed,
                    "in_metal": bool(in_metal),
                })

        x, y, z = nx, ny, nz
        st["z_min"] = nz if st["z_min"] is None else min(st["z_min"], nz)
        st["z_max"] = nz if st["z_max"] is None else max(st["z_max"], nz)

    st["stock_top"] = stock_top
    st["time_min"] = st["feed_time"] + st["rapid_time"]
    st["plunges_in_metal"] = [q for q in st["plunges"] if q["in_metal"]]
    st["no_passport"] = [q["t"] for q in st["tool_changes"] if not q["passport"]]
    return st


def hms(minutes):
    s = int(round(minutes * 60))
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def label(st):
    d = os.path.basename(os.path.dirname(os.path.abspath(st["path"])))
    return d or os.path.basename(st["path"])


def render(stats, md=False):
    rows = [
        ("строк", lambda s: "%d" % s["lines"]),
        ("смен инструмента", lambda s: "%d (%s)" % (
            len(s["tool_changes"]),
            ", ".join("T%d" % q["t"] for q in s["tool_changes"]) or "—")),
        ("без паспорта", lambda s: ", ".join("T%d" % t for t in s["no_passport"])
                                   or "нет"),
        ("рабочий ход, мм", lambda s: "%.0f" % s["feed_len"]),
        ("холостой ход, мм", lambda s: "%.0f" % s["rapid_len"]),
        ("оценка времени", lambda s: hms(s["time_min"])),
        ("отвесных врезаний", lambda s: "%d (в металл %d)" % (
            len(s["plunges"]), len(s["plunges_in_metal"]))),
        ("вниз по Z, потом вбок", lambda s: "%d" % len(s["z_then_xy"])),
    ]
    head = [""] + [label(s) for s in stats]
    body = [[name] + [fn(s) for s in stats] for name, fn in rows]

    if md:
        out = ["| " + " | ".join(head) + " |",
               "|" + "|".join(["---"] * len(head)) + "|"]
        out += ["| " + " | ".join(r) + " |" for r in body]
        return "\n".join(out)

    w = [max([len(head[i])] + [len(r[i]) for r in body])
         for i in range(len(head))]
    out = ["  ".join(h.ljust(w[i]) for i, h in enumerate(head))]
    out.append("  ".join("-" * w[i] for i in range(len(head))))
    out += ["  ".join(c.ljust(w[i]) for i, c in enumerate(r)) for r in body]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", action="store_true", help="выдать словарь целиком")
    ap.add_argument("--md", action="store_true", help="таблица Markdown")
    ap.add_argument("--ops", action="store_true", help="раскладка по операциям")
    ap.add_argument("--list", action="store_true",
                    help="перечислить врезания и «вниз по Z, потом вбок»")
    a = ap.parse_args()

    stats = [parse(f) for f in a.files]
    if a.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        return
    print(render(stats, md=a.md))

    if a.ops:
        for s in stats:
            print("\n" + s["path"])
            for name, v in s["ops"].items():
                if v["feed_len"] or v["rapid_len"]:
                    print("  %-18s рабочий %8.0f  холостой %8.0f"
                          % (name, v["feed_len"], v["rapid_len"]))
    if a.list:
        for s in stats:
            print("\n" + s["path"])
            for q in s["plunges_in_metal"]:
                print("  врезание   стр.%-6d %-16s Z %+8.3f -> %+8.3f  F%g"
                      % (q["line"], q["op"], q["z_from"], q["z_to"], q["feed"]))
            for q in s["z_then_xy"]:
                frm = ("%+8.3f" % q["z_from"]) if q["z_from"] is not None \
                    else "  (смена)"
                print("  Z-затем-XY стр.%-6d %-16s Z %s -> %+8.3f%s"
                      % (q["line"], q["op"], frm, q["z_to"],
                         "  вбок %.1f" % q["len_xy"] if q["len_xy"] else ""))


if __name__ == "__main__":
    main()
