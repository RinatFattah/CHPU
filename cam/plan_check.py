# -*- coding: utf-8 -*-
"""Проверка техплана — детерминированная, до того как строится траектория.

Смысл в разделении ролей: решения принимает технолог (сейчас — код, позже —
агент), а проверяет их отдельный слой по нормативам. Поэтому валидатор ничего
не знает про то, КАК план получился, и работает на любом плане, включая
написанный руками.

    python cam/plan_check.py runs/1/out_plan.json [ещё планы...]

Коды возврата: 0 — отказов нет, 1 — есть хотя бы один отказ.

Строгость различается по тому, ЧЕМ проверка располагает:

* **отказ** — нарушение видно по самому плану (фича без перехода, слой больше
  диаметра, превышен предел при ТОЧНО известных t и B);
* **предупреждение** — нарушение видно только по ОЦЕНКЕ СВЕРХУ. Если по оценке
  сверху нарушения нет, его нет и на самом деле; если есть — надо смотреть, а
  не запрещать.

Это не педантизм: `t` для контурного прохода из плана не выводится — сколько
металла стоит сбоку, знает модель снятого материала, а не план.
"""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cam import norms  # noqa: E402

# Стратегия → как оценить радиальную глубину `t` (мм) по записи перехода.
# Второе значение: точная величина или оценка сверху.
EXACT, UPPER = "точно", "оценка сверху"


def tb_of(tr):
    """(t, B, основание) для перехода. B — слой по оси, t — радиальный съём.

    Соглашение ОН-1980: `B` меряется ВДОЛЬ оси фрезы, `t` — поперёк. Для
    концевой фрезы это значит, что слой по Z — это `B`, а шаг строчек или
    ширина захвата — `t`. Перепутать их местами легко, и на пазе перепутанные
    оси дают ложное нарушение предела.
    """
    d = tr.get("инструмент_Ø")
    B = tr.get("слой")
    so = tr.get("шаг_строчек_%")
    strat = tr.get("стратегия", "")
    if not d or not B:
        return None, None, None
    if so:                                  # выборка и 3D-проход: шаг строчек в %
        return round(d * float(so) / 100.0, 3), B, EXACT
    # Проход по средней линии и контурный обход: сколько металла стоит сбоку,
    # знает модель снятого материала, а не план. На 003 полоса объёма шириной
    # 2.4 мм даёт настоящее t = 2.4 при диаметре 12 — впятеро меньше оценки.
    return d, B, UPPER


def check(plan, name=""):
    """→ (отказы, предупреждения, заметки). Каждый элемент — строка."""
    bad, warn, note = [], [], []
    vin = plan.get("вход", {})
    feats = plan.get("фичи", [])
    trs = plan.get("переходы", [])
    order = plan.get("порядок", [])

    # ── структура ──────────────────────────────────────────────────────────
    if plan.get("версия") is None:
        bad.append("нет поля «версия»")
    if not trs:
        bad.append("в плане нет ни одного перехода")
    if len(order) != len(trs):
        bad.append(f"порядок ({len(order)}) не совпадает с числом переходов "
                   f"({len(trs)})")

    # ── покрытие ───────────────────────────────────────────────────────────
    covered = {t.get("фича") for t in trs}
    for f in feats:
        if f["id"] not in covered:
            bad.append(f"фича {f['id']} ({f.get('класс')}) не покрыта ни одним "
                       f"переходом")
    for t in trs:
        if not t.get("фича"):
            bad.append(f"переход {t['id']} не привязан к фиче")

    # ── материал ───────────────────────────────────────────────────────────
    group = norms.material_group(vin.get("материал"))
    if group is None:
        note.append("группа материала неизвестна — предел t×B не проверяется "
                    f"(в плане: {vin.get('материал') or 'не задан'})")

    # Материал режущей части — из записи инструмента в плане. Без него берётся
    # быстрорез, и это надо видеть: у алюминия другой колонки нет вовсе.
    tool_mat = {}
    for it in (vin.get("инструмент") or []):
        try:
            tool_mat[round(float(it.get("Ø")), 3)] = it.get("мат")
        except (TypeError, ValueError):
            pass

    by_id = {f["id"]: f for f in feats}
    for t in trs:
        tid = t["id"]
        d = t.get("инструмент_Ø")

        # ── слой ───────────────────────────────────────────────────────────
        B = t.get("слой")
        if B is not None and d and B > d + 1e-9:
            bad.append(f"{tid}: слой {B} мм больше диаметра фрезы {d} мм")
        if B is not None and B <= 0:
            bad.append(f"{tid}: слой {B} мм")
        # Слой крупнее всей высоты среза. На исполнение не влияет — FreeCAD
        # обрежет до одного хода, — но в плане это ложь о том, как режется
        # переход, и по ней нельзя ни сверять, ни рассуждать. Нашёл агент
        # (`47_claude_cli`) на FinishFace1: слой 1.5 при высоте 0.5.
        z1, z2 = t.get("Z_от"), t.get("Z_до")
        if B and z1 is not None and z2 is not None and B > (z1 - z2) + 1e-9:
            warn.append(f"{tid}: слой {B} мм больше всей высоты среза "
                        f"{round(z1 - z2, 3)} мм")

        # ── фреза влезает в фичу ───────────────────────────────────────────
        f = by_id.get(t.get("фича")) or {}
        w = f.get("наименьшая_ширина")
        if w and d and d > w + 1e-6:
            bad.append(f"{tid}: фреза Ø{d} не входит в фичу {f['id']} "
                       f"шириной {w} мм")

        # ── предел t×B ─────────────────────────────────────────────────────
        if group:
            tt, BB, basis = tb_of(t)
            if tt is None:
                note.append(f"{tid}: t и B из плана не выводятся")
            else:
                mat = tool_mat.get(round(float(d), 3)) or "быстрорез"
                lim = norms.tb_limit(d, group, mat)
                if lim is None:
                    note.append(f"{tid}: таблица не покрывает сочетание "
                                f"Ø{d} / {group} / {mat} — предел не проверяется")
                elif tt * BB > lim + 1e-9:
                    msg = (f"{tid}: t×B = {tt * BB:.1f} мм² больше предела "
                           f"{lim} ({group}, Ø{d})")
                    (bad if basis == EXACT else warn).append(
                        msg + ("" if basis == EXACT else f" — {basis}"))

        # ── грунтинг ───────────────────────────────────────────────────────
        if not t.get("обоснование"):
            warn.append(f"{tid}: пустое обоснование")

    for u in plan.get("невыполнено", []):
        note.append(f"шаг {u.get('шаг')} не выполнен: {u.get('причина')}"
                    + (f" → {u['принято_по_умолчанию']}"
                       if u.get("принято_по_умолчанию") else ""))
    return bad, warn, note


def main(argv):
    if not argv:
        print(__doc__.strip().splitlines()[0])
        print("использование: python cam/plan_check.py <план.json> [...]")
        return 2
    worst = 0
    for path in argv:
        plan = json.load(io.open(path, encoding="utf-8"))
        bad, warn, note = check(plan, os.path.basename(path))
        head = os.path.relpath(path)
        print(f"\n=== {head}")
        print(f"    фич {len(plan.get('фичи', []))}, "
              f"переходов {len(plan.get('переходы', []))}, "
              f"отказов {len(bad)}, предупреждений {len(warn)}")
        for m in bad:
            print("  ОТКАЗ         " + m)
        for m in warn:
            print("  предупреждение " + m)
        for m in note:
            print("  заметка        " + m)
        worst = max(worst, 1 if bad else 0)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
