#!/usr/bin/env python3
"""
nc_glue_heidenhain.py — склейка нескольких программ Heidenhain (.h) в одну.

Зачем: заводской комплект на деталь — это НЕ одна программа, а цепочка переходов
(у 75.6121.0.0411.003 это PR_1_01…PR_1_06), которые оператор запускает подряд по
одной заготовке. NX ISV назначает каналу ОДНУ главную программу, поэтому для
прогона «как на заводе» цепочку надо сшить в один файл.

Что делается со швом (и почему именно так):
  - `BEGIN PGM` / `END PGM` промежуточных программ убираются: в файле Heidenhain
    они допустимы ровно по одному разу, по краям;
  - промежуточные `M30` убираются — стойка на них закончила бы прогон на первом
    же переходе; финальный `M30` ставится один;
  - `M9 M5` на стыке СОХРАНЯЕТСЯ, как и `TOOL CALL` + `M3` в начале следующего
    перехода: так шпиндель останавливается и заново раскручивается ровно там же,
    где на заводе между запусками. Это ближе к оригиналу, чем «сшить встык»;
  - блоки перенумеровываются сквозной нумерацией с 1 — номер блока у Heidenhain
    обязан расти, а у каждого исходника он начинается заново;
  - шапки `; PART:` / `; DATE:` промежуточных файлов выбрасываются, а строка с
    именем перехода (`; NK_1_01`) остаётся: по ней видно, что сейчас исполняется.

`BLK FORM`: у каждого перехода он СВОЙ и реальной заготовкой не является — это
рамка для графики стойки, выданная постпроцессором (у PR_1_01 это вообще полоса
Z 0…+1). Поэтому по умолчанию все они выбрасываются, а вместо них пишется один
`--blk` по настоящей заготовке. `--keep-blk` оставляет `BLK FORM` первого файла.

Координаты НЕ ТРОГАЮТСЯ: программа остаётся в своей системе координат.

Использование:
  python nx/nc_glue_heidenhain.py out.h in1.h in2.h ... [--name PGM] \
         [--blk "x0 y0 z0 x1 y1 z1"] [--keep-blk]
"""
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_NUM = re.compile(r"^\s*(\d+)\s?(.*)$")      # «14 TOOL CALL 1 …» → номер + тело
_BEGIN = re.compile(r"^BEGIN\s+PGM\b", re.I)
_END = re.compile(r"^END\s+PGM\b", re.I)
_BLK = re.compile(r"^BLK\s+FORM\b", re.I)
_M30 = re.compile(r"^M30\b", re.I)
# служебные строки шапки постпроцессора — на прогон не влияют, только шумят
_HEAD = re.compile(r"^;\s*(PART|GENERATE BY|MACHINE|DATE|PROGRAMMER|TIME=|DIST=)", re.I)


def strip_number(line: str) -> str:
    """«14 TOOL CALL 1 Z DR+0.0» → «TOOL CALL 1 Z DR+0.0»."""
    m = _NUM.match(line)
    return m.group(2).rstrip() if m else line.strip()


def body_of(path: str) -> tuple[list[str], str, list[str]]:
    """Возвращает (строки тела без номеров, имя программы, выброшенные BLK FORM)."""
    name, out, blk = os.path.splitext(os.path.basename(path))[0], [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = strip_number(raw)
            if not s:
                continue
            if _BEGIN.match(s):
                m = re.match(r"BEGIN\s+PGM\s+(\S+)", s, re.I)
                if m:
                    name = m.group(1)
                continue
            if _END.match(s) or _M30.match(s):
                continue
            if _BLK.match(s):
                blk.append(s)
                continue
            if _HEAD.match(s):
                continue
            out.append(s)
    return out, name, blk


def glue(paths: list[str], pgm_name: str = "PR_1_ALL",
         blk: tuple[float, ...] | None = None, keep_blk: bool = False) -> list[str]:
    # комментарии — только ASCII: файл уходит на стойку, кириллица там мусор
    out = [f"BEGIN PGM {pgm_name} MM",
           f"; GLUED FROM {len(paths)}: "
           + ", ".join(os.path.splitext(os.path.basename(p))[0] for p in paths),
           "; coordinates untouched"]
    first_blk = None
    chunks = []
    for p in paths:
        body, name, blks = body_of(p)
        if first_blk is None and blks:
            first_blk = blks
        chunks.append((name, body))
    if keep_blk and first_blk:
        out += first_blk
    elif blk:
        x0, y0, z0, x1, y1, z1 = blk
        out += ["; BLK FORM replaced by real stock box",
                f"BLK FORM 0.1 Z X{x0:g} Y{y0:g} Z{z0:g}",
                f"BLK FORM 0.2 X{x1:g} Y{y1:g} Z{z1:g}"]
    for i, (name, body) in enumerate(chunks):
        out.append(f"; ===== OP {i + 1}/{len(chunks)}: {name} =====")
        out += body
    # последний переход уже кончается своим «M9 M5» — второй не нужен
    if not out or out[-1].upper().replace(" ", "") != "M9M5":
        out.append("M9 M5")
    out += ["M30", f"END PGM {pgm_name} MM"]
    return [f"{i} {s}" for i, s in enumerate(out, 1)]


def main():
    args = sys.argv[1:]
    name, blk, keep_blk, rest = "PR_1_ALL", None, False, []
    i = 0
    while i < len(args):
        if args[i] == "--name":
            name = args[i + 1]; i += 2
        elif args[i] == "--blk":
            blk = tuple(float(v) for v in args[i + 1].replace(",", " ").split()); i += 2
        elif args[i] == "--keep-blk":
            keep_blk = True; i += 1
        else:
            rest.append(args[i]); i += 1
    if len(rest) < 2:
        print(__doc__.strip().splitlines()[-3])
        print("usage: nc_glue_heidenhain.py out.h in1.h in2.h ... "
              "[--name PGM] [--blk \"x0 y0 z0 x1 y1 z1\"] [--keep-blk]")
        sys.exit(1)
    if blk is not None and len(blk) != 6:
        print("--blk хочет ровно 6 чисел: x0 y0 z0 x1 y1 z1")
        sys.exit(1)
    dst, srcs = rest[0], rest[1:]
    lines = glue(srcs, pgm_name=name, blk=blk, keep_blk=keep_blk)
    with open(dst, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"OK: {len(srcs)} программ -> {dst} ({len(lines)} блоков)")
    for p in srcs:
        b, n, _ = body_of(p)
        print(f"  {n}: {len(b)} блоков")


if __name__ == "__main__":
    main()
