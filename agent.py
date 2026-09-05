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

# База грунтинга лежит В РЕПОЗИТОРИИ: она часть системы, а не заметка о ней, и
# версионируется вместе с кодом, который по ней проверяет.
BLOCKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")


def log(msg):
    print(f"[agent] {msg}", flush=True)


def blocks(codes):
    """Тексты нескольких блоков подряд, разделённые заголовком.

    Шаг опирается не всегда на один блок: у деления припуска норматив (B06)
    задаёт ПРЕДЕЛ, а заводские величины (B04) — практический ориентир, и без
    второго первый на нашей паре «алюминий + твёрдый сплав» не даёт ничего.
    """
    return "\n\n".join(block(c).strip() for c in codes if block(c).strip())


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
    vin = plan.get("вход", {})
    group = norms.material_group(vin.get("материал"))
    mats = {}
    for it in (vin.get("инструмент") or []):
        try:
            mats[round(float(it.get("Ø")), 3)] = it.get("мат")
        except (TypeError, ValueError):
            pass
    by_id = {f["id"]: f for f in plan.get("фичи", [])}
    rows = []
    for tr in plan.get("переходы", []):
        t, B, basis = plan_check.tb_of(tr)
        d = tr.get("инструмент_Ø")
        mat = mats.get(round(float(d), 3)) if d else None
        lim = norms.tb_limit(d, group, mat or "быстрорез") if group else None
        f = by_id.get(tr.get("фича")) or {}
        h = None
        if tr.get("Z_от") is not None and tr.get("Z_до") is not None:
            h = round(tr["Z_от"] - tr["Z_до"], 3)
        rows.append({
            "переход": tr["id"], "класс": f.get("класс"), "вид": tr.get("вид"),
            "Ø": d, "высота_среза": h, "слой": tr.get("слой"),
            "ходов": tr.get("рабочих_ходов"),
            "t": t, "B": B, "t×B": round(t * B, 2) if t and B else None,
            "съём_мм3_мин": tr.get("съём_мм3_мин"),
            "предел": lim, "оценка_t": basis, "материал_фрезы": mat,
        })
    return {"группа_материала": group, "переходы": rows,
            "примечание": ("предел = null означает, что ОН-1980 такое сочетание "
                           "материала детали и материала фрезы НЕ НОРМИРУЕТ; "
                           "подставлять число из соседней колонки нельзя")}


PROMPT_11 = """Ты технолог-фрезеровщик. Работаешь по нормативу, а не по наитию.

ШАГ 11 АЛГОРИТМА: деление припуска на рабочие ходы.
Надо назначить слой (глубину одного рабочего хода по оси фрезы, мм) каждому
переходу программы.

БЛОКИ БАЗЫ, по которым принимается решение (норматив и заводская практика):
--- начало блоков ---
{block}
--- конец блоков ---

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
7. **Если `предел` равен null**, норматив это сочетание материала детали и
   материала фрезы НЕ НОРМИРУЕТ. Брать число из соседней колонки нельзя. Тогда
   единственная опора — таблица заводских слоёв по стадиям из блока B04:
   держись её диапазона и назови стадию, к которой отнёс переход.

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


# ── Шаг 14: рабочая подача ───────────────────────────────────────────────────

def _base_feed(plan):
    """Рабочая подача программы — самая частая среди переходов.

    Не из конфига: план должен читаться сам по себе, в том числе написанный
    руками. Половинная подача чистовых по скруглениям в меньшинстве, поэтому
    мода даёт именно рабочую.
    """
    vals = [float(t["подача"]) for t in plan.get("переходы", [])
            if t.get("подача")]
    return max(set(vals), key=vals.count) if vals else None


def facts_14(plan):
    """Что нужно для подачи: вид перехода, класс фичи, текущая подача."""
    by_id = {f["id"]: f for f in plan.get("фичи", [])}
    rows = []
    for tr in plan.get("переходы", []):
        f = by_id.get(tr.get("фича")) or {}
        rows.append({"переход": tr["id"], "вид": tr.get("вид"),
                     "класс": f.get("класс"), "стратегия": tr.get("стратегия"),
                     "Ø": tr.get("инструмент_Ø"), "подача": tr.get("подача")})
    return {"рабочая_подача_программы": _base_feed(plan),
            "материал": plan.get("вход", {}).get("материал"),
            "переходы": rows}


PROMPT_14 = """Ты технолог-фрезеровщик. Работаешь по правилу, а не по наитию.

ШАГ 14 АЛГОРИТМА: назначить рабочую подачу каждому переходу.

БЛОК, по которому принимается решение:
--- начало блока B12 ---
{block}
--- конец блока B12 ---

ЧИСЛА ПО ТЕКУЩЕЙ ДЕТАЛИ (посчитаны инструментами):
{facts}

ТРЕБОВАНИЯ:
1. Решай ТОЛЬКО по признакам из блока. Своих значений подачи не выдумывай:
   взять их неоткуда и проверить нечем.
2. Признак — ПАРА «вид = чистовой» И «класс = криволинейная грань». Ни один
   по отдельности не годится, в блоке сказано почему.
3. Половинная подача считается от рабочей подачи ЭТОЙ программы (она в фактах),
   а не от заводских 2000.
4. Состав переходов не меняешь.

ОТВЕТЬ СТРОГО ОДНИМ JSON-объектом, без текста вокруг:
{{"подача": {{"ИмяПерехода": число, ...}},
  "обоснование": {{"ИмяПерехода": "правило и признак, коротко", ...}}}}

Перечисляй только те переходы, которым МЕНЯЕШЬ подачу."""


def apply_14(plan, answer):
    """Патч шага 14 → новый план."""
    new = copy.deepcopy(plan)
    vals = answer.get("подача") or {}
    why = answer.get("обоснование") or {}
    if not isinstance(vals, dict):
        raise ValueError("поле «подача» должно быть объектом")
    by_id = {t["id"]: t for t in new["переходы"]}
    unknown = set(vals) - set(by_id)
    if unknown:
        raise ValueError(f"в плане нет переходов: {', '.join(sorted(unknown))}")
    for name, val in vals.items():
        try:
            v = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: подача «{val}» не число")
        if v <= 0:
            raise ValueError(f"{name}: подача {v}")
        tr = by_id[name]
        tr["подача"] = round(v, 1)
        tr.setdefault("обоснование", []).append({
            "шаг": 14, "блок": "B12", "правило": str(why.get(name, ""))[:300],
            "значение": f"F{v:g}", "вид": "выбор"})
    return new


def post_14(plan):
    """Правило B12 выполнено? Возврат — список нарушений."""
    base = _base_feed(plan)
    if not base:
        return []
    by_id = {f["id"]: f for f in plan.get("фичи", [])}
    bad = []
    for tr in plan.get("переходы", []):
        klass = (by_id.get(tr.get("фича")) or {}).get("класс")
        want = norms.feed_for(tr.get("вид"), klass, base)
        got = tr.get("подача")
        if got is None or abs(float(got) - want) > 1.0:
            bad.append(f"{tr['id']}: «{tr.get('вид')}» по классу «{klass}» "
                       f"требует F{want:g}, в плане F{got}")
    return bad


# ── Шаг 15: обороты шпинделя ─────────────────────────────────────────────────

def facts_15(plan):
    """Что нужно, чтобы решить про обороты: класс фичи, стратегия, зацепление."""
    import math
    by_id = {f["id"]: f for f in plan.get("фичи", [])}
    rows = []
    for tr in plan.get("переходы", []):
        d = tr.get("инструмент_Ø")
        n = tr.get("обороты")
        f = by_id.get(tr.get("фича")) or {}
        rows.append({
            "переход": tr["id"], "класс": f.get("класс"),
            "стратегия": tr.get("стратегия"), "Ø": d,
            "наименьшая_ширина_фичи": f.get("наименьшая_ширина"),
            "обороты": n,
            "скорость_резания": (round(math.pi * d * n / 1000.0)
                                 if d and n else None),
        })
    return {"материал": plan.get("вход", {}).get("материал"), "переходы": rows}


PROMPT_15 = """Ты технолог-фрезеровщик. Работаешь по правилу, а не по наитию.

ШАГ 15 АЛГОРИТМА: назначить обороты шпинделя каждому переходу.

БЛОК, по которому принимается решение:
--- начало блока B11 ---
{block}
--- конец блока B11 ---

ЧИСЛА ПО ТЕКУЩЕЙ ДЕТАЛИ (посчитаны инструментами):
{facts}

ТРЕБОВАНИЯ:
1. Решай ТОЛЬКО по признакам из блока. Своих значений скорости резания не
   выдумывай: их неоткуда взять и нечем проверить.
2. Обороты назначай из тех, что блок называет: 12000 или 7984.
3. Полосы, контуры и проходы по средней линии — обычные обороты.
4. Состав переходов не меняешь.

ОТВЕТЬ СТРОГО ОДНИМ JSON-объектом, без текста вокруг:
{{"обороты": {{"ИмяПерехода": число, ...}},
  "обоснование": {{"ИмяПерехода": "правило и признак, коротко", ...}}}}

Перечисляй только те переходы, которым МЕНЯЕШЬ обороты."""


def apply_15(plan, answer):
    """Патч шага 15 → новый план."""
    new = copy.deepcopy(plan)
    vals = answer.get("обороты") or {}
    why = answer.get("обоснование") or {}
    if not isinstance(vals, dict):
        raise ValueError("поле «обороты» должно быть объектом")
    by_id = {t["id"]: t for t in new["переходы"]}
    unknown = set(vals) - set(by_id)
    if unknown:
        raise ValueError(f"в плане нет переходов: {', '.join(sorted(unknown))}")
    for name, val in vals.items():
        try:
            v = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: обороты «{val}» не число")
        if v <= 0:
            raise ValueError(f"{name}: обороты {v}")
        tr = by_id[name]
        tr["обороты"] = round(v, 1)
        tr.setdefault("обоснование", []).append({
            "шаг": 15, "блок": "B11", "правило": str(why.get(name, ""))[:300],
            "значение": f"S{v:g}", "вид": "выбор"})
    return new


def post_15(plan):
    """Правило B11 выполнено? Возврат — список нарушений, пустой если да.

    Признак блока — один и проверяется без модели, поэтому и проверяется без
    модели. Прогон 55 показал, зачем: на 9 деталях из 12 агент правило применил,
    а на 025, 027 и 031 просто не заметил окно и оставил полные обороты. Ошибка
    молчаливая — воксельная сверка режимов не видит, машинное время ISV считает
    по подачам, и в приёмке такая деталь выглядит безупречной.
    """
    by_id = {f["id"]: f for f in plan.get("фичи", [])}
    bad = []
    for tr in plan.get("переходы", []):
        klass = (by_id.get(tr.get("фича")) or {}).get("класс")
        want = norms.rpm_for(klass)
        got = tr.get("обороты")
        if got is None or abs(float(got) - want) > 1.0:
            bad.append(f"{tr['id']}: класс «{klass}» требует S{want:g}, "
                       f"в плане S{got}")
    return bad


STEPS = {
    11: {"название": "деление припуска на рабочие ходы",
         "блок": ["B06", "B04"],
         "факты": facts_11, "промпт": PROMPT_11, "патч": apply_11,
         "поле": "слой"},
    14: {"название": "рабочая подача", "блок": "B12",
         "факты": facts_14, "промпт": PROMPT_14, "патч": apply_14,
         "поле": "подача", "пост": post_14},
    15: {"название": "обороты шпинделя", "блок": "B11",
         "факты": facts_15, "промпт": PROMPT_15, "патч": apply_15,
         "поле": "обороты", "пост": post_15},
}


def post(spec, plan):
    """Постусловие шага, если оно у шага есть.

    Отличие от `plan_check`: тот проверяет план целиком и на любом входе, а это
    — выполнено ли правило ИМЕННО ЭТОГО шага. Разделение принципиальное: у
    плана-заготовки шаг 15 честно лежит в «невыполнено», и объявлять его
    нарушением плана нельзя, а вот шаг, который агент только что отработал,
    обязан своё правило выполнить.
    """
    f = spec.get("пост")
    return f(plan) if f else []


def new_faults(before, after):
    """Отказы, которых до патча не было.

    Отвергать патч за отказ, который уже был в плане, нельзя: у детали 033
    вырез 1 x 1 мм не покрыт ни одним переходом с самого начала, и любое решение
    агента по слою или оборотам отвергалось бы за чужую вину.
    """
    was = set(plan_check.check(before)[0])
    return [m for m in plan_check.check(after)[0] if m not in was]


def run_step(step, plan, provider, model, dry=False, retries=1, answer_file=None):
    """Один шаг: факты → промпт → ответ → патч → валидатор. Возврат: (план, запись)."""
    spec = STEPS[step]
    facts = spec["факты"](plan)
    codes = spec["блок"] if isinstance(spec["блок"], (list, tuple)) \
        else [spec["блок"]]
    prompt = spec["промпт"].format(block=blocks(codes),
                                   facts=json.dumps(facts, ensure_ascii=False,
                                                    indent=1))
    if dry:
        print(prompt)
        return plan, {"шаг": step, "статус": "dry"}

    # Готовый ответ из файла: тот же путь, что и у модели, — те же валидаторы,
    # тот же патч. Нужен, когда решение принимает человек или агент, у которого
    # нет доступа к CLI: `--dry` печатает промпт, ответ кладётся в файл.
    if answer_file:
        answer = extract_json(io.open(answer_file, encoding="utf-8").read())
        cand = spec["патч"](plan, answer)
        bad = new_faults(plan, cand) + post(spec, cand)
        warn = plan_check.check(cand)[1]
        if bad:
            raise SystemExit("ответ из файла отклонён валидатором: "
                             + "; ".join(bad[:3]))
        fld = spec.get("поле", "слой")
        changed = sum(1 for t, o in zip(cand["переходы"], plan["переходы"])
                      if t.get(fld) != o.get(fld))
        log(f"шаг {step}: ответ из {os.path.basename(answer_file)} — "
            f"изменено переходов {changed}, предупреждений {len(warn)}")
        return cand, {"шаг": step, "статус": "ок", "изменено": changed,
                      "источник": answer_file, "ответ": answer}

    last = ""
    for attempt in range(retries + 1):
        text = ask_llm(prompt if attempt == 0 else
                       prompt + f"\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН: {last}\n"
                                f"Исправь и ответь снова одним JSON.",
                       provider=provider, model=model)
        try:
            answer = extract_json(text)
            cand = spec["патч"](plan, answer)
            bad = new_faults(plan, cand) + post(spec, cand)
            warn = plan_check.check(cand)[1]
            if bad:
                raise ValueError("валидатор: " + "; ".join(bad[:3]))
        except Exception as e:
            last = str(e)[:400]
            log(f"шаг {step}: попытка {attempt + 1} отклонена — {last}")
            continue
        fld = spec.get("поле", "слой")
        changed = sum(1 for t, o in zip(cand["переходы"], plan["переходы"])
                      if t.get(fld) != o.get(fld))
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
    ap.add_argument("--steps", default="11,14,15",
                    help="через запятую в порядке алгоритма, напр. 11,14,15")
    ap.add_argument("--dry", action="store_true", help="только показать промпт")
    ap.add_argument("--answer", metavar="FILE",
                    help="взять решение из файла вместо вызова модели "
                         "(проходит те же валидаторы)")
    a = ap.parse_args()

    plan = json.load(io.open(a.plan, encoding="utf-8"))
    plan0 = copy.deepcopy(plan)          # чтобы отличить своё от унаследованного
    steps = [int(x) for x in a.steps.split(",") if x.strip()]
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        ap.error(f"шаги не реализованы: {unknown}; есть {sorted(STEPS)}")

    journal = []
    for s in steps:
        plan, rec = run_step(s, plan, a.llm, a.llm_model, a.dry,
                             answer_file=a.answer)
        journal.append(rec)
    if a.dry:
        return 0

    out = a.out or (os.path.splitext(a.plan)[0] + "_agent.json")
    json.dump(plan, io.open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(journal, io.open(os.path.splitext(out)[0] + "_journal.json", "w",
                               encoding="utf-8"), ensure_ascii=False, indent=1)
    bad, warn, _ = plan_check.check(plan)
    added = new_faults(plan0, plan)
    log(f"план записан: {out} (отказов {len(bad)}, из них внесённых агентом "
        f"{len(added)}; предупреждений {len(warn)})")
    for m in bad:
        if m not in added:
            log(f"отказ был в плане-заготовке, не от агента: {m}")
    # Код возврата — про РАБОТУ АГЕНТА, а не про качество исходного плана. У
    # детали 033 вырез 1 x 1 мм не покрыт ни одним переходом изначально, и
    # ненулевой код останавливал бы пакетный прогон на детали, где агент всё
    # сделал правильно.
    return 1 if added else 0


if __name__ == "__main__":
    sys.exit(main())
