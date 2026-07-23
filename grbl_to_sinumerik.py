#!/usr/bin/env python3
"""
grbl_to_sinumerik.py — конвертер G-кода GRBL (FreeCAD grbl_post) → Sinumerik .mpf
для симуляции в NX (ISV, пример станка sim01_mill_3ax, стойка Sinumerik).

Три правки (по гайду «NX_симуляция_G-кода», Часть 4):
  1. (...)-комментарии удалить — Sinumerik трактует скобки как выражение → Parse error.
  2. Смену инструмента сделать активной и в порядке T<n> ↵ M6.
     grbl_post пишет её комментарием «( M6 T1 )» (GRBL одноинструментальный);
     Sinumerik-симуляции нужна живая команда, иначе «Tool 1 not defined» / нет резки.
  3. G21 удалить — это код RS274/GRBL; Sinumerik единицы задаёт иначе (G70/G71),
     G21 для него неизвестен.

Всё остальное (G17/G90/G54, M3 S…, G0/G1/G2/G3 X Y Z F, M5, M2) — как есть.

Использование:
  python grbl_to_sinumerik.py in.nc [out.mpf]
  # out по умолчанию — рядом с in, с расширением .mpf
"""
import os
import re
import sys

# Windows: консоль по умолчанию cp1251 — печать не-cp1251 символов (⚠, …) иначе
# падает с UnicodeEncodeError. Переключаем на UTF-8 (на Linux уже UTF-8 — no-op).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_TNUM = re.compile(r"\bT(\d+)\b")      # номер инструмента: T1, T2, …
_M6 = re.compile(r"\bM0?6\b")          # смена инструмента: M6 или M06
_G21 = re.compile(r"\bG21\b")          # «единицы = мм» (GRBL)
_PARENS = re.compile(r"\([^)]*\)")     # (комментарий) в круглых скобках


def convert(text: str) -> tuple[str, int]:
    """GRBL-текст → Sinumerik-текст. Возвращает (результат, число смен инструмента)."""
    out = []
    toolchanges = 0

    def emit_toolchange(line):
        nonlocal toolchanges
        out.append(f"T{_TNUM.search(line).group(1)}")   # (2) сначала выбор инструмента,
        out.append("M6")                                #     потом смена — порядок важен
        toolchanges += 1

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        # (1) комментарий: в grbl_post это всегда ЦЕЛАЯ строка в (...), м.б. с вложенными
        #     скобками — поэтому просто отбрасываем строку целиком (а не режем regex'ом).
        if s.startswith("("):
            if _M6.search(s) and _TNUM.search(s):       # закомментированная смена «( M6 T1 )»
                emit_toolchange(s)
            continue                                    # прочие комментарии — убрать
        s = _PARENS.sub("", s).strip()                  # на всякий случай — хвостовой inline (...)
        if _M6.search(s) and _TNUM.search(s):           # активная смена «M6 T1» / «T1 M6»
            emit_toolchange(s)
            continue
        # (3) убрать G21 (в т.ч. если он в строке с другими кодами)
        s = _G21.sub("", s).strip()
        if not s:
            continue
        out.append(s)
    return "\r\n".join(out) + "\r\n", toolchanges


def main():
    if len(sys.argv) < 2:
        print("usage: python grbl_to_sinumerik.py in.nc [out.mpf]")
        sys.exit(1)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + ".mpf"
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

    with open(src, encoding="utf-8") as f:
        text = f.read()
    result, tc = convert(text)
    # newline="" — не даём Python трогать наши \r\n
    with open(dst, "w", encoding="utf-8", newline="") as f:
        f.write(result)

    n_in = text.count("\n") + 1
    n_out = result.count("\r\n")
    # Смена инструмента может быть уже активной в исходнике (T<n>/M6) или преобразованной
    # из комментария (tc>0). Предупреждаем, только если её нет в итоговой программе.
    lines = result.split("\r\n")
    has_tc = (any(re.fullmatch(r"T\d+", l) for l in lines)
              and any(re.fullmatch(r"M0?6", l) for l in lines))
    print(f"OK: {src} ({n_in} строк) -> {dst} ({n_out} строк)")
    print(f"  смена инструмента T<n>/M6: {'есть' if has_tc else 'НЕТ'}"
          + (f" (преобразовано из комментария: {tc})" if tc else ""))
    if not has_tc:
        print("  ⚠ в программе нет T<n>/M6 — стойка не загрузит инструмент, проверь исходник")


if __name__ == "__main__":
    main()
