#!/usr/bin/env python3
"""
auto_fix_lathe.py — агентная петля для ТОЧЕНИЯ: подбор НАБОРА ИНСТРУМЕНТА.

Одна итерация — один вызов `run_lathe.py --two-setups --simulate`, то есть
обе программы, оба установа в NX ISV, сборка итоговой детали и сверка с
моделью. Дальше факты уходят ЛЛМ, та возвращает НОВЫЙ АКТИВНЫЙ НАБОР
инструмента, и цикл повторяется.

    набор инструмента → генерация → ISV установа 1 → ISV установа 2 →
    сборка итога → сверка → ЛЛМ → новый набор

Агент решает РОВНО ОДНО: какие инструменты из заводского парка выдать
генератору. Ни параметров резания, ни траекторий, ни того, какой резец что
делает — раскладку работ генератор строит сам из геометрии выданного
(lathe_tools.plan): чистовым идёт проходной с наибольшим φ₁ = 180 − φ − ε,
черновым самый жёсткий, а если проходной один — он делает и то, и другое.

Почему набор это настоящий рычаг: что не достаёт проходной, уходит канавочному
резцу, а чего нет вовсе — не делается. Убери с 14-31A острый 35°-ромб, и
худшее отклонение растёт с +0.177 до +0.536 мм. Числовые параметры (припуск,
глубина резания) сюда намеренно НЕ вынесены: на разобранной детали они уже на
разрешении метода, а метрику ими можно двигать без улучшения детали.

ЦЕЛЕВАЯ ФУНКЦИЯ считается за вычетом ПОСТОЯННОГО ПОЛА метрики — того, что
точением не лечится в принципе:
  * пояса «грани под ключ» (axisym=false) — лыски шестигранника делает фреза,
    их не точат ни у нас, ни на заводе (на 14-31A это 526 из 529 мм³ недореза);
  * пояса торцов/уступов (face=true) — там расхождение осевое, а не радиальное;
  * КРАЙНИЕ пояса у обоих торцов — известная и необъяснённая просадка торца
    в ISV на ~0.8 мм, воспроизводится на обоих концах с точностью 0.001 мм.
Вычтенное печатается каждой итерацией — молча пол не прячется.

Транспорт ЛЛМ общий с фрезерной петлёй (`auto_fix.ask_llm`): по умолчанию
OpenRouter (ключ в `.openrouter_key`, файл в .gitignore) — он уже умеет
обходить строки-«пульс» перед телом ответа и повторять запрос, когда у
рассуждающей модели ответ умирает на стороне провайдера. `--llm claude` и
`--llm gigachat` работают через тот же диспетчер.

CLI:
  python auto_fix_lathe.py деталь.prt --gcode runs/N/out.gcode \
         --config cfg.yaml --iters 3
"""

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time

import config
from lathe import lathe_tools
from auto_fix import ask_llm, extract_json            # общий транспорт и парсер

for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"


def log(msg):
    print(f"[loop] {msg}", flush=True)


def signature(ids, extra=None):
    """Слепок того, что из набора ДОЙДЁТ ДО ГЕНЕРАТОРА.

    Сравнивать сырые списки id мало: разные наборы дают одни и те же параметры.
    В runs/100 модель добавила второй проходной, не убрав первый, — программа
    вышла побитово прежней, а 340 секунд ISV сгорели. Слепок берётся с самих
    параметров, поэтому их равенство означает равенство программ.
    """
    return lathe_tools.signature(ids, extra)


# ── ЦЕЛЕВАЯ ФУНКЦИЯ ─────────────────────────────────────────────────────────

def score(diff, z_end=None):
    """Расхождение с моделью ЗА ВЫЧЕТОМ постоянного пола метрики.

    Возвращает {"under","over","total","floor_under","floor_over","skipped"}.
    Объёмы пересчитываются из радиального отклонения пояса: для тела вращения
    объём пояса = 2π·r·dz·dr, поэтому обратный ход точен. Берётся отклонение
    С ПОПРАВКОЙ на плёнку моста ISV там, где поправка применялась.
    """
    u = o = fu = fo = 0.0
    worst, worst_at = 0.0, None
    skipped = []
    bands = diff.get("by_z") or []
    # Торцы детали: ближний в нуле, дальний в z_end. Границы СПИСКА поясов для
    # этого не годятся — он выходит за деталь (у 14-31A есть пояс z 0..+1, где
    # металла нет вовсе, и поправка на плёнку дорисовывает там 7 мм³).
    z_hi = 0.0
    z_lo = float(z_end) if z_end is not None else (
        min(float(b["z0"]) for b in bands) if bands else 0.0)
    for b in bands:
        z0, z1 = float(b["z0"]), float(b["z1"])
        if z0 >= z_hi - 1e-6 or z1 <= z_lo + 1e-6:
            continue                        # пояс целиком вне тела детали
        why = None
        if b.get("axisym") is False:
            why = "грани под ключ"
        elif b.get("face"):
            why = "торец/уступ"
        elif b.get("unreachable"):
            # зона, которую программа сама объявила недостижимой выданным
            # набором: остаток отдан канавочному, а он уже наличной пластины.
            # Это заявка на инструмент, а не дефект траектории
            why = b.get("unreachable_why") or "недостижимо выданным набором"
        elif z0 - 1e-6 <= z_hi <= z1 + 1e-6 or z0 - 1e-6 <= z_lo <= z1 + 1e-6:
            why = "крайний пояс (просадка торца в ISV)"
        bu, bo = float(b.get("under_mm3", 0.0)), float(b.get("over_mm3", 0.0))
        dr = float(b.get("dr_under_mm", 0.0)) + float(b.get("dr_over_mm", 0.0))
        # ПРИЁМКА ПО ПРОФИЛЮ, если он снят: у воксельной половины на теле
        # вращения систематический сдвиг +0.03 мм (сетка 0.1 мм округляет
        # границу наружу), и он один по всей детали набирал десятки мм³
        # несуществующего зареза. Профиль на солиде точнее на порядок; где его
        # нет (фасетное тело), остаются воксели.
        dr_prof = b.get("dr_prof_fixed_mm")
        if dr_prof is not None and b.get("r_nom"):
            dz = abs(float(b["z1"]) - float(b["z0"]))
            k = 2.0 * math.pi * float(b["r_nom"]) * dz
            dr = float(dr_prof)
            bu, bo = (dr * k, 0.0) if dr >= 0 else (0.0, -dr * k)
        elif b.get("film_applied") and b.get("r_nom"):
            dz = abs(float(b["z1"]) - float(b["z0"]))
            k = 2.0 * math.pi * float(b["r_nom"]) * dz
            dr = float(b.get("dr_fixed_mm", 0.0))
            bu, bo = (dr * k, 0.0) if dr >= 0 else (0.0, -dr * k)
        if why:
            fu += bu
            fo += bo
            if bu + bo > 1.0:
                skipped.append(f"z {b['z0']:.0f}..{b['z1']:.0f} {why}: "
                               f"{bu + bo:.0f} мм³")
            continue
        u += bu
        o += bo
        if abs(dr) > abs(worst):
            worst, worst_at = dr, (b["z0"], b["z1"])
    return {"under": round(u, 1), "over": round(o, 1), "total": round(u + o, 1),
            "max_dr": round(worst, 3), "max_dr_at": worst_at,
            "floor_under": round(fu, 1), "floor_over": round(fo, 1),
            "skipped": skipped}


# ── ОДНА ИТЕРАЦИЯ ───────────────────────────────────────────────────────────

def _run_logged(cmd, log_path, timeout, stream=False):
    """Запуск прогона с логом. При stream=True вывод дублируется в свой stdout.

    Дублирование нужно тому, кто смотрит за петлёй снаружи (веб-морда): вехи
    прогона печатает run_lathe.py, и без пересылки страница пять минут
    показывала бы «генерация», пока идут два прогона в ISV.
    """
    with open(log_path, "w", encoding="utf-8") as lf:
        if not stream:
            return subprocess.run(cmd, cwd=ROOT, stdout=lf,
                                  stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout)
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
        # Таймаут своими руками: чтение из трубы блокирует, и на зависшем
        # прогоне communicate(timeout=…) сюда уже не добраться.
        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        try:
            for line in proc.stdout:
                lf.write(line)
                lf.flush()
                print(line.rstrip(), flush=True)
            proc.wait()
        finally:
            killer.cancel()
        return proc


def run_once(model, gcode, active, args, tag):
    """Запускает run_lathe.py заданным набором и возвращает его отчёт (dict)."""
    out_dir = os.path.dirname(os.path.abspath(gcode))
    rep_path = os.path.join(out_dir, f"report_{tag}.json")
    log_path = os.path.join(out_dir, f"run_{tag}.log")
    cmd = [sys.executable, "-X", "utf8", "-u", os.path.join(ROOT, "run_lathe.py"),
           model, gcode, "--two-setups", "--simulate",
           "--tools", ",".join(active), "--report", rep_path]
    if args.config:
        cmd += ["--config", args.config]
    if args.sim_setup:
        cmd += ["--sim-setup", args.sim_setup]
    t0 = time.perf_counter()
    proc = _run_logged(cmd, log_path, args.timeout, stream=args.stream)
    wall = round(time.perf_counter() - t0, 1)
    if not os.path.exists(rep_path):
        tail = ""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-12:])
        except OSError:
            pass
        raise RuntimeError(f"прогон не дал отчёта (код {proc.returncode}), "
                           f"лог {log_path}\n{tail}")
    with open(rep_path, encoding="utf-8") as f:
        rep = json.load(f)
    rep["wall_s"] = wall
    rep["log"] = log_path
    return rep


# ── ПРОМПТ ──────────────────────────────────────────────────────────────────

def build_prompt(rep, sc, history, catalog_desc, ok_dr, failure=None):
    setups = [{k: v for k, v in s.items() if k != "ops"} for s in rep["setups"]]
    bands = [b for b in (rep["diff"].get("by_z") or [])
             if b.get("under_mm3", 0) + b.get("over_mm3", 0) > 1.0]
    fail = ("" if not failure else f"""
⚠ ПОСЛЕДНЯЯ ПОПЫТКА ПРОВАЛИЛАСЬ. Набор {json.dumps(failure['tools'], ensure_ascii=False)}
не удалось прогнать в симуляторе даже со второй попытки:
  {failure['error']}
Ниже — данные ПРЕДЫДУЩЕГО удачного прогона. Выбери набор, отличный от
провалившегося; смена чистового резца на форму пластины, которой в удачных
прогонах не было, — вероятная причина отказа симулятора.
""")
    return f"""Ты — технолог-программист ЧПУ.{fail} Токарный CAM-пайплайн сам сгенерировал
управляющую программу на деталь, прогнал её на виртуальном станке NX ISV со съёмом
материала и сверил результат с моделью. Твоя задача — решить, КАКИЕ ИНСТРУМЕНТЫ
ВЫДАТЬ ГЕНЕРАТОРУ.

КАК ЭТО РАБОТАЕТ. Генератору подаётся список доступных инструментов, и он сам
раскладывает по ним работу: чистовой проход и выборку уступов ведёт проходной с
наибольшим φ₁, черновые слои — самый жёсткий, а если проходной один, он делает
и то, и другое. Здесь φ₁ = 180 − φ − ε (φ — угол державки, ε — угол при вершине
пластины) и это мера достижимости: чем φ₁ меньше, тем дольше резец волочит
вспомогательной кромкой по уже обточенной стенке, тем дальше ему приходится
держаться от уступа и тем больше работы уходит канавочному резцу. Работу, которую
делать нечем, генератор не делает вовсе — она остаётся необработанной.

ИНСТРУМЕНТ, КОТОРЫЙ ЕСТЬ НА ЗАВОДЕ (весь парк):
{json.dumps(catalog_desc, ensure_ascii=False)}

ВЫДАНО ГЕНЕРАТОРУ В ЭТОМ ПРОГОНЕ: {json.dumps(rep['tools']['available'], ensure_ascii=False)}
КАК ОН ЭТО РАЗЛОЖИЛ: {json.dumps(rep['tools']['assigned'], ensure_ascii=False)}
Граница установов z = {rep.get('z_split')}, дальний торец детали z = {rep.get('z_end')}.

ЧТО ПОЛУЧИЛОСЬ У ГЕНЕРАТОРА (по установам):
{json.dumps(setups, ensure_ascii=False)}
  uncut_mm3 — объём, который в этом установе не обработан ничем;
  groove_volume_mm3 — объём, недостижимый проходным резцом, отдан канавочному;
  blade_mm — ширина подобранной канавочной пластины, blade_tight=true значит,
  что нужна пластина УЖЕ самой узкой наличной, и дно канавки останется недорезанным;
  left_passes — сколько проходов делает левый резец.

СВЕРКА С МОДЕЛЬЮ (NX ISV, воксельная, поправка на плёнку моста уже снята):
  ХУДШЕЕ ОТКЛОНЕНИЕ ПО РАДИУСУ: {sc['max_dr']:+.3f} мм — это и есть приёмка
  объём по существу: недорез {sc['under']} мм³, зарез {sc['over']} мм³
  не в счёт (точением не лечится): {sc['floor_under'] + sc['floor_over']:.0f} мм³ —
  {'; '.join(sc['skipped']) or 'нет'}
Пояса по z с расхождением (dr в мм по радиусу, плюс = остался металл,
минус = срезано лишнее):
{json.dumps(bands, ensure_ascii=False)}

ИСТОРИЯ ИТЕРАЦИЙ (не повторяй уже испробованный набор):
{json.dumps(history, ensure_ascii=False)}

ВАЖНОЕ, это НЕ дефекты и чинить их не надо:
- лыски шестигранника точением не делаются ни у нас, ни на заводе — это отдельная
  фрезерная операция, и в целевую функцию они уже не входят;
- торцы в ISV срезаются на ~0.8 мм глубже программы, это известная особенность
  симулятора, а не программы;
- остаточная плёнка 0.1–0.2 мм по радиусу — смещённая точка отсчёта у модели резца
  в NX ISV, она снята поправкой;
- резьба нарезается только по явному объявлению, шаг из модели не выводится.

ЧТО ТЫ МОЖЕШЬ СДЕЛАТЬ: выдать генератору ДРУГОЙ НАБОР ИНСТРУМЕНТА — список id
из парка. Больше ничего: ни параметров резания, ни траекторий, ни того, какой
резец что делает. Раскладку он строит сам, твоё дело — что у него будет в руках.
Больше ничего; параметры резания и границу установов трогать нельзя.
Соображения, по которым набор меняют:
- остался металл в узких уступах и переходах → выдать проходной с БОЛЬШИМ φ₁;
  если такой уже выдан, взять его в паре с жёстким — тогда черновую поведёт
  жёсткий, а чистовую острый, и оба будут на своём месте;
- blade_tight=true → нужна более узкая канавочная пластина, а если её нет —
  сменить проходной так, чтобы остаточная канавка получилась шире;
- велик uncut_mm3, а раскладка показывает null → этой работы делать нечем,
  выдай подходящий инструмент;
- инструмент выдан, но ничего не делает (left_passes=0, канавок нет) — можно
  убрать: меньше смен инструмента, короче программа.

ТРЕБОВАНИЕ К РЕЗУЛЬТАТУ, жёсткое и не на твоё усмотрение:
ДОПУСК ПО РАДИУСУ {ok_dr} мм. Сейчас худшее отклонение {sc['max_dr']:+.3f} мм, то есть
{'ДОПУСК ВЫДЕРЖАН' if abs(sc['max_dr']) <= ok_dr else 'ДОПУСК НЕ ВЫДЕРЖАН'}.
verdict=ok разрешён ТОЛЬКО когда |отклонение| ≤ {ok_dr} мм. Если допуск не выдержан,
у тебя ровно два ответа: retry с ДРУГИМ набором инструмента либо unfixable с
объяснением, почему набором это не лечится. Объявлять ok при непройденном допуске
ЗАПРЕЩЕНО — приёмку задаёт допуск, а не твоя оценка.

ОТВЕТЬ СТРОГО ОДНИМ JSON-ОБЪЕКТОМ, без markdown и текста вокруг:
{{"analysis": "краткий разбор по-русски: чем вызвано расхождение",
  "verdict": "ok | retry | unfixable",
  "tools": ["id", "id", ...],
  "report": "итог для технолога по-русски"}}
verdict=ok — допуск выдержан, набор менять не надо (поле tools повтори);
retry — выдай ДРУГОЙ набор (не тот же самый и не из истории), его и прогоним;
unfixable — набором инструмента это не лечится, объясни в report."""


# ── ЗАПРОС К ЛЛМ И ВАЛИДАЦИЯ ОТВЕТА ─────────────────────────────────────────

def ask_next(rep, sc, history, cat_desc, args, cat, journal, entry, active,
             failure=None):
    """Спросить ЛЛМ новый набор. Возвращает список id или None (остановиться).

    Запись в журнал делается здесь же, чтобы след остался у любого исхода.
    """
    extra = getattr(config, "LATHE_TOOLS", None)
    log(f"спрашиваю ЛЛМ ({args.llm_model or args.llm})...")
    try:
        raw = ask_llm(build_prompt(rep, sc, history, cat_desc,
                                   args.ok_dr, failure),
                      timeout=900, model=args.llm_model, provider=args.llm)
        ans = extract_json(raw)
    except Exception as e:
        log(f"ЛЛМ не ответила разбираемым JSON: {e}")
        entry["llm_error"] = str(e)[:2000]
        journal["iterations"].append(entry)
        return None

    entry["llm"] = {k: ans.get(k) for k in ("analysis", "verdict", "report")}
    log(f"ЛЛМ: {ans.get('verdict')} — {str(ans.get('analysis', ''))[:250]}")

    def stop(verdict, msg):
        entry["verdict"] = verdict
        journal["iterations"].append(entry)
        log(msg)
        return None

    if ans.get("verdict") == "ok":
        # Допуск проверен ДО вызова, и он не прошёл — иначе мы бы сюда не
        # дошли. Принимать «ok» после этого нельзя: это ровно тот случай,
        # когда судья двигает собственную планку.
        return stop("ЛЛМ сказала ok при непройденном допуске",
                    f"⚠  ЛЛМ объявила ok, но допуск не выдержан "
                    f"(|{sc['max_dr']:+.3f}| > {args.ok_dr} мм). Её оценку не "
                    f"принимаю — приёмку задаёт допуск. Ответ модели: "
                    f"{ans.get('report', '')}")
    if ans.get("verdict") == "unfixable":
        return stop("unfixable",
                    f"набором инструмента не лечится: {ans.get('report', '')}")

    new = [str(t).strip() for t in (ans.get("tools") or []) if str(t).strip()]
    bad = [t for t in new if t not in cat]
    if bad:
        return stop(f"отклонено: нет в каталоге {bad}",
                    f"ЛЛМ назвала несуществующий инструмент {bad} — "
                    f"останавливаюсь")
    try:
        sig_new = signature(new, extra)
    except ValueError as e:
        return stop(f"отклонено: {e}", f"набор непригоден: {e}")

    # Сравниваем РАЗРЕШЁННЫЕ наборы, а не списки id: «добавить второй чистовой,
    # не убрав первый» даёт побитово ту же программу (runs/100, 340 с впустую).
    if sig_new == signature(active, extra):
        return stop("тот же набор по существу",
                    f"после разрешения ролей набор тот же ({sig_new}) — "
                    f"программа выйдет прежней, останавливаюсь")
    if sig_new in [h.get("sig") for h in history]:
        return stop("повтор набора",
                    f"набор {sig_new} уже пробовали — останавливаюсь")

    entry["next_tools"] = new
    entry["next_sig"] = sig_new
    journal["iterations"].append(entry)
    return new


# ── ПЕТЛЯ ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Агентная петля точения: ЛЛМ подбирает набор инструмента")
    ap.add_argument("model", help="деталь: .step/.stp/.prt")
    ap.add_argument("--gcode", required=True,
                    help="куда писать программу (папка прогона)")
    ap.add_argument("--config", metavar="FILE", help="YAML-конфиг")
    ap.add_argument("--iters", type=int, default=3, metavar="N",
                    help="максимум итераций (дефолт 3; итерация ~6 минут)")
    ap.add_argument("--tools", metavar="IDS",
                    help="стартовый набор (по умолчанию — заводской комплект)")
    ap.add_argument("--llm", default="openrouter",
                    choices=("openrouter", "claude", "gigachat"),
                    help="транспорт запроса к агенту (дефолт openrouter)")
    ap.add_argument("--llm-model", default="", metavar="M",
                    help=f"модель; для openrouter дефолт {DEFAULT_MODEL}")
    ap.add_argument("--stream", action="store_true",
                    help="дублировать вывод прогонов в свой stdout — так за "
                         "петлёй видно снаружи (этим пользуется веб-морда)")
    ap.add_argument("--sim-setup", choices=("1", "2", "both"),
                    help="гнать в ISV только один установ — вдвое дешевле, "
                         "для отладки самой петли")
    ap.add_argument("--ok-dr", type=float, default=0.12, metavar="MM",
                    help="ДОПУСК ПРИЁМКИ по радиусу, мм (дефолт 0.12). Именно "
                         "он решает, готово ли: у токаря приёмка — допуск, а не "
                         "объём. Объём зависит от размера детали и идёт в отчёт "
                         "как контекст")
    ap.add_argument("--timeout", type=int, default=3600, metavar="SEC",
                    help="потолок на одну итерацию, с (дефолт 3600)")
    args = ap.parse_args()
    if args.llm == "openrouter" and not args.llm_model:
        args.llm_model = DEFAULT_MODEL

    if args.config:
        config.load(args.config)
    extra = getattr(config, "LATHE_TOOLS", None)
    cat = lathe_tools.catalog(extra)
    # стартовый набор: что задали флагом, иначе весь парк
    active = ([s.strip() for s in args.tools.split(",") if s.strip()]
              if args.tools else lathe_tools.all_ids(extra))
    cat_desc = lathe_tools.describe(extra)

    stem = os.path.splitext(os.path.abspath(args.gcode))[0]
    journal_path = stem + "_loop.json"
    journal = {"model": os.path.abspath(args.model), "llm": args.llm,
               "llm_model": args.llm_model, "iterations": []}
    history = []
    last_ok = None            # (rep, sc) последнего УДАЧНОГО прогона
    failure = None            # чем провалилась последняя попытка

    for it in range(1, args.iters + 1):
        log(f"── итерация {it}/{args.iters} ── набор: {', '.join(active)}")
        try:
            rep = run_once(args.model, args.gcode, active, args, f"it{it}")
        except Exception as e:
            # ISV — капризная часть: программа иногда не доходит до конца.
            # Один повтор дешевле потерянной петли.
            log(f"прогон не удался: {str(e)[:300]}")
            log("повторяю тем же набором...")
            try:
                rep = run_once(args.model, args.gcode, active, args, f"it{it}r")
            except Exception as e2:
                # Повторилось — набор действительно не симулируется. Это ФАКТ
                # для агента, а не конец петли: пусть выберет другой.
                failure = {"tools": list(active), "error": str(e2)[:600]}
                journal["iterations"].append(
                    {"iter": it, "tools": list(active),
                     "error": str(e2)[:2000], "retried": True})
                log("повтор тоже не прошёл — отдаю провал агенту как факт")
                if last_ok is None:
                    log("удачных прогонов ещё не было, рассуждать не от чего "
                        "— останавливаюсь")
                    break
                rep, sc = last_ok
                history.append({"iter": it, "tools": list(active),
                                "СИМУЛЯЦИЯ НЕ ПРОШЛА": failure["error"][:200]})
                nxt = ask_next(rep, sc, history, cat_desc, args, cat,
                               journal, {"iter": it, "tools": list(active)},
                               active, failure)
                if nxt is None:
                    break
                active, failure = nxt, None
                continue

        if not (rep.get("diff") or {}).get("by_z"):
            # без поясов по z считать нечего: так бывает при --sim-setup 1|2,
            # когда итог не собирается. Молча выдать ноль нельзя — это читалось
            # бы как «дефектов нет».
            log("сверка без поясов по z (итог не собран) — петле нечего "
                "оптимизировать; уберите --sim-setup")
            journal["iterations"].append({"iter": it, "tools": list(active),
                                          "error": "нет by_z в сверке"})
            break
        sc = score(rep.get("diff") or {}, rep.get("z_end"))
        entry = {"iter": it, "tools": list(active), "wall_s": rep["wall_s"],
                 "setups": [{k: s[k] for k in
                             ("setup", "lines", "uncut_mm3", "blade_mm",
                              "blade_tight", "groove_volume_mm3", "left_passes")}
                            for s in rep["setups"]],
                 "score": sc}
        at = (f" (пояс z {sc['max_dr_at'][0]:.0f}..{sc['max_dr_at'][1]:.0f})"
              if sc.get("max_dr_at") else "")
        log(f"худшее отклонение по радиусу: {sc['max_dr']:+.3f} мм{at}; "
            f"объём по существу {sc['total']} мм³ "
            f"(недорез {sc['under']} + зарез {sc['over']}; не в счёт "
            f"{sc['floor_under'] + sc['floor_over']:.0f} мм³)")
        for s in sc["skipped"]:
            log(f"   не в счёт: {s}")
        log(f"итерация заняла {rep['wall_s']:.0f} с")

        if abs(sc["max_dr"]) <= args.ok_dr:
            entry["verdict"] = "ok (по допуску, без ЛЛМ)"
            journal["iterations"].append(entry)
            log(f"в допуске (|{sc['max_dr']:+.3f}| ≤ {args.ok_dr} мм) — готово ✅")
            break

        last_ok = (rep, sc)
        nxt = ask_next(rep, sc, history, cat_desc, args, cat, journal, entry,
                       active)
        if nxt is None:
            break
        history.append({"iter": it, "tools": list(active),
                        "sig": signature(active, getattr(config, "LATHE_TOOLS",
                                                         None)),
                        "score": {k: sc[k] for k in
                                  ("max_dr", "total", "under", "over")},
                        "analysis": str((entry.get("llm") or {})
                                        .get("analysis", ""))[:400]})
        active = nxt
    else:
        log(f"достигнут лимит итераций ({args.iters})")

    with open(journal_path, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False, indent=1)
    log(f"журнал: {journal_path}")


if __name__ == "__main__":
    main()
