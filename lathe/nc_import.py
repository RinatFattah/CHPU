#!/usr/bin/env python3
"""
nc_import.py — ЧУЖАЯ токарная программа → наш диалект, чтобы прогнать её нашим
симулятором съёма.

Зачем. Главное возражение к нашим цифрам: симулятор и генератор писались вместе,
и общая для обоих ошибка так не ловится. Возражение снимается, если прогнать
через симулятор программу, которую писали НЕ мы: заводскую `UST1.NC` для
14-31A делали люди в NX CAM, постпроцессором CLIO SOFT, гоняли в заводском
Vericut, и по ней реально точат детали. Если наш симулятор на ней НЕ показывает
зареза — он проверен против реальности. Если показывает — врёт симулятор, и это
надо знать.

Что делает конвертер:

  * убирает комментарии в квадратных скобках (у CNC PILOT они такие);
  * **раскрывает МОДАЛЬНЫЕ G-коды.** В заводской программе 268 строк вида
    `X-.8` — без G-слова, продолжают предыдущее G01. Наш разборщик требует
    G-слово в начале строки и такие строки просто пропустил бы, то есть съём
    вышел бы вчетверо меньше настоящего;
  * нормализует числа `-.8` → `-0.8` (наши регулярки требуют цифру до точки);
  * расставляет метки операций `(Begin operation: ...)` и шапки инструментов,
    по которым симулятор отличает НАРУЖНУЮ обработку от работы ВНУТРИ
    отверстия. У завода таких меток нет — соответствие «инструмент → что он
    делает» задаётся явно, из списка инструментов в шапке программы.

Резьбовой инструмент по умолчанию ПРОПУСКАЕТСЯ: в CAD-модели резьбы нет
(гладкий цилиндр наружного диаметра), и её нарезание неизбежно выглядело бы
зарезом у кого угодно — и у нас, и у завода. Сравнение должно быть честным.

Пример:

    python lathe/nc_import.py UST1.NC out_factory.gcode \\
           --drill T4=3.15 --drill T6=10 --bore T8 --skip T3
"""

import argparse
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# G-слова, которые НЕ являются перемещением: их нельзя делать модальными,
# иначе следующая строка с координатой унаследует не тот режим.
_NON_MOTION = {"4", "04", "14", "18", "20", "40", "50", "80", "94", "95", "97"}


def _fix_num(text):
    """`X-.8` → `X-0.8`, `Z.5` → `Z0.5` — наши регулярки требуют цифру до точки."""
    return re.sub(r"(?<=[XZIKR])(-?)\.(\d)", r"\g<1>0.\2", text)


def convert(src, dst, drills=None, bores=(), skips=(), encoding="cp1251"):
    """Читает чужую программу, пишет наш диалект. Возвращает статистику."""
    drills = dict(drills or {})
    bores = {t.upper() for t in bores}
    skips = {t.upper() for t in skips}

    # Преамбула стойки. Нашему симулятору она безразлична — он читает только
    # G0/G1/G2/G3, — но на настоящем контроллере без неё программа не поедет:
    # без DIAMON X читается радиусом (координаты вдвое, отказ по лимитам осей),
    # без G95 у G01 нет режима подачи. В исходнике эти команды есть, но идут
    # строками без координат, и ранняя версия конвертера их отбрасывала — из-за
    # чего прогон в NX ISV вставал на первых секундах.
    out = ["(Imported from %s)" % os.path.basename(src),
           "G18 G90", "G54", "DIAMON", "G95"]
    tool = None
    op_no = 0
    modal = None
    stats = {"tools": [], "motion": 0, "skipped": 0}

    with open(src, encoding=encoding, errors="replace") as f:
        raw_lines = f.read().splitlines()

    for raw in raw_lines:
        line = re.sub(r"^N\d+\s*", "", raw).strip()
        m = re.match(r"T(\d+)\.\d+\s*\[TOOL\s*([^\s\]]*)", line)
        if m:
            tool = f"T{int(m.group(1))}"
            op_no += 1
            name = m.group(2) or tool
            out.append("")
            if tool in skips:
                # Пропущенный инструмент НЕ вызываем вовсе: строка «T<n>» без
                # созданного в станке инструмента останавливает стойку на
                # пустой станции револьвера, и прогон в NX ISV обрывается.
                out.append(f"(Tool {tool}: {name} — SKIPPED by import)")
                stats["tools"].append(tool)
                modal = None
                continue
            out.append(tool)
            # M6 обязателен: на стойке Sinumerik «T<n>» без M6 инструмент в
            # шпиндель не ставит (проверено перебором преамбул). Нашему
            # симулятору эта строка безразлична, а для прогона в NX ISV нужна.
            out.append("M6")
            if tool in drills:
                kind = ("center drill" if drills[tool] < 4.0 else "twist drill")
                out.append(f"(Tool {tool}: {kind} D{drills[tool]:.2f} mm)")
                out.append(f"(Begin operation: Drill{op_no})")
            elif tool in bores:
                out.append(f"(Tool {tool}: boring bar)")
                out.append(f"(Begin operation: Bore{op_no})")
            else:
                out.append(f"(Tool {tool}: {name})")
                out.append(f"(Begin operation: Turn{op_no})")
            stats["tools"].append(tool)
            modal = None
            continue

        if not line or line.startswith("[") or line.startswith("%"):
            continue
        line = _fix_num(line)

        mg = re.match(r"G(\d+)", line)
        if mg:
            code = mg.group(1)
            if code not in _NON_MOTION:
                modal = code
        has_xz = re.search(r"[XZ]-?\d", line)
        if not has_xz:
            # Строки без координат — не движение, но среди них ОБОРОТЫ и РЕЖИМ
            # ПОДАЧИ (`G97 S2000 M04`, `G95 F.15`). Их надо пронести: без них
            # шпиндель стоит и у рабочих ходов нет подачи. Всё остальное
            # (G14 возврат в точку смены, M15/M108/M109 — команды CNC PILOT,
            # которых Sinumerik не знает) отбрасываем.
            if tool in skips:
                continue
            keep = re.match(r"G9[4567]\b", line) or re.search(r"\b[SF][\d.]", line)
            if keep:
                kept = " ".join(w for w in line.split()
                                if not re.match(r"M(?!0?[345]\b)\d+$", w))
                out.append(kept)
            continue
        if tool in skips:
            stats["skipped"] += 1
            continue
        if modal is None:
            continue
        # G-слово печатаем ЯВНО на каждой строке с координатой
        body = re.sub(r"^G\d+\s*", "", line)
        body = re.sub(r"\bM\d+\b", "", body)
        # Ось Y. Заводская программа её пишет (`G00 X100. Y0`), у 2-осевого
        # токарного станка её нет, и стойка на такой строке встаёт.
        body = re.sub(r"\bY-?[\d.]+", "", body).strip()
        out.append(f"G{int(modal):02d} {body}".strip())
        stats["motion"] += 1

    out.append("M30")
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="чужая программа (.NC)")
    ap.add_argument("dst", help="куда писать наш диалект")
    ap.add_argument("--drill", action="append", default=[], metavar="T4=3.15",
                    help="инструмент — сверло указанного диаметра (можно несколько)")
    ap.add_argument("--bore", action="append", default=[], metavar="T8",
                    help="инструмент работает ВНУТРИ отверстия (расточной)")
    ap.add_argument("--skip", action="append", default=[], metavar="T3",
                    help="инструмент пропустить (например резьбовой: резьбы нет "
                         "в CAD-модели, её нарезание выглядело бы зарезом)")
    ap.add_argument("--encoding", default="cp1251")
    args = ap.parse_args()

    drills = {}
    for item in args.drill:
        t, _, d = item.partition("=")
        drills[t.strip().upper()] = float(d)

    st = convert(args.src, args.dst, drills, args.bore, args.skip, args.encoding)
    print(f"✅ {args.src} → {args.dst}")
    print(f"   инструментов {len(st['tools'])}: {', '.join(st['tools'])}")
    print(f"   рабочих и холостых перемещений: {st['motion']}"
          + (f", пропущено {st['skipped']}" if st["skipped"] else ""))


if __name__ == "__main__":
    main()
