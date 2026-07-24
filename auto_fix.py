#!/usr/bin/env python3
"""
auto_fix.py — автономная ЛЛМ-петля исправления программы обработки.

Цикл (до --iters итераций):
  1. генерация G-кода (FreeCAD, stages) + экспорт детали/заготовки в СК программы;
  2. симуляция на виртуальном станке NX (ISV/CSE) → фактический вырез `_sim.stp`;
  3. булев diff «деталь vs вырез» (step_diff) → JSON недорезов/зарезов;
  4. если дефектов нет — стоп; иначе факты (описание детали + diff + параметры +
     история) отправляются ЛЛМ через `claude -p` (headless CLI, БЕЗ ключей API);
  5. ответ ЛЛМ — СТРОГИЙ JSON с действиями — парсится и применяется:
       set_param — изменить параметр генерации (белый список с границами);
       dead_zone — запретная XY-зона (воркер исключает её из обработки);
       verdict unfixable — дефект параметрами не лечится (второй установ и т.п.);
  6. регенерация со скорректированными параметрами — новая итерация.

Журнал итераций пишется в <gcode>_autofix.json.

CLI:
  python auto_fix.py деталь.stp|.prt [--stock файл] [--stock-align]
                     [--iters 3] [--config config.yaml] [--gcode выход.gcode]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import config
import freecad_cam
import step_describe
import step_diff

for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

# Параметры, которые ЛЛМ разрешено менять: (тип, мин, макс) либо (тип, варианты)
PARAM_WHITELIST = {
    "ROUGH_STEPDOWN":        (float, 0.2, 3.0),
    "ROUGH_STEPOVER":        (float, 10, 60),
    "ROUGH_TOLERANCE":       (float, 0.05, 0.3),
    "ROUGH_ALLOWANCE":       (float, 0.0, 1.0),
    "ROUGH_ALLOWANCE_MODE":  (str, ("none", "xy", "all")),
    "ROUGH_MODE":            (str, ("stages", "layers")),
    "FINISH":                (bool,),
    "SURFACE_STEPOVER":      (float, 10, 50),
    "SURFACE_SAMPLE_INTERVAL": (float, 0.2, 1.0),
}
# Порог «дефектов нет» (мм³): недорез мягче (припуск/фаски), зарез — жёстко
OK_UNDERCUT_MM3 = 100.0
OK_OVERCUT_MM3 = 10.0


def log(msg):
    print(f"[autofix] {msg}", flush=True)


def find_claude() -> str:
    """claude CLI: env CLAUDE_CLI → PATH → бинарник из VSCode-расширения."""
    env = os.environ.get("CLAUDE_CLI", "")
    if env and os.path.exists(env):
        return env
    for name in ("claude", "claude.cmd", "claude.exe"):
        p = shutil.which(name)
        if p:
            return p
    import glob as _glob
    hits = _glob.glob(os.path.join(
        os.path.expanduser("~"), ".vscode", "extensions",
        "anthropic.claude-code-*", "resources", "native-binary", "claude.exe"))
    if hits:
        return sorted(hits)[-1]      # свежайшая версия расширения
    raise RuntimeError("claude CLI не найден (PATH, CLAUDE_CLI, VSCode-расширение) "
                       "— ЛЛМ-петля недоступна")


def ask_llm(prompt: str, timeout: int = 900) -> str:
    """Запрос к ЛЛМ через headless Claude Code (`claude -p`), промпт — через stdin."""
    exe = find_claude()
    proc = subprocess.run(
        [exe, "-p"], input=prompt,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"claude -p вернул код {proc.returncode}: "
                           f"{(proc.stderr or out)[:300]}")
    return out


def extract_json(text: str) -> dict:
    """Достаёт первый сбалансированный JSON-объект из ответа ЛЛМ
    (модель может обернуть его текстом или ```-блоком)."""
    start = text.find("{")
    if start < 0:
        raise ValueError(f"в ответе ЛЛМ нет JSON: {text[:200]}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("JSON в ответе ЛЛМ не сбалансирован")


def gcode_ops(gcode_path: str) -> list:
    """Имена операций из комментариев G-кода — чтобы ЛЛМ могла ссылаться на них."""
    ops = []
    try:
        with open(gcode_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r"\((?:Begin|Finish) operation:\s*([\w-]+)\)", line)
                if m and m.group(1) not in ops:
                    ops.append(m.group(1))
    except OSError:
        pass
    return ops


def build_prompt(part_desc: dict, diff_data: dict, history: list,
                 ops: list | None = None) -> str:
    params_now = {k: getattr(config, k) for k in PARAM_WHITELIST}
    return f"""Ты — технолог-программист ЧПУ. Автоматический CAM-пайплайн (FreeCAD, 3-осевая
фрезеровка сверху, одна концевая фреза Ø{config.TOOL_DIAMETER} мм) сгенерировал программу,
симулятор NX вырезал заготовку, ниже — расхождение результата с моделью детали.

ОПИСАНИЕ ДЕТАЛИ (координаты программы, Z0 = верх детали):
{json.dumps(part_desc, ensure_ascii=False)}

РАСХОЖДЕНИЕ (булев diff; недорез = лишний материал в границах детали, зарез = снято лишнее):
{json.dumps(diff_data, ensure_ascii=False)}

ТЕКУЩИЕ ПАРАМЕТРЫ ГЕНЕРАЦИИ:
{json.dumps(params_now, ensure_ascii=False)}
Операции текущей программы (по порядку): {json.dumps(ops or [], ensure_ascii=False)}
Мёртвые зоны сейчас: {json.dumps(getattr(config, 'DEAD_ZONES', []), ensure_ascii=False)}
Доп. зоны съёма сейчас: {json.dumps(getattr(config, 'EXTRA_ZONES', []), ensure_ascii=False)}
Отключённые операции: {json.dumps(getattr(config, 'SKIP_OPS', []), ensure_ascii=False)}

ИСТОРИЯ ПРЕДЫДУЩИХ ИТЕРАЦИЙ (не повторяй уже испробованное без причины):
{json.dumps(history, ensure_ascii=False)}

ВАЖНЫЕ ФАКТЫ (это НЕ дефекты, не пытайся их чинить):
- floor_skin — намеренная плёнка {config.FLOOR_CLEARANCE} мм у дна (зазор от стола);
- рамка заготовки вне силуэта детали остаётся по техпроцессу;
- припуск задаётся ROUGH_ALLOWANCE_MODE (сейчас {config.ROUGH_ALLOWANCE_MODE!r});
- материал, недоступный сверху (поднутрения, накрытые грани), 3-осевая обработка
  снять НЕ может — это второй установ, параметрами не лечится (verdict unfixable).

ЗАДАЧА: объясни причину каждого значимого недореза/зареза и предложи исправление.
Доступные действия:
- set_param: имена строго из списка {list(PARAM_WHITELIST)} (границы разумные);
- extra_zone: ПРИНУДИТЕЛЬНО дообработать XY-бокс (лечит НЕДОРЕЗ по месту:
  бери bbox зоны недореза с запасом ~2 мм) —
  {{"type":"extra_zone","x":[x0,x1],"y":[y0,y1],"z_bottom":z,"reason":"..."}}
  (z_bottom = нижняя граница съёма, обычно ZMin зоны недореза; зазор от стола
  применится автоматически);
- skip_op: отключить конкретную операцию (лечит ЗАРЕЗ от неё; имя — из списка
  операций выше) — {{"type":"skip_op","name":"RoughSlope2","reason":"..."}};
- dead_zone: запретить ЛЮБУЮ обработку в XY-боксе (крайняя мера против зареза;
  недорезы в этой зоне станут неустранимы) —
  {{"type":"dead_zone","x":[x0,x1],"y":[y0,y1],"reason":"..."}}.

ОТВЕТЬ СТРОГО ОДНИМ JSON-ОБЪЕКТОМ без markdown и пояснений вокруг:
{{"analysis": "краткий разбор по-русски",
  "verdict": "ok | retry | unfixable",
  "actions": [{{"type": "set_param", "name": "...", "value": ...}},
              {{"type": "dead_zone", "x": [x0, x1], "y": [y0, y1], "reason": "..."}}],
  "report": "итог для оператора по-русски"}}
verdict=ok — расхождения приемлемы, действий не нужно; retry — применить actions
и перегенерировать; unfixable — параметрами не лечится (объясни в report)."""


def apply_actions(actions: list) -> list:
    """Применяет действия ЛЛМ к config. Возвращает список принятых (для журнала)."""
    applied = []
    for a in actions or []:
        try:
            if a.get("type") == "set_param":
                name = a["name"]
                spec = PARAM_WHITELIST.get(name)
                if not spec:
                    log(f"отклонено set_param {name}: не в белом списке")
                    continue
                val = a["value"]
                if spec[0] is bool:
                    val = bool(val)
                elif spec[0] is float:
                    val = min(max(float(val), spec[1]), spec[2])
                elif spec[0] is str:
                    if str(val) not in spec[1]:
                        log(f"отклонено set_param {name}={val!r}: вне {spec[1]}")
                        continue
                    val = str(val)
                setattr(config, name, val)
                applied.append({"set_param": name, "value": val})
                log(f"параметр: {name} = {val}")
            elif a.get("type") == "dead_zone":
                zone = {"x": [float(a["x"][0]), float(a["x"][1])],
                        "y": [float(a["y"][0]), float(a["y"][1])]}
                config.DEAD_ZONES = list(getattr(config, "DEAD_ZONES", [])) + [zone]
                applied.append({"dead_zone": zone, "reason": a.get("reason", "")})
                log(f"мёртвая зона: {zone} ({a.get('reason', '')})")
            elif a.get("type") == "extra_zone":
                zone = {"x": [float(a["x"][0]), float(a["x"][1])],
                        "y": [float(a["y"][0]), float(a["y"][1])]}
                for key in ("z_top", "z_bottom"):
                    if key in a:
                        zone[key] = float(a[key])
                config.EXTRA_ZONES = list(getattr(config, "EXTRA_ZONES", [])) + [zone]
                applied.append({"extra_zone": zone, "reason": a.get("reason", "")})
                log(f"доп. зона съёма: {zone} ({a.get('reason', '')})")
            elif a.get("type") == "skip_op":
                name = str(a.get("name", "")).strip()
                if not re.fullmatch(r"(RoughHole|RoughFace|RoughSlope|ExtraZone)\d+"
                                    r"|RoughPerimeter|Finish", name):
                    log(f"отклонено skip_op {name!r}: не похоже на имя операции")
                    continue
                config.SKIP_OPS = list(getattr(config, "SKIP_OPS", [])) + [name]
                applied.append({"skip_op": name, "reason": a.get("reason", "")})
                log(f"операция отключена: {name} ({a.get('reason', '')})")
            else:
                log(f"неизвестное действие: {a}")
        except Exception as e:
            log(f"действие {a} не применилось: {e}")
    return applied


def main():
    ap = argparse.ArgumentParser(
        description="Автономная ЛЛМ-петля: генерация → симуляция NX → diff → "
                    "правка параметров через claude -p → регенерация")
    ap.add_argument("model", help="деталь: .step/.stp/.iges/.brep/.prt")
    ap.add_argument("--gcode", help="куда писать G-Code (дефолт: рядом с моделью)")
    ap.add_argument("--stock", metavar="FILE", help="заготовка из файла")
    ap.add_argument("--stock-align", action="store_true",
                    help="выровнять заготовку по детали (уголок в уголке)")
    ap.add_argument("--iters", type=int, default=3, metavar="N",
                    help="максимум итераций петли (дефолт 3)")
    ap.add_argument("--config", metavar="FILE", help="YAML-конфиг")
    args = ap.parse_args()

    if args.config:
        config.load(args.config)
    if args.stock:
        config.STOCK_FILE = args.stock
    if args.stock_align:
        config.STOCK_ALIGN = True
    config.NX_EXPORT = True          # нужен _part.step в СК программы для diff

    find_claude()                    # проверить ЛЛМ до долгих расчётов
    import nx_sim

    model = args.model
    if os.path.splitext(model)[1].lower() == ".prt":
        import nx_export
        log(f"NX: {os.path.basename(model)} → STEP...")
        model = nx_export.prt_to_step(model)
    gcode = args.gcode or (os.path.splitext(args.model)[0] + ".gcode")
    stem = os.path.splitext(os.path.abspath(gcode))[0]
    journal_path = stem + "_autofix.json"
    journal = {"model": os.path.abspath(args.model), "iterations": []}
    part_desc = None
    history = []

    for it in range(1, args.iters + 1):
        log(f"── итерация {it}/{args.iters} ──")
        t0 = time.perf_counter()
        n = freecad_cam.generate_gcode_freecad(model, gcode)
        log(f"G-Code: {n} строк")
        res = nx_sim.simulate(gcode, stem + "_stock.stp")
        log(f"симуляция: {res['step']} (машинное время {res['machine_time']})")
        # копия детали с расширением .stp: 8.3-имя «.step» даёт «.STE»,
        # который OCCT/step_describe не понимают (пути тут кириллические)
        part_step = os.path.join(tempfile.gettempdir(), "autofix_part.stp")
        shutil.copyfile(stem + "_part.step", part_step)
        d = step_diff.diff(part_step, res["step"])
        log(f"diff: недорез {d['undercut_total_mm3']} мм³ "
            f"({len(d['undercuts'])} зон), зарез {d['overcut_total_mm3']} мм³ "
            f"({len(d['overcuts'])} зон), плёнка дна {d['floor_skin_mm3']} мм³")

        entry = {"iter": it, "gcode_lines": n,
                 "machine_time": res.get("machine_time", ""),
                 "undercut_mm3": d["undercut_total_mm3"],
                 "overcut_mm3": d["overcut_total_mm3"],
                 "wall_s": round(time.perf_counter() - t0, 1)}

        if (d["undercut_total_mm3"] <= OK_UNDERCUT_MM3
                and d["overcut_total_mm3"] <= OK_OVERCUT_MM3):
            entry["verdict"] = "ok (по порогам, без ЛЛМ)"
            journal["iterations"].append(entry)
            log("расхождения в допуске — готово ✅")
            break

        if part_desc is None:
            part_desc = step_describe.describe(part_step)
        log("спрашиваю ЛЛМ (claude -p)...")
        raw = ask_llm(build_prompt(part_desc, d, history, gcode_ops(gcode)))
        try:
            ans = extract_json(raw)
        except ValueError as e:
            log(f"ответ ЛЛМ не разобрался: {e}")
            entry["llm_raw"] = raw[:2000]
            journal["iterations"].append(entry)
            break
        entry["llm"] = {k: ans.get(k) for k in ("analysis", "verdict", "report")}
        log(f"ЛЛМ: {ans.get('verdict')} — {ans.get('analysis', '')[:200]}")

        if ans.get("verdict") == "ok":
            entry["verdict"] = "ok (по оценке ЛЛМ)"
            journal["iterations"].append(entry)
            log(f"ЛЛМ считает результат приемлемым: {ans.get('report', '')}")
            break
        if ans.get("verdict") == "unfixable" or not ans.get("actions"):
            entry["verdict"] = "unfixable"
            journal["iterations"].append(entry)
            log(f"параметрами не лечится: {ans.get('report', '')}")
            break

        applied = apply_actions(ans.get("actions"))
        entry["applied"] = applied
        journal["iterations"].append(entry)
        history.append({"iter": it, "undercut_mm3": d["undercut_total_mm3"],
                        "overcut_mm3": d["overcut_total_mm3"],
                        "actions": applied})
        if not applied:
            log("ни одно действие не применилось — останавливаюсь")
            break
    else:
        log(f"достигнут лимит итераций ({args.iters})")

    with open(journal_path, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False, indent=1)
    log(f"журнал: {journal_path}")


if __name__ == "__main__":
    main()
