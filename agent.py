# -*- coding: utf-8 -*-
"""agent.py — оркестратор: агент идёт по шагам алгоритма и правит техплан.

Замысел (полностью — `База знаний/Агент-технолог/АРХИТЕКТУРА.md`): порядок шагов
задан и агент его не выбирает. На каждом шаге он получает определение шага, блок
базы, посчитанные инструментами числа и текущий план, а возвращает решение в
виде данных. Решение проходит детерминированный валидатор до того, как попадёт
в план.

Что агент НЕ делает: не парсит геометрию (её классифицирует воркер и кладёт в
план-заготовку), не печатает координаты, не выбирает порядок шагов.

    python agent.py <план.json> [--out <план2.json>] [--llm claude|gigachat|openrouter]
                    [--steps 11] [--dry]

`--dry` печатает промпт и выходит: удобно смотреть, что именно уходит модели.

Дальше готовый план подаётся в обычный расчёт:

    python run_cam.py part.stp out.gcode --stock stock.stp --plan план2.json
"""

import argparse
import copy
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_fix import ask_llm, extract_json          # noqa: E402
from cam import norms, plan_check                   # noqa: E402

BLOCKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "База знаний", "Агент-технолог", "Блоки")


def log(msg):
    print(f"[agent] {msg}", flush=True)


def block(code):
    """Текст блока базы по коду (B06 → B06-*.md). Пустая строка, если нет."""
    d = os.path.abspath(BLOCKS_DIR)
    if not os.path.isdir(d):
        return ""
    for f in sorted(os.listdir(d)):
        if f.startswith(code + "-") and f.endswith(".md"):
            return io.open(os.path.join(d, f), encoding="utf-8").read()
    return ""


# ── Шаг 11: деление припуска на рабочие ходы ─────────────────────────────────

def facts_11(plan):
    """Числа, которые агент НЕ должен считать сам: t, B, предел, запас."""
    group = norms.material_group(plan.get("вход", {}).get("материал"))
    by_id = {f["id"]: f for f in plan.get("фичи", [])}
    rows = []
    for tr in plan.get("переходы", []):
        t, B, basis = plan_check.tb_of(tr)
        d = tr.get("инструмент_Ø")
        lim = norms.tb_limit(d, group) if group else None
        f = by_id.get(tr.get("фича")) or {}
        h = None
        if tr.get("Z_от") is not None and tr.get("Z_до") is not None:
            h = round(tr["Z_от"] - tr["Z_до"], 3)
        rows.append({
            "переход": tr["id"], "класс": f.get("класс"), "вид": tr.get("вид"),
            "Ø": d, "высота_среза": h, "слой": tr.get("слой"),
            "ходов": tr.get("рабочих_ходов"),
            "t": t, "B": B, "t×B": round(t * B, 2) if t and B else None,
            "предел": lim, "оценка_t": basis,
        })
    return {"группа_материала": group, "переходы": rows}


PROMPT_11 = """Ты технолог-фрезеровщик. Работаешь по нормативу, а не по наитию.

ШАГ 11 АЛГОРИТМА: деление припуска на рабочие ходы.
Надо назначить слой (глубину одного рабочего хода по оси фрезы, мм) каждому
переходу программы.

НОРМАТИВНЫЙ БЛОК, по которому принимается решение:
--- начало блока B06 ---
{block}
--- конец блока B06 ---

ЧИСЛА ПО ТЕКУЩЕЙ ДЕТАЛИ (посчитаны инструментами, пересчитывать не надо):
{facts}

Пояснения к колонкам: `B` — слой по оси фрезы (то, что назначаешь), `t` —
радиальный съём, `высота_среза` — сколько всего снимает переход по Z,
`оценка_t` = "оценка сверху" значит, что настоящее `t` не больше указанного.

ТРЕБОВАНИЯ:
1. `t × B` каждого хода не больше предела. Где `оценка_t` = "оценка сверху",
   запас должен оставаться и при этой оценке.
2. Слой не больше диаметра фрезы и не больше высоты среза.
3. Слой строго больше нуля.
4. Состав переходов НЕ меняешь: ни добавлять, ни удалять.
5. Чистовым переходам (вид = "чистовой") слой не увеличивай — они снимают
   припуск, а не объём.
6. Где запас до предела велик, слой имеет смысл увеличить: меньше ходов —
   меньше машинного времени. Это цель шага, а не побочный эффект.

ОТВЕТЬ СТРОГО ОДНИМ JSON-объектом, без текста вокруг:
{{"слой": {{"ИмяПерехода": число, ...}},
  "обоснование": {{"ИмяПерехода": "правило и числа, коротко", ...}}}}

Перечисляй только те переходы, которым МЕНЯЕШЬ слой. Если менять нечего —
{{"слой": {{}}, "обоснование": {{}}}}."""


def apply_11(plan, answer):
    """Патч шага 11 → новый план. Бросает ValueError на негодном ответе."""
    new = copy.deepcopy(plan)
    layers = answer.get("слой") or {}
    why = answer.get("обоснование") or {}
    if not isinstance(layers, dict):
        raise ValueError("поле «слой» должно быть объектом")
    by_id = {t["id"]: t for t in new["переходы"]}
    unknown = set(layers) - set(by_id)
    if unknown:
        raise ValueError(f"в плане нет переходов: {', '.join(sorted(unknown))}")
    for name, val in layers.items():
        try:
            v = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: слой «{val}» не число")
        tr = by_id[name]
        tr["слой"] = round(v, 4)
        # Число ходов пересчитывается ЗДЕСЬ же: иначе в плане останется старое,
        # и валидатор будет проверять одно, а исполнитель делать другое.
        if tr.get("Z_от") is not None and tr.get("Z_до") is not None and v > 0:
            import math
            tr["рабочих_ходов"] = max(1, int(math.ceil(
                (tr["Z_от"] - tr["Z_до"]) / v - 1e-6)))
        tr.setdefault("обоснование", []).append({
            "шаг": 11, "блок": "B06", "правило": str(why.get(name, ""))[:300],
            "значение": f"слой {v:g} мм", "вид": "выбор"})
    return new


STEPS = {
    11: {"название": "деление припуска на рабочие ходы", "блок": "B06",
         "факты": facts_11, "промпт": PROMPT_11, "патч": apply_11},
}


def run_step(step, plan, provider, model, dry=False, retries=1):
    """Один шаг: факты → промпт → ответ → патч → валидатор. Возврат: (план, запись)."""
    spec = STEPS[step]
    facts = spec["факты"](plan)
    prompt = spec["промпт"].format(block=block(spec["блок"]).strip(),
                                   facts=json.dumps(facts, ensure_ascii=False,
                                                    indent=1))
    if dry:
        print(prompt)
        return plan, {"шаг": step, "статус": "dry"}

    last = ""
    for attempt in range(retries + 1):
        text = ask_llm(prompt if attempt == 0 else
                       prompt + f"\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН: {last}\n"
                                f"Исправь и ответь снова одним JSON.",
                       provider=provider, model=model)
        try:
            answer = extract_json(text)
            cand = spec["патч"](plan, answer)
            bad, warn, _ = plan_check.check(cand)
            if bad:
                raise ValueError("валидатор: " + "; ".join(bad[:3]))
        except Exception as e:
            last = str(e)[:400]
            log(f"шаг {step}: попытка {attempt + 1} отклонена — {last}")
            continue
        changed = sum(1 for t, o in zip(cand["переходы"], plan["переходы"])
                      if t.get("слой") != o.get("слой"))
        log(f"шаг {step} ({spec['название']}): изменено переходов {changed}, "
            f"предупреждений {len(warn)}")
        return cand, {"шаг": step, "статус": "ок", "изменено": changed,
                      "ответ": answer}

    log(f"шаг {step}: не удалось — оставляю как есть")
    plan.setdefault("невыполнено", []).append(
        {"шаг": step, "причина": f"агент не дал валидного решения: {last}",
         "принято_по_умолчанию": "решение детерминированного воркера"})
    return plan, {"шаг": step, "статус": "отказ", "причина": last}


def main():
    ap = argparse.ArgumentParser(description="агент-технолог: правит техплан по шагам")
    ap.add_argument("plan", help="план-заготовка (out_plan.json)")
    ap.add_argument("--out", help="куда писать (по умолчанию <план>_agent.json)")
    ap.add_argument("--llm", default="claude",
                    choices=["claude", "gigachat", "openrouter"])
    ap.add_argument("--llm-model", default="")
    ap.add_argument("--steps", default="11", help="через запятую, напр. 11")
    ap.add_argument("--dry", action="store_true", help="только показать промпт")
    a = ap.parse_args()

    plan = json.load(io.open(a.plan, encoding="utf-8"))
    steps = [int(x) for x in a.steps.split(",") if x.strip()]
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        ap.error(f"шаги не реализованы: {unknown}; есть {sorted(STEPS)}")

    journal = []
    for s in steps:
        plan, rec = run_step(s, plan, a.llm, a.llm_model, a.dry)
        journal.append(rec)
    if a.dry:
        return 0

    out = a.out or (os.path.splitext(a.plan)[0] + "_agent.json")
    json.dump(plan, io.open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(journal, io.open(os.path.splitext(out)[0] + "_journal.json", "w",
                               encoding="utf-8"), ensure_ascii=False, indent=1)
    bad, warn, _ = plan_check.check(plan)
    log(f"план записан: {out} (отказов {len(bad)}, предупреждений {len(warn)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
