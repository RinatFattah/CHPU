#!/usr/bin/env python3
"""
gcode_shift.py — перенос ПРОГРАММЫ в другую систему координат (сдвиг по осям).

Пара к `cam/step_shift.py`, и путать их не надо:

* `step_shift` двигает ГЕОМЕТРИЮ. Нужен, когда программа чужая и переписывать
  её нельзя — заводскую гоняем в её родной раме, а под неё подкладываем
  сдвинутые деталь и заготовку.
* `gcode_shift` двигает ПРОГРАММУ. Нужен в обратную сторону: наш генератор
  ставит ноль по ВЕРХНЕЙ плоскости детали, а у заказчика он по нижней
  (плоскость стола), и на станок программа должна уехать в его раме.

Сдвиг обычно равен высоте детали и читается из шапки G-кода («Part: … x … x h»).
Двигаются адреса X/Y/Z у ходов и строка `(Stock box: …)`: из неё конвертер в
диалог Heidenhain строит `BLK FORM`. Адреса I/J/K не трогаются — это смещения
центра дуги ОТНОСИТЕЛЬНО её начала, сдвиг рамы их не меняет.

CLI:  python cam/gcode_shift.py вход.gcode dz [выход.gcode]
      python cam/gcode_shift.py вход.gcode "dx dy dz" [выход.gcode]
API:  gcode_shift.shift_text(text, (dx, dy, dz)) -> str
"""

import io
import os
import re
import sys


def shift_text(text, delta):
    """G-код → G-код, сдвинутый на (dx, dy, dz)."""
    dx, dy, dz = (float(v) for v in delta)
    by_axis = {"X": dx, "Y": dy, "Z": dz}
    out = []
    for line in text.splitlines(True):
        m = re.match(r"(\(Stock box:\s*)(.*?)(\)\s*)$", line)
        if m:
            body = re.sub(
                r"\b([XYZ])\s*(-?[\d.]+)\.\.(-?[\d.]+)",
                lambda q: "%s %.1f..%.1f" % (q.group(1),
                                             float(q.group(2)) + by_axis[q.group(1)],
                                             float(q.group(3)) + by_axis[q.group(1)]),
                m.group(2))
            out.append(m.group(1) + body + m.group(3))
            continue
        code, sep, tail = line.partition("(")
        if not code.strip():
            out.append(line)
            continue
        code = re.sub(r"\b([XYZ])(-?[\d.]+)",
                      lambda q: "%s%.3f" % (q.group(1),
                                            float(q.group(2)) + by_axis[q.group(1)]),
                      code)
        out.append(code + sep + tail)
    return "".join(out)


def shift(in_path, delta, out_path=None):
    out_path = out_path or (os.path.splitext(in_path)[0] + "_shifted"
                            + os.path.splitext(in_path)[1])
    text = io.open(in_path, encoding="utf-8", errors="replace").read()
    io.open(out_path, "w", encoding="utf-8", newline="\n").write(
        shift_text(text, delta))
    return out_path


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(sys.argv) < 3:
        print(__doc__.strip())
        sys.exit(1)
    raw = sys.argv[2].replace(",", " ").split()
    delta = (0.0, 0.0, float(raw[0])) if len(raw) == 1 else tuple(raw[:3])
    dst = shift(sys.argv[1], delta,
                sys.argv[3] if len(sys.argv) > 3 else None)
    print("✅ %s  (сдвиг %s)" % (dst, " ".join(str(v) for v in delta)))


if __name__ == "__main__":
    main()
