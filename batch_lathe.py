#!/usr/bin/env python3
"""
batch_lathe.py — пакетный прогон ТОКАРНЫХ деталей агентной петлёй.

Гоняет `auto_fix_lathe.py` по списку деталей и по списку агентов, СТРОГО
ПОСЛЕДОВАТЕЛЬНО: NX берёт лицензию, а FreeCAD и симуляция занимают машину
целиком — две задачи параллельно не ускорят, а сорвут обе.

    python batch_lathe.py runs/110_noch --agents kimi,gigachat \\
           --tools-without vcmt35_r --until 08:00

Порядок обхода — ПО ДЕТАЛЯМ, внутри детали по агентам: если ночь кончится
раньше списка, на руках останутся ПОЛНЫЕ ПАРЫ на первых деталях, а не половина
опыта на всех. Что не успели — видно в сводке.

Три вещи, ради которых это отдельный скрипт, а не цикл в консоли:

  * СВОДКА ПЕРЕСОБИРАЕТСЯ ПОСЛЕ КАЖДОГО ПРОГОНА (`itog.md`). Утром её можно
    читать, даже если пакет ещё идёт;
  * ПАДЕНИЕ ОДНОГО ПРОГОНА НЕ РОНЯЕТ ПАКЕТ — ошибка уходит в лог, очередь
    едет дальше;
  * ПОТОЛОК ВРЕМЕНИ НА ПРОГОН убивает ДЕРЕВО процессов. Обычный timeout снял
    бы только петлю, а зависший NX остался бы держать лицензию и сорвал бы
    всё, что за ним.

Повторный запуск той же командой ПРОДОЛЖАЕТ пакет: пара, у которой уже есть
журнал петли, пропускается.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

# Агенты: id → (аргументы транспорта, сколько итераций даём, потолок на прогон).
# Потолок — не про качество, а про очередь: у петли есть собственные стопы
# (повтор набора, unfixable), потолок ловит только зависание.
# `--llm-timeout 300` у всех, кто ходит через OpenRouter: там внутри ещё три
# попытки, и с дефолтными 900 зависший запрос съедает 45 минут. В пакете 110
# так сгорели два прогона Kimi целиком — ровно по потолку, секунда в секунду.
AGENTS = {
    "kimi": {"args": ["--llm", "openrouter",
                      "--llm-model", "moonshotai/kimi-k3",
                      "--llm-timeout", "300"],
             "iters": 5, "cap_min": 45,
             "desc": "Kimi K3 через OpenRouter"},
    "gigachat": {"args": ["--llm", "gigachat"],
                 "iters": 10, "cap_min": 60,
                 "desc": "GigaChat (Сбер)"},
    "deepseek": {"args": ["--llm", "openrouter",
                          "--llm-model", "deepseek/deepseek-v4-flash-0731",
                          "--llm-timeout", "300"],
                 "iters": 5, "cap_min": 45,
                 "desc": "DeepSeek V4 Flash через OpenRouter"},
    "claude": {"args": ["--llm", "claude"], "iters": 5, "cap_min": 45,
               "desc": "Claude через headless CLI"},
}

PARTS_DIR = (r"C:\Users\denis\OneDrive\Работа\BRAInLab\Станки\Примеры деталей"
             r"\Примеры ДСЕ КнААЗ 11.06.2026\Детали_после_ВКС\Токарные")


def log(batch_dir, msg):
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(os.path.join(batch_dir, "batch.log"), "a",
              encoding="utf-8") as f:
        f.write(line + "\n")


def find_parts(parts_dir):
    """Детали: по одному .prt на папку. CAM-проекты и результаты симуляции
    отбрасываем — в CAM-проекте самое большое тело это ПЛИТА приспособления,
    и пайплайн обточил бы её."""
    out = []
    for d in sorted(os.listdir(parts_dir)):
        full = os.path.join(parts_dir, d)
        if not os.path.isdir(full):
            continue
        for name in sorted(os.listdir(full)):
            if not name.lower().endswith(".prt"):
                continue
            low = name.lower()
            if any(bad in low for bad in ("cam", "ipw", "sravn", "compare")):
                continue
            out.append((d, os.path.join(full, name)))
            break
    return out


def kill_tree(pid):
    """Убить процесс со всеми потомками.

    Без этого потолок времени бесполезен: subprocess снимет только петлю, а
    её внуки — freecadcmd и NX — останутся, и NX будет держать лицензию.
    """
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, text=True)


def kill_stray_nx():
    """Прибрать NX, если прогон сняли по таймауту: окно ISV живёт своей жизнью
    и следующий прогон встанет на лицензии."""
    for exe in ("ugraf.exe", "run_journal.exe"):
        subprocess.run(["taskkill", "/F", "/IM", exe],
                       capture_output=True, text=True)


def run_one(batch_dir, part_name, model, agent, tools, config, cap_s):
    """Один прогон петли. Возвращает (код, секунды, сообщение)."""
    out_dir = os.path.join(batch_dir, part_name, agent)
    os.makedirs(out_dir, exist_ok=True)
    a = AGENTS[agent]
    cmd = [sys.executable, "-X", "utf8", "-u",
           os.path.join(ROOT, "auto_fix_lathe.py"), model,
           "--gcode", os.path.join(out_dir, "out.gcode"),
           "--iters", str(a["iters"]),
           "--tools", ",".join(tools)] + a["args"]
    if config:
        cmd += ["--config", config]
    with open(os.path.join(out_dir, "cmd.txt"), "w", encoding="utf-8") as f:
        f.write(" ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n")

    t0 = time.perf_counter()
    log_path = os.path.join(out_dir, "loop.log")
    with open(log_path, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=lf,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        try:
            code = proc.wait(timeout=cap_s)
            note = ""
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            kill_stray_nx()
            code, note = -1, f"снят по потолку {cap_s // 60} мин"
    return code, round(time.perf_counter() - t0), note


# ── СВОДКА ──────────────────────────────────────────────────────────────────

def read_journal(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def summarise_run(journal, missing):
    """Журнал петли → строка сводки.

    `missing` — инструмент, которого не дали на старте: главный вопрос опыта
    в том, вернёт ли его агент, и на какой итерации.
    """
    its = (journal or {}).get("iterations") or []
    drs, fixed_at, verdict, wall = [], None, "", 0.0
    for it in its:
        sc = it.get("score") or {}
        if sc.get("max_dr") is not None:
            drs.append(sc["max_dr"])
        wall += float(it.get("wall_s") or 0)
        if missing and missing in (it.get("tools") or []) and fixed_at is None:
            fixed_at = it["iter"]
        v = it.get("verdict") or (it.get("llm") or {}).get("verdict") or ""
        if it.get("error"):
            v = "ПРОГОН УПАЛ"
        if v:
            verdict = v
    # Вернуть инструмент мало — надо его УДЕРЖАТЬ. Отдельная колонка не
    # придирка: в ночном пакете 110 Kimi трижды возвращала 35°-резец на второй
    # итерации и снимала его на третьей, наводя порядок в наборе, — и результат
    # откатывался ровно к первой итерации.
    last = (its[-1].get("tools") or []) if its else []
    return {"iters": len(its), "drs": drs, "fixed_at": fixed_at,
            "kept": bool(missing) and missing in last,
            "verdict": verdict, "wall_s": round(wall),
            "best": min((abs(d) for d in drs), default=None)}


def write_summary(batch_dir, parts, agents, missing, ok_dr):
    """Пересобрать itog.md. Вызывается ПОСЛЕ КАЖДОГО прогона — сводку можно
    читать, пока пакет ещё идёт."""
    lines = [f"# Пакетный прогон токарных деталей — {len(parts)} деталей × "
             f"{len(agents)} агента", ""]
    lines += [f"Старт без инструмента `{missing}`. Вопрос опыта: увидит ли "
              f"агент по сверке, что набор плохой, и вернёт ли недостающий "
              f"резец. Допуск приёмки {ok_dr} мм по радиусу.", "",
              "| деталь | агент | итер. | вернул инструмент | удержал | "
              "max\\|Δr\\| по итерациям, мм | лучшее | вердикт | время |",
              "|---|---|---:|---|---|---|---:|---|---:|"]
    stats = {a: {"runs": 0, "fixed": 0, "kept": 0, "iters": 0, "wall": 0}
             for a in agents}
    for part_name, _ in parts:
        for agent in agents:
            jp = os.path.join(batch_dir, part_name, agent, "out_loop.json")
            j = read_journal(jp)
            if j is None:
                continue
            s = summarise_run(j, missing)
            st = stats[agent]
            st["runs"] += 1
            st["iters"] += s["iters"]
            st["wall"] += s["wall_s"]
            if s["fixed_at"]:
                st["fixed"] += 1
            if s["kept"]:
                st["kept"] += 1
            traj = " → ".join(f"{d:+.3f}" for d in s["drs"]) or "—"
            fixed = (f"да, на итер. {s['fixed_at']}" if s["fixed_at"] else
                     "**нет**")
            kept = "да" if s["kept"] else ("**снял обратно**" if s["fixed_at"]
                                           else "—")
            best = f"{s['best']:.3f}" if s["best"] is not None else "—"
            lines.append(f"| {part_name} | {agent} | {s['iters']} | {fixed} | "
                         f"{kept} | {traj} | {best} | {s['verdict'] or '—'} | "
                         f"{s['wall_s'] // 60} мин |")
    lines += ["", "## Итого по агентам", "",
              "| агент | прогонов | вернул инструмент | удержал до конца | "
              "итераций всего | время |", "|---|---:|---:|---:|---:|---:|"]
    for agent in agents:
        st = stats[agent]
        lines.append(f"| {agent} | {st['runs']} | {st['fixed']} | "
                     f"{st['kept']} | {st['iters']} | {st['wall'] // 60} мин |")
    done = {(p, a) for p, _ in parts for a in agents
            if os.path.exists(os.path.join(batch_dir, p, a, "out_loop.json"))}
    todo = [(p, a) for p, _ in parts for a in agents if (p, a) not in done]
    if todo:
        lines += ["", f"## Не успели ({len(todo)})", "",
                  ", ".join(f"{p}/{a}" for p, a in todo)]
    with open(os.path.join(batch_dir, "itog.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── ПАКЕТ ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Пакетный прогон токарных деталей агентной петлёй")
    ap.add_argument("out", help="папка пакета, напр. runs/110_noch")
    ap.add_argument("--parts-dir", default=PARTS_DIR,
                    help="папка с деталями (по одному .prt на подпапку)")
    ap.add_argument("--parts", metavar="ИМЕНА",
                    help="только эти детали (по имени папки, через запятую)")
    ap.add_argument("--agents", default="kimi,gigachat",
                    help=f"через запятую: {', '.join(AGENTS)}")
    ap.add_argument("--tools", metavar="IDS",
                    help="стартовый набор инструмента (по умолчанию весь парк "
                         "минус --tools-without)")
    ap.add_argument("--tools-without", metavar="ID", default="vcmt35_r",
                    help="убрать из стартового набора этот инструмент "
                         "(дефолт vcmt35_r — 35°-ромб)")
    ap.add_argument("--config", metavar="FILE", default="config.yaml",
                    help="YAML пайплайна (дефолт config.yaml)")
    ap.add_argument("--until", metavar="ЧЧ:ММ",
                    help="не начинать новых прогонов после этого времени")
    ap.add_argument("--ok-dr", type=float, default=0.12, metavar="MM",
                    help="допуск приёмки, мм по радиусу (в сводку)")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать очередь и выйти")
    ap.add_argument("--summary-only", action="store_true",
                    help="только пересобрать itog.md по готовым журналам")
    a = ap.parse_args()

    agents = [s.strip() for s in a.agents.split(",") if s.strip()]
    bad = [x for x in agents if x not in AGENTS]
    if bad:
        ap.error(f"нет таких агентов: {bad}; есть: {', '.join(AGENTS)}")

    from lathe import lathe_tools
    if a.tools:
        tools = [s.strip() for s in a.tools.split(",") if s.strip()]
    else:
        tools = [t for t in lathe_tools.all_ids()
                 if t != (a.tools_without or "")]
    lathe_tools.plan(tools)                       # набор обязан быть рабочим

    parts = find_parts(a.parts_dir)
    if a.parts:
        want = {s.strip() for s in a.parts.split(",") if s.strip()}
        parts = [p for p in parts if p[0] in want]
    if not parts:
        ap.error(f"деталей не найдено в {a.parts_dir}")

    deadline = None
    if a.until:
        h, m = (int(x) for x in a.until.split(":"))
        now = dt.datetime.now()
        deadline = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if deadline <= now:
            deadline += dt.timedelta(days=1)

    batch_dir = os.path.abspath(a.out)
    os.makedirs(batch_dir, exist_ok=True)
    config = os.path.abspath(a.config) if a.config and os.path.exists(
        a.config) else ""

    plan_txt = [f"деталей {len(parts)}, агентов {len(agents)} → "
                f"{len(parts) * len(agents)} прогонов",
                f"стартовый набор ({len(tools)}): {', '.join(tools)}",
                f"НЕ выдан: {a.tools_without or '—'}",
                f"агенты: " + "; ".join(
                    f"{x} — {AGENTS[x]['desc']}, до {AGENTS[x]['iters']} итер., "
                    f"потолок {AGENTS[x]['cap_min']} мин" for x in agents),
                f"дедлайн: {deadline:%d.%m %H:%M}" if deadline else
                "дедлайна нет"]
    if a.dry_run:
        print("\n".join(plan_txt))
        for p, m in parts:
            print(f"   {p:<24} {m}")
        return
    if a.summary_only:
        write_summary(batch_dir, parts, agents, a.tools_without, a.ok_dr)
        print(f"сводка пересобрана → {os.path.join(batch_dir, 'itog.md')}")
        return

    for line in plan_txt:
        log(batch_dir, line)

    for part_name, model in parts:
        for agent in agents:
            out_dir = os.path.join(batch_dir, part_name, agent)
            if os.path.exists(os.path.join(out_dir, "out_loop.json")):
                log(batch_dir, f"{part_name}/{agent}: уже есть журнал — "
                               f"пропускаю")
                continue
            if deadline and dt.datetime.now() >= deadline:
                log(batch_dir, f"дедлайн {deadline:%H:%M} — новых прогонов не "
                               f"начинаю")
                write_summary(batch_dir, parts, agents, a.tools_without,
                              a.ok_dr)
                return
            log(batch_dir, f"── {part_name} / {agent} ── старт")
            try:
                code, secs, note = run_one(
                    batch_dir, part_name, model, agent, tools, config,
                    AGENTS[agent]["cap_min"] * 60)
            except Exception as e:                            # noqa: BLE001
                log(batch_dir, f"{part_name}/{agent}: сорвалось — "
                               f"{type(e).__name__}: {str(e)[:200]}")
                continue
            j = read_journal(os.path.join(out_dir, "out_loop.json"))
            s = summarise_run(j, a.tools_without) if j else None
            tail = ""
            if s:
                traj = " → ".join(f"{d:+.3f}" for d in s["drs"]) or "—"
                tail = (f"итераций {s['iters']}, {traj}, "
                        + ("инструмент вернул на итер. "
                           f"{s['fixed_at']}" if s["fixed_at"]
                           else "инструмент НЕ вернул")
                        + f", вердикт {s['verdict'] or '—'}")
            log(batch_dir, f"{part_name}/{agent}: код {code}, "
                           f"{secs // 60} мин {secs % 60} с"
                           + (f", {note}" if note else "")
                           + (f" | {tail}" if tail else ""))
            write_summary(batch_dir, parts, agents, a.tools_without, a.ok_dr)

    write_summary(batch_dir, parts, agents, a.tools_without, a.ok_dr)
    log(batch_dir, f"пакет закончен, сводка → "
                   f"{os.path.join(batch_dir, 'itog.md')}")


if __name__ == "__main__":
    main()
