#!/usr/bin/env python3
"""
grbl_to_heidenhain.py — наш G-код (grbl из FreeCAD) → диалог Heidenhain (.h).

Второй мост после `grbl_to_sinumerik.py`. Нужен для станка DMC 635V со стойкой
iTNC 530: у КнААЗ на нём сделаны детали 75.6121.0.0411.*, и заводские программы
к ним лежат рядом — по ним и сверялся диалект.

ЧТО ВЫЯСНЕНО ПО ЗАВОДСКИМ ПРОГРАММАМ (6 штук, 1916 строк, деталь ...003):
диалект крошечный, одиннадцать конструкций, и НИ ОДНОГО постоянного цикла:

    BEGIN PGM <имя> MM
    BLK FORM 0.1 Z X.. Y.. Z..   заготовка для графики стойки
    BLK FORM 0.2 X.. Y.. Z..
    TOOL CALL <n> Z DR+0.0       инструмент; длина и радиус — из таблицы СТАНКА
    TOOL CALL S<обороты>
    M3 / M9 / M30
    L X.. Y.. Z.. F..  |  FMAX   линейное (FMAX = ускоренное)
    CC X.. Y..                   центр дуги
    C  X.. Y.. DR±               дуга в этот центр: DR- по часовой, DR+ против
    END PGM <имя> MM

Правила записи, снятые оттуда же:
  * блоки нумеруются подряд с 1;
  * в блоке пишутся ТОЛЬКО ИЗМЕНИВШИЕСЯ адреса, подача модальна;
  * числа без хвостовых нулей, целое пишется с точкой («Z41.»), ноль — «X0».

ЧЕГО ЭТОТ ПЕРЕВОД НЕ ДЕЛАЕТ (и никакой другой не сделал бы):
  * НЕ задаёт ноль детали — в заводских программах его нет вовсе, привязку
    ставит наладчик в таблице преднастроек стойки;
  * НЕ задаёт геометрию инструмента — `TOOL CALL 1` берёт длину и радиус из
    таблицы СТАНКА, в программе только номер и дельта радиуса.
Оба пункта — наладка, а не программа. Перед резом их проверяет технолог.

    python nx/grbl_to_heidenhain.py вход.gcode [выход.h] [--name PR_1_01]
"""

import argparse
import math
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

_NUM = r"(-?\d+(?:\.\d*)?)"
_STOCK = re.compile(r"Stock box:\s*X\s*" + _NUM + r"\.\." + _NUM +
                    r"\s*Y\s*" + _NUM + r"\.\." + _NUM +
                    r"\s*Z\s*" + _NUM + r"\.\." + _NUM)
_TOOLCH = re.compile(r"\(\s*M6\s+T(\d+)\s*\)")
_SPINDLE = re.compile(r"spindle\s+(\d+)\s*rpm", re.I)


def h(v):
    """Координата в стиле Heidenhain: без хвостовых нулей, целое — с точкой."""
    s = f"{v:.3f}".rstrip("0")
    if s.endswith("."):
        s = s if float(v) else "0"      # «41.» но «0», как у завода
    return s if s not in ("-0", "-0.") else "0"


def hf(v):
    """Подача: у завода целая и БЕЗ точки («F2000»), в отличие от координат."""
    return str(int(round(v))) if abs(v - round(v)) < 1e-6 else f"{v:.1f}"


_PART = re.compile(r"\(Part:\s*[\d.]+\s*x\s*[\d.]+\s*x\s*([\d.]+)\s*mm")


def z_shift_for(src_lines, mode):
    """Сдвиг по Z под НОЛЬ ЗАКАЗЧИКА. Возвращает (сдвиг, пояснение).

    Наш генератор ставит ноль по ВЕРХУ детали (`ORIGIN = corner-top`), КнААЗ —
    по НИЗУ, то есть по плоскости вакуумного стола. Прочитано с их карты
    эскизов КЭ №2: ось Z построена от нижней плоскости вверх, от неё отложены
    высота детали 21 и высота заготовки 45.

    Подтверждено их же программами: у всех шести ход по Z лежит целиком в
    плюсе (+1.312…48.000), у наших — в минусе (−20.516…28.984). По X диапазоны
    совпадают, по Y совпадают центры, так что расходится ТОЛЬКО Z.

    Сдвиг равен высоте детали и берётся из шапки G-кода, а не задаётся руками:
    у каждой детали она своя.
    """
    if mode != "bottom":
        return 0.0, ""
    for ln in src_lines:
        m = _PART.search(ln)
        if m:
            h = float(m.group(1))
            return h, f"ноль перенесён на низ детали: Z += {h:g} мм"
    return 0.0, ("в G-коде нет строки «(Part: …)» — высоту детали взять неоткуда, "
                 "ноль ОСТАВЛЕН по верху")


def convert(src_lines, name="PROG", tol=0.01, z_datum="top"):
    """Строки grbl → строки диалога Heidenhain (без номеров блоков).

    `tol` — допуск линеаризации дуг, мм. 0.01 выбрано по замеру: вырожденные
    дуги FreeCAD в программе ...003 отходят от хорды на 7 мкм, то есть при
    пороге 5 мкм формально остаются «точнее прямой» и проходят в стойку дугами
    радиусом 15 метров. Для фрезеровки с допусками в сотые это лишняя экзотика.
    """
    out, warn = [], []
    stock = None
    rpm = None
    dz, note = z_shift_for(src_lines, z_datum)
    if note:
        warn.append(note)
    for ln in src_lines:                       # шапка живёт в комментариях
        m = _STOCK.search(ln)
        if m:
            stock = [float(x) for x in m.groups()]
        m = _SPINDLE.search(ln)
        if m and rpm is None:
            rpm = int(m.group(1))
    if stock:
        stock[4] += dz                          # заготовка едет вместе с нулём
        stock[5] += dz

    out.append(f"BEGIN PGM {name} MM")
    if stock:
        x0, x1, y0, y1, z0, z1 = stock
        out.append(f"BLK FORM 0.1 Z X{h(x0)} Y{h(y0)} Z{h(z0)}")
        out.append(f"BLK FORM 0.2 X{h(x1)} Y{h(y1)} Z{h(z1)}")
    else:
        warn.append("в G-коде нет строки «Stock box:» — BLK FORM не записан")

    pos = {"X": None, "Y": None, "Z": None}
    feed = None
    started = False

    for raw in src_lines:
        ln = raw.strip()
        m = _TOOLCH.search(ln)
        if m:
            out.append(f"TOOL CALL {int(m.group(1))} Z DR+0.0")
            if rpm:
                out.append(f"TOOL CALL S{rpm}")
            continue
        if ln.startswith("(") or not ln:
            continue
        g = re.match(r"G(\d+)", ln)
        code = int(g.group(1)) if g else None
        if code in (0, 1, 2, 3):
            words = dict(re.findall(r"([XYZIJF])" + _NUM, ln))
            tgt = {a: float(words[a]) + (dz if a == "Z" else 0.0)
                   if a in words else pos[a]
                   for a in "XYZ"}
            if code in (2, 3):
                if pos["X"] is None or pos["Y"] is None:
                    warn.append("дуга до первого линейного хода — пропущена")
                    continue
                cx = pos["X"] + float(words.get("I", 0.0))
                cy = pos["Y"] + float(words.get("J", 0.0))
                # ДВА СЛУЧАЯ, КОГДА ДУГУ НЕЛЬЗЯ ПИСАТЬ БЛОКОМ `C`.
                #
                # 1. Винтовая (Z меняется по дуге). У Heidenhain `C` — дуга В
                #    ПЛОСКОСТИ, подъём по ней задаётся иначе; записав только XY,
                #    мы бы МОЛЧА ПОТЕРЯЛИ ход по Z. В программе ...003 таких нет
                #    ни одной, но проверка обязана быть, иначе однажды потеряем.
                # 2. Вырожденная — радиус много больше детали. FreeCAD пишет
                #    почти прямые участки дугами: в той же программе 45 дуг из
                #    315 имеют радиус свыше метра, максимум 15 метров. Стойке
                #    такое лучше не давать.
                #
                # Оба случая линеаризуются: считаем стрелку прогиба и режем дугу
                # на столько отрезков, чтобы она нигде не отходила от хорды
                # дальше tol. У вырожденной выходит ровно один отрезок.
                rad = math.hypot(pos["X"] - cx, pos["Y"] - cy)
                # ИМЯ ВАЖНО: `dz` снаружи — это сдвиг нуля детали. Одноимённая
                # локальная переменная затирала его после первой же дуги, и
                # дальше программа шла без переноса нуля.
                arc_dz = 0.0 if tgt["Z"] is None or pos["Z"] is None \
                    else tgt["Z"] - pos["Z"]
                a0 = math.atan2(pos["Y"] - cy, pos["X"] - cx)
                a1 = math.atan2(tgt["Y"] - cy, tgt["X"] - cx)
                sweep = a1 - a0
                if code == 2:                       # по часовой
                    while sweep >= 0:
                        sweep -= 2 * math.pi
                else:
                    while sweep <= 0:
                        sweep += 2 * math.pi
                sag = rad * (1 - math.cos(abs(sweep) / 2))   # стрелка прогиба
                if abs(arc_dz) > 1e-6 or sag < tol:
                    n = 1 if sag < tol else max(
                        2, int(math.ceil(abs(sweep) /
                                         (2 * math.acos(max(-1.0, min(
                                             1.0, 1 - tol / rad)))))))
                    if abs(arc_dz) > 1e-6:
                        warn.append("винтовая дуга разложена на отрезки "
                                    "(блок `C` подъём по Z не несёт)")
                    for k in range(1, n + 1):
                        t = k / n
                        ang = a0 + sweep * t
                        px, py = cx + rad * math.cos(ang), cy + rad * math.sin(
                            ang)
                        pz = None if pos["Z"] is None else pos["Z"] + arc_dz * t
                        seg = {"X": px, "Y": py, "Z": pz}
                        if k == n:
                            seg = tgt                # конец — точно как в G-коде
                        b = " ".join(f"{a}{h(seg[a])}" for a in "XYZ"
                                     if seg[a] is not None and seg[a] != pos[a])
                        if b:
                            f = float(words["F"]) if "F" in words else feed
                            tail = ""
                            if f is not None and f != feed:
                                tail, feed = f" F{hf(f)}", f
                            out.append(f"L {b}{tail}")
                        pos = dict(seg)
                else:
                    out.append(f"CC X{h(cx)} Y{h(cy)}")
                    body = " ".join(f"{a}{h(tgt[a])}" for a in "XY"
                                    if tgt[a] != pos[a])
                    out.append(f"C {body} DR{'-' if code == 2 else '+'}")
            else:
                body = " ".join(f"{a}{h(tgt[a])}" for a in "XYZ"
                                if tgt[a] is not None and tgt[a] != pos[a])
                if not body:
                    continue                    # холостой повтор
                if code == 0:
                    out.append(f"L {body} FMAX")
                    feed = None                 # FMAX сбрасывает модальность
                else:
                    f = float(words["F"]) if "F" in words else feed
                    tail = ""
                    if f is not None and f != feed:
                        tail, feed = f" F{hf(f)}", f
                    out.append(f"L {body}{tail}")
            pos = tgt
            started = True
            continue
        m = re.match(r"M(\d+)", ln)
        if m:
            n = int(m.group(1))
            if n in (3, 4, 5, 8, 9):
                out.append(f"M{n}")
            elif n in (2, 30):
                out.append("M30")
        # G17/G21/G54/G90 в диалог не переносятся: плоскость и единицы заданы
        # заголовком PGM ... MM, ноль детали ставит наладчик (см. шапку файла)

    if not started:
        warn.append("в программе не нашлось ни одного хода")
    if out[-1] != "M30":
        out.append("M30")
    out.append(f"END PGM {name} MM")
    return out, warn


def main():
    ap = argparse.ArgumentParser(
        description="G-код grbl → диалог Heidenhain (.h) для iTNC 530")
    ap.add_argument("src", help="входной .gcode (grbl из FreeCAD)")
    ap.add_argument("dst", nargs="?", help="выходной .h")
    ap.add_argument("--name", help="имя программы (по умолчанию — имя файла)")
    ap.add_argument("--tol", type=float, default=0.01, metavar="MM",
                    help="допуск линеаризации дуг, мм (дефолт 0.01)")
    ap.add_argument("--z-datum", choices=("top", "bottom"), default="top",
                    help="где ноль по Z: top — по верху детали, как считает наш "
                         "генератор (дефолт); bottom — по низу, как на КнААЗ "
                         "(сдвиг = высота детали из шапки G-кода)")
    a = ap.parse_args()

    dst = a.dst or os.path.splitext(a.src)[0] + ".h"
    # Имя программы у стойки — идентификатор, а не путь: только буквы, цифры и
    # подчёркивание, иначе стойка отвергает файл.
    name = a.name or re.sub(r"\W", "_",
                            os.path.splitext(os.path.basename(dst))[0])[:16]

    with open(a.src, encoding="utf-8", errors="replace") as f:
        src = f.read().splitlines()
    body, warn = convert(src, name, a.tol, a.z_datum)

    parent = os.path.dirname(os.path.abspath(dst))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="\r\n") as f:
        for i, ln in enumerate(body, 1):
            f.write(f"{i} {ln}\n")

    print(f"✅ {a.src}\n   → {dst}: {len(body)} блоков, программа {name}")
    for w in dict.fromkeys(warn):
        print(f"   ⚠  {w}")


if __name__ == "__main__":
    main()
