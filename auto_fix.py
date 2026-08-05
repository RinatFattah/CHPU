#!/usr/bin/env python3
"""
auto_fix.py — автономная ЛЛМ-петля исправления программы обработки.

Цикл (до --iters итераций):
  1. генерация G-кода (FreeCAD, stages) + экспорт детали/заготовки в СК программы;
  2. симуляция на виртуальном станке NX (ISV/CSE) → фактический вырез `_sim.stp`;
  3. булев diff «деталь vs вырез» (step_diff) → JSON недорезов/зарезов;
  4. если дефектов нет — стоп; иначе факты (описание детали + diff + параметры +
     история) отправляются ЛЛМ: `claude -p` (headless CLI, без ключей API),
     GigaChat или любая модель через OpenRouter — выбор ключом --llm;
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
from cam import freecad_cam
from cam import step_describe
from cam import step_diff

for _s in (sys.stdout, sys.stderr):
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

# Параметры, которые ЛЛМ разрешено менять: (тип, мин, макс) либо (тип, варианты)
PARAM_WHITELIST = {
    # инструмент ЛЛМ может ДИКТОВАТЬ: в симуляции новая фреза подхватывается
    # автоматически (таблица стойки перезаписывается); на реальном станке её
    # ставит оператор — рекомендация попадает в report/журнал
    "TOOL_DIAMETER":         (float, 1.0, 20.0),
    "ROUGH_STEPDOWN":        (float, 0.2, 3.0),
    "ROUGH_STEPOVER":        (float, 10, 95),
    "ROUGH_STEPOVER_SLOPE":  (float, 10, 60),
    "ROUGH_TOLERANCE":       (float, 0.05, 0.3),
    "ROUGH_ALLOWANCE":       (float, 0.0, 1.0),
    "ROUGH_ALLOWANCE_MODE":  (str, ("none", "xy", "all")),
    "ROUGH_MODE":            (str, ("stages", "layers")),
    # запирать ли 3D-проход внутри границы грани: True убирает зарез у стенки,
    # но оставляет полоску в радиус фрезы вдоль края (лечится мелкой фрезой)
    "SURFACE_KEEP_INSIDE":   (bool,),
    "FLOOR_CLEARANCE":       (float, 0.0, 2.0),
}
# ПОРОГИ ПРОВЕРКИ (DIFF_*) в белый список НЕ входят намеренно: судья не должен
# двигать собственную планку, иначе «дефектов нет» достигается их занижением.
# Порог «дефектов нет» (мм³) — берётся из конфига (DIFF_OK_*) уже ПОСЛЕ
# загрузки YAML, значения тут — только запасные.
OK_UNDERCUT_MM3 = 5.0
OK_OVERCUT_MM3 = 1.0
# Снимок параметров на старте прогона — к нему возвращают reset_param /
# reset_all. Заполняется в main() после загрузки YAML.
PARAM_INITIAL = {}


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


def gigachat_key() -> str:
    """Ключ авторизации GigaChat: переменная окружения GIGACHAT_CREDENTIALS,
    иначе файл .gigachat_key в корне репозитория (он в .gitignore — ключ не
    должен попадать в историю)."""
    key = os.environ.get("GIGACHAT_CREDENTIALS", "").strip()
    if key:
        return key
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gigachat_key")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def ask_gigachat(prompt: str, timeout: int = 900, model: str = "") -> str:
    """Запрос к GigaChat (Сбер) по API. Ключ авторизации — из окружения
    GIGACHAT_CREDENTIALS (в личном кабинете: «Ключ авторизации»), доступ —
    GIGACHAT_SCOPE (PERS для физлиц, B2B/CORP для юрлиц). Токен доступа SDK
    получает и обновляет сам, он живёт 30 минут."""
    try:
        from gigachat import GigaChat
        from gigachat.models import Chat, Messages, MessagesRole
    except ImportError as e:
        raise RuntimeError("не установлен пакет gigachat (pip install gigachat)") from e
    creds = gigachat_key()
    if not creds:
        raise RuntimeError("нет ключа: положите его в файл .gigachat_key в корне "
                           "репозитория или задайте GIGACHAT_CREDENTIALS")
    # verify_ssl_certs=False — как в документации Сбера: цепочка сертификатов
    # у них подписана НУЦ Минцифры, в системном хранилище Windows её обычно нет.
    # Если корневой сертификат установлен — выключите через GIGACHAT_VERIFY_SSL=1.
    verify = os.environ.get("GIGACHAT_VERIFY_SSL", "0") not in ("0", "", "false")
    client = GigaChat(
        base_url=os.environ.get("GIGACHAT_BASE_URL", "https://api.giga.chat/v1"),
        credentials=creds,
        scope=os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        verify_ssl_certs=verify,
        timeout=timeout,
    )
    chat = Chat(model=model or os.environ.get("GIGACHAT_MODEL", "GigaChat-3-Ultra"),
                messages=[Messages(role=MessagesRole.USER, content=prompt)])
    resp = client.chat(chat)
    out = (resp.choices[0].message.content or "").strip()
    u = getattr(resp, "usage", None)
    if u:
        log(f"gigachat: вход {getattr(u, 'prompt_tokens', '?')}, "
            f"выход {getattr(u, 'completion_tokens', '?')} токенов")
    if not out:
        raise RuntimeError("GigaChat вернул пустой ответ")
    return out


def openrouter_key() -> str:
    """Ключ OpenRouter: переменная OPENROUTER_API_KEY, иначе файл
    .openrouter_key в корне репозитория (он в .gitignore)."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".openrouter_key")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def ask_openrouter(prompt: str, timeout: int = 900, model: str = "",
                   tries: int = 3) -> str:
    """Запрос к любой модели через OpenRouter (совместимо с OpenAI Chat API).
    Дефолт — Kimi K3. Стандартной библиотеки хватает, лишних зависимостей нет.

    Запрос повторяется: у рассуждающих моделей длинный ответ иногда обрывается
    на стороне провайдера (finish_reason=error, пустой content) — без повтора
    из-за одного такого сбоя теряется вся деталь (полчаса симуляции)."""
    import urllib.request
    import urllib.error
    key = openrouter_key()
    if not key:
        raise RuntimeError("нет ключа: файл .openrouter_key в корне репозитория "
                           "или переменная OPENROUTER_API_KEY")
    body = json.dumps({
        "model": model or os.environ.get("OPENROUTER_MODEL", "moonshotai/kimi-k3"),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    url = (os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
           + "/chat/completions")
    last = ""
    for n in range(1, tries + 1):
        req = urllib.request.Request(
            url, data=body,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     # OpenRouter просит идентифицировать приложение
                     "X-Title": "CHPU auto_fix"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            # пока модель думает, OpenRouter шлёт строки-«пульс» (": OPENROUTER
            # PROCESSING") перед телом ответа — json.load на них спотыкается
            i = raw.find("{")
            data = json.loads(raw[i:]) if i >= 0 else None
            if data is None:
                last = f"OpenRouter вернул не-JSON: {raw[:300]!r}"
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError) as e:
            detail = (e.read()[:300].decode("utf-8", "replace")
                      if isinstance(e, urllib.error.HTTPError) else str(e))
            last = f"OpenRouter: {detail}"
            data = None
        if data is not None:
            u = data.get("usage") or {}
            if u:
                log(f"openrouter/{data.get('model', '?')}: вход "
                    f"{u.get('prompt_tokens', '?')}, выход "
                    f"{u.get('completion_tokens', '?')} токенов")
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            out = (msg.get("content") or "").strip()
            if out:
                return out
            last = f"OpenRouter вернул пустой ответ: {str(data)[:300]}"
        if n < tries:
            log(f"{last[:160]} — повтор {n + 1}/{tries}")
            time.sleep(10)
    raise RuntimeError(last)


def ask_llm(prompt: str, timeout: int = 900, model: str = "",
            provider: str = "claude") -> str:
    """Запрос к ЛЛМ. provider: claude (headless Claude Code) | gigachat (API
    Сбера) | openrouter (любая модель через OpenRouter, по умолчанию Kimi K3)."""
    if provider == "gigachat":
        return ask_gigachat(prompt, timeout, model)
    if provider == "openrouter":
        return ask_openrouter(prompt, timeout, model)
    exe = find_claude()
    cmd = [exe, "-p"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(
        cmd, input=prompt,
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


def gcode_report(gcode_path: str) -> dict:
    """Разбор ПОСЛЕДНЕЙ траектории по G-коду — что именно поехало на станок.
    По каждой операции: какой фрезой, сколько кадров, где ходила (габарит XY,
    диапазон Z), сколько резала и сколько шла вхолостую. ЛЛМ без этого судит
    вслепую: он не знает ни текущего инструмента операции, ни её глубин, и
    «уменьши фрезу» превращается в угадывание."""
    ops, cur, tool = [], None, None
    tools, lines_total = [], 0
    header = []
    try:
        f = open(gcode_path, encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with f:
        for line in f:
            lines_total += 1
            if line.startswith("(") and lines_total < 12:
                header.append(line.strip())
            m = re.search(r"\(TC:\s*Endmill\s*D([\d.]+)mm\)", line)
            if m:
                tool = float(m.group(1))
                if tool not in tools:
                    tools.append(tool)
                continue
            m = re.search(r"\(Begin operation:\s*([\w-]+)\)", line)
            if m:
                name = m.group(1)
                cur = None
                if not name.startswith("TC:") and name != "Fixture":
                    cur = {"op": name, "tool_d": tool, "кадров": 0,
                           "резы": 0, "холостые": 0,
                           "x": [1e9, -1e9], "y": [1e9, -1e9], "z": [1e9, -1e9]}
                    ops.append(cur)
                continue
            if cur is None or not line[:2] in ("G0", "G1", "G2", "G3"):
                continue
            cur["кадров"] += 1
            cur["холостые" if line.startswith("G0") else "резы"] += 1
            for ax, val in re.findall(r"([XYZ])(-?\d+\.?\d*)", line):
                v = float(val)
                k = ax.lower()
                cur[k][0] = min(cur[k][0], v)
                cur[k][1] = max(cur[k][1], v)
    for o in ops:
        for k in ("x", "y", "z"):
            o[k] = ([round(o[k][0], 2), round(o[k][1], 2)]
                    if o[k][0] < 1e8 else None)
    return {"строк": lines_total, "фрезы_в_программе": tools,
            "шапка": header, "операции": ops}


def numbered(items: list) -> str:
    """Список с номерами — чтобы ЛЛМ могла сослаться на конкретную зону и снять её."""
    if not items:
        return "  (нет)"
    return "\n".join(f"  [{i}] {json.dumps(z, ensure_ascii=False)}"
                     for i, z in enumerate(items))


def build_prompt(part_desc: dict, diff_data: dict, history: list,
                 ops: list | None = None, prog: dict | None = None) -> str:
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
ПОСЛЕДНЯЯ ТРАЕКТОРИЯ (разбор самого G-кода: по операции — какой ФРЕЗОЙ она шла
(tool_d, мм), сколько кадров, сколько из них рабочих и холостых, и где ходила:
x/y — габарит по XY, z — от верхней до нижней точки). Именно эти фрезы стоят
сейчас; «уменьшить фрезу» = задать диаметр МЕНЬШЕ указанного здесь:
{json.dumps(prog or {}, ensure_ascii=False)}
Набор доступных фрез: {json.dumps(getattr(config, 'TOOL_SET', []), ensure_ascii=False)};
пофрезные переопределения: {json.dumps(getattr(config, 'SET_OP_TOOLS', {}), ensure_ascii=False)}
Операции текущей программы (по порядку): {json.dumps(ops or [], ensure_ascii=False)}
Мёртвые зоны сейчас (номер — для отката через remove_dead_zone):
{numbered(getattr(config, 'DEAD_ZONES', []))}
Доп. зоны съёма сейчас (номер — для отката через remove_extra_zone):
{numbered(getattr(config, 'EXTRA_ZONES', []))}
Отключённые операции: {json.dumps(getattr(config, 'SKIP_OPS', []), ensure_ascii=False)}

ИСТОРИЯ ПРЕДЫДУЩИХ ИТЕРАЦИЙ (не повторяй уже испробованное без причины):
{json.dumps(history, ensure_ascii=False)}

ВАЖНЫЕ ФАКТЫ (это НЕ дефекты, не пытайся их чинить):
- floor_skin — намеренная плёнка {config.FLOOR_CLEARANCE} мм у дна (зазор от стола);
- рамка заготовки вне силуэта детали остаётся по техпроцессу;
- припуск задаётся ROUGH_ALLOWANCE_MODE (сейчас {config.ROUGH_ALLOWANCE_MODE!r});
- материал, недоступный сверху (поднутрения, накрытые грани), 3-осевая обработка
  снять НЕ может — это второй установ, параметрами не лечится (verdict unfixable);
- фрезу МОЖНО менять (set_param TOOL_DIAMETER, считай что на складе есть любая):
  меньшая фреза лечит недорезы в узких местах и углах (внутренний угол получает
  радиус = радиус фрезы; в паз уже фрезы она не влезает), но режет медленнее.

ТРЕБОВАНИЯ К РЕЗУЛЬТАТУ (жёсткие, не на твоё усмотрение):
- ЗАРЕЗ ДОЛЖЕН БЫТЬ РОВНО 0. Снятый лишний металл не восстановить — это брак
  детали, а не «мелочь в допуске». Любой зарез выше 0 мм³ обязан быть устранён:
  verdict=ok при ненулевом зарезе ЗАПРЕЩЁН. Если причина понятна, но действий
  из списка не хватает — verdict=unfixable с объяснением, а не ok.
- НЕДОРЕЗ — меньше 1 мм³ суммарно. Оставшийся металл поправим, поэтому здесь
  цель мягче, но 1 мм³ — потолок, выше него тоже retry.
- Компромисс между этими двумя: зарез приоритетнее. Лучше оставить недорез и
  дочистить его следующей итерацией, чем срезать лишнее.

ПОРЯДОК СРЕДСТВ (пробуй строго сверху вниз, ниже спускайся только когда
предыдущее уже испробовано и не помогло — это видно в истории итераций):
 1. ПОДОБРАТЬ ФРЕЗУ на проблемной операции (set_op_tool, при общей причине —
    set_param TOOL_DIAMETER). Самое безопасное средство, но диаметр меняют В ОБЕ
    СТОРОНЫ — смотри, какой механизм у дефекта:
    · НЕДОРЕЗ в узком месте, углу, радиусе → фрезу МЕНЬШЕ: крупная туда не лезет
      (внутренний угол получается радиусом фрезы, в паз уже фрезы вход закрыт);
    · ЗАРЕЗ от 3D-прохода рядом со стенкой → фрезу БОЛЬШЕ. Проход по поверхности
      видит только свою грань; выйдя за её край, мелкая фреза ПРОЛЕЗАЕТ в щель
      между гранью и стенкой, опускается и скребёт стенку боком. Фреза шире щели
      перекрывает её, ложится на стенку сверху и опуститься рядом не может —
      зарез пропадает (проверено: Ø1 → зарез 0.8 мм³, Ø4 на тех же гранях → 0).
 2. МЁРТВАЯ ЗОНА (dead_zone) на место зареза. Ограничивает фрезу по месту, но
    вместе с зарезом убивает и обработку в этом прямоугольнике.
 3. ОТКЛЮЧИТЬ ОПЕРАЦИЮ (skip_op) — крайняя мера. Пропадает ВЕСЬ съём, который
    делала только она, и на её месте гарантированно вырастет большой недорез.
Дообработка недореза (extra_zone) вне этой лестницы — применяй её тогда, когда
металл просто не сняли, независимо от того, чем лечится зарез.

ЗАДАЧА: объясни причину каждого недореза/зареза и предложи исправление.
Доступные действия:
- set_param: имена строго из списка {list(PARAM_WHITELIST)} (границы разумные);
- extra_zone: ПРИНУДИТЕЛЬНО дообработать XY-бокс (лечит НЕДОРЕЗ по месту:
  бери bbox зоны недореза с запасом ~2 мм) —
  {{"type":"extra_zone","x":[x0,x1],"y":[y0,y1],"z_bottom":z,"reason":"..."}}
  (z_bottom = нижняя граница съёма, обычно ZMin зоны недореза; зазор от стола
  применится автоматически);
- set_op_tool: назначить операции фрезу другого Ø (лечит недорез в узком месте/
  углу: мелкая фреза лезет туда, куда крупная нет) —
  {{"type":"set_op_tool","name":"RoughSlope2","diameter":6.0}};
- skip_op: отключить конкретную операцию (лечит ЗАРЕЗ от неё; имя — из списка
  операций выше) — {{"type":"skip_op","name":"RoughSlope2","reason":"..."}}.
  ВНИМАНИЕ: операция перестаёт создаваться совсем, и весь материал, который
  снимала только она, останется недорезом. Отключай, лишь когда её вред больше
  пользы, и помни про обратный ход:
- enable_op: включить обратно ранее отключённую операцию —
  {{"type":"enable_op","name":"RoughSlope2","reason":"..."}}. То же происходит
  автоматически, если назначить отключённой операции фрезу через set_op_tool;

ОТКАТ — любое твоё действие обратимо, пользуйся этим:
- remove_dead_zone / remove_extra_zone: снять зону по НОМЕРУ из списка выше
  (или сразу все) — {{"type":"remove_dead_zone","index":0,"reason":"..."}},
  {{"type":"remove_extra_zone","index":"all","reason":"..."}};
- reset_op_tool: снять назначенную фрезу, операция снова выберет её сама —
  {{"type":"reset_op_tool","name":"RoughSlope1","reason":"..."}};
- reset_param: вернуть параметр к значению на старте прогона —
  {{"type":"reset_param","name":"ROUGH_STEPDOWN","reason":"..."}};
- reset_all: полный откат ВСЕХ правок к первой итерации — когда запутался и
  проще начать заново — {{"type":"reset_all","reason":"..."}}.
- dead_zone: запретить ЛЮБУЮ обработку в XY-боксе (крайняя мера против зареза;
  недорезы в этой зоне станут неустранимы) —
  {{"type":"dead_zone","x":[x0,x1],"y":[y0,y1],"reason":"..."}}.

ОТВЕТЬ СТРОГО ОДНИМ JSON-ОБЪЕКТОМ без markdown и пояснений вокруг:
{{"analysis": "краткий разбор по-русски",
  "verdict": "ok | retry | unfixable",
  "actions": [{{"type": "set_param", "name": "...", "value": ...}},
              {{"type": "dead_zone", "x": [x0, x1], "y": [y0, y1], "reason": "..."}}],
  "report": "итог для оператора по-русски"}}
verdict=ok — ТОЛЬКО когда зарез 0 и недорез < 1 мм³; retry — применить actions
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
            elif a.get("type") in ("remove_dead_zone", "remove_extra_zone"):
                # ОТКАТ зоны по номеру из списка в промпте (или "all" — снять все).
                # Без этого действия ошибочная зона висела до конца прогона и
                # петля не могла выбраться из ямы, которую сама же и выкопала.
                attr = ("DEAD_ZONES" if a["type"] == "remove_dead_zone"
                        else "EXTRA_ZONES")
                cur = list(getattr(config, attr, []) or [])
                idx = a.get("index", a.get("номер"))
                if str(idx).lower() == "all":
                    setattr(config, attr, [])
                    applied.append({a["type"]: "all", "reason": a.get("reason", "")})
                    log(f"сняты ВСЕ зоны {attr} ({len(cur)} шт)")
                    continue
                try:
                    i = int(idx)
                except (TypeError, ValueError):
                    log(f"отклонено {a['type']}: нет номера зоны")
                    continue
                if not 0 <= i < len(cur):
                    log(f"отклонено {a['type']}: номера {i} нет (всего {len(cur)})")
                    continue
                gone = cur.pop(i)
                setattr(config, attr, cur)
                applied.append({a["type"]: gone, "reason": a.get("reason", "")})
                log(f"снята зона {attr}[{i}]: {gone} ({a.get('reason', '')})")
            elif a.get("type") == "reset_op_tool":
                # ОТКАТ пофрезного назначения: операция снова выбирает фрезу сама
                name = str(a.get("name", "")).strip()
                cur = dict(getattr(config, "SET_OP_TOOLS", {}) or {})
                if name not in cur:
                    log(f"reset_op_tool {name!r}: назначения и так нет")
                    continue
                d = cur.pop(name)
                config.SET_OP_TOOLS = cur
                applied.append({"reset_op_tool": name, "было": d,
                                "reason": a.get("reason", "")})
                log(f"снято назначение фрезы у {name} (было Ø{d:g})")
            elif a.get("type") == "reset_param":
                # ОТКАТ параметра к значению, с которого начинали прогон
                name = str(a.get("name", "")).strip()
                if name not in PARAM_INITIAL:
                    log(f"отклонено reset_param {name!r}: не в белом списке")
                    continue
                setattr(config, name, PARAM_INITIAL[name])
                applied.append({"reset_param": name, "value": PARAM_INITIAL[name],
                                "reason": a.get("reason", "")})
                log(f"параметр {name} возвращён к исходному "
                    f"{PARAM_INITIAL[name]}")
            elif a.get("type") == "reset_all":
                # ПОЛНЫЙ откат к состоянию первой итерации — когда петля
                # запуталась в собственных правках и проще начать заново
                for k, v in PARAM_INITIAL.items():
                    setattr(config, k, v)
                config.DEAD_ZONES, config.EXTRA_ZONES = [], []
                config.SKIP_OPS, config.SET_OP_TOOLS = [], {}
                applied.append({"reset_all": True, "reason": a.get("reason", "")})
                log(f"ПОЛНЫЙ откат к исходным параметрам ({a.get('reason', '')})")
            elif a.get("type") == "extra_zone":
                zone = {"x": [float(a["x"][0]), float(a["x"][1])],
                        "y": [float(a["y"][0]), float(a["y"][1])]}
                for key in ("z_top", "z_bottom"):
                    if key in a:
                        zone[key] = float(a[key])
                config.EXTRA_ZONES = list(getattr(config, "EXTRA_ZONES", [])) + [zone]
                applied.append({"extra_zone": zone, "reason": a.get("reason", "")})
                log(f"доп. зона съёма: {zone} ({a.get('reason', '')})")
            elif a.get("type") == "set_op_tool":
                name = str(a.get("name", "")).strip()
                if not re.fullmatch(r"(RoughHole|RoughFace|RoughSlope|ExtraZone)\d+"
                                    r"|RoughPerimeter", name):
                    log(f"отклонено set_op_tool {name!r}: не похоже на имя операции")
                    continue
                d = min(max(float(a["diameter"]), 1.0), 20.0)   # границы фрезы
                config.SET_OP_TOOLS = dict(getattr(config, "SET_OP_TOOLS", {}))
                config.SET_OP_TOOLS[name] = d
                if d not in (getattr(config, "TOOL_SET", None) or []):
                    config.TOOL_SET = sorted(
                        set((getattr(config, "TOOL_SET", None) or
                             [config.TOOL_DIAMETER]) + [d]), reverse=True)
                # назначили фрезу отключённой операции — значит хотят её вернуть
                # (типичный ход: сначала выключили из-за зареза, теперь пускают
                # мелкой фрезой). Без этого фреза назначалась бы в пустоту.
                if name in (getattr(config, "SKIP_OPS", None) or []):
                    config.SKIP_OPS = [n for n in config.SKIP_OPS if n != name]
                    log(f"операция {name} снова включена (ей назначена фреза)")
                applied.append({"set_op_tool": name, "diameter": d})
                log(f"фреза операции {name} = Ø{d:g}")
            elif a.get("type") == "enable_op":
                name = str(a.get("name", "")).strip()
                if name not in (getattr(config, "SKIP_OPS", None) or []):
                    log(f"enable_op {name!r}: операция и так включена")
                    continue
                config.SKIP_OPS = [n for n in config.SKIP_OPS if n != name]
                applied.append({"enable_op": name, "reason": a.get("reason", "")})
                log(f"операция включена обратно: {name} ({a.get('reason', '')})")
            elif a.get("type") == "skip_op":
                name = str(a.get("name", "")).strip()
                if not re.fullmatch(r"(RoughHole|RoughFace|RoughSlope|ExtraZone)\d+"
                                    r"|RoughPerimeter", name):
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
                    "правка параметров ЛЛМ (--llm) → регенерация")
    ap.add_argument("model", help="деталь: .step/.stp/.iges/.brep/.prt")
    ap.add_argument("--gcode", help="куда писать G-Code (дефолт: рядом с моделью)")
    ap.add_argument("--stock", metavar="FILE", help="заготовка из файла")
    ap.add_argument("--stock-align", action="store_true",
                    help="выровнять заготовку по детали (уголок в уголке)")
    ap.add_argument("--iters", type=int, default=3, metavar="N",
                    help="максимум итераций петли (дефолт 3)")
    ap.add_argument("--llm", default="claude",
                    choices=("claude", "gigachat", "openrouter"),
                    help="кто правит программу: claude (headless CLI, дефолт), "
                         "gigachat (API Сбера, ключ в .gigachat_key) или "
                         "openrouter (Kimi K3 и др., ключ в .openrouter_key)")
    ap.add_argument("--llm-model", default="", metavar="MODEL",
                    help="модель: для claude — opus/sonnet/claude-fable-5; "
                         "для gigachat — GigaChat-3-Ultra и т.п. "
                         "'' = дефолт провайдера")
    ap.add_argument("--config", metavar="FILE", help="YAML-конфиг")
    ap.add_argument("--no-compare", action="store_true",
                    help="не собирать <gcode>_compare.prt (деталь + вырез слоями)")
    args = ap.parse_args()

    if args.config:
        config.load(args.config)
    if args.stock:
        config.STOCK_FILE = args.stock
    if args.stock_align:
        config.STOCK_ALIGN = True
    config.NX_EXPORT = True          # нужен _part.step в СК программы для diff

    if args.llm == "claude":
        find_claude()                # проверить ЛЛМ до долгих расчётов
    elif args.llm == "gigachat" and not gigachat_key():
        raise SystemExit("нет ключа GigaChat: файл .gigachat_key в корне репозитория "
                         "или переменная GIGACHAT_CREDENTIALS")
    elif args.llm == "openrouter" and not openrouter_key():
        raise SystemExit("нет ключа OpenRouter: файл .openrouter_key в корне "
                         "репозитория или переменная OPENROUTER_API_KEY")
    from nx import nx_sim

    model = args.model
    if os.path.splitext(model)[1].lower() == ".prt":
        from nx import nx_export
        log(f"NX: {os.path.basename(model)} → STEP...")
        model = nx_export.prt_to_step(model)
    gcode = args.gcode or (os.path.splitext(args.model)[0] + ".gcode")
    stem = os.path.splitext(os.path.abspath(gcode))[0]
    journal_path = stem + "_autofix.json"
    journal = {"model": os.path.abspath(args.model),
               "llm": args.llm, "llm_model": args.llm_model or "(дефолт)",
               "iterations": []}
    part_desc = None
    last_sim = None
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
        last_sim = res["step"]
        # diff кладём и файлом рядом с G-кодом: в журнал идут только итоговые
        # цифры, а зоны с координатами нужны глазами/следующему инструменту
        d = step_diff.diff(part_step, res["step"], stem + "_diff.json")
        log(f"diff: недорез {d['undercut_total_mm3']} мм³ "
            f"({len(d['undercuts'])} зон), зарез {d['overcut_total_mm3']} мм³ "
            f"({len(d['overcuts'])} зон), плёнка дна {d['floor_skin_mm3']} мм³")

        entry = {"iter": it, "gcode_lines": n,
                 "machine_time": res.get("machine_time", ""),
                 "undercut_mm3": d["undercut_total_mm3"],
                 "overcut_mm3": d["overcut_total_mm3"],
                 "wall_s": round(time.perf_counter() - t0, 1)}

        ok_u = float(getattr(config, "DIFF_OK_UNDERCUT_MM3", OK_UNDERCUT_MM3))
        ok_o = float(getattr(config, "DIFF_OK_OVERCUT_MM3", OK_OVERCUT_MM3))
        if (d["undercut_total_mm3"] <= ok_u
                and d["overcut_total_mm3"] <= ok_o):
            entry["verdict"] = "ok (по порогам, без ЛЛМ)"
            journal["iterations"].append(entry)
            log("расхождения в допуске — готово ✅")
            break

        if part_desc is None:
            part_desc = step_describe.describe(part_step)
        default_model = {"claude": "opus",
                         "gigachat": os.environ.get("GIGACHAT_MODEL",
                                                    "GigaChat-3-Ultra"),
                         "openrouter": os.environ.get("OPENROUTER_MODEL",
                                                      "moonshotai/kimi-k3")}
        mdl = args.llm_model or default_model[args.llm]
        log(f"спрашиваю ЛЛМ ({args.llm}, модель {mdl})...")
        raw = ask_llm(build_prompt(part_desc, d, history, gcode_ops(gcode),
                                  gcode_report(gcode)),
                      model=mdl, provider=args.llm)
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

    # сравнение «модель vs что получилось» одним .prt со слоями (1 = деталь,
    # 2 = вырез) — по ПОСЛЕДНЕЙ посчитанной симуляции. Один запуск NX (~1 мин),
    # поэтому в конце, а не на каждой итерации.
    if last_sim and not args.no_compare:
        out_prt = stem + "_compare.prt"
        try:
            from nx import nx_compare
            log("сборка сравнения (деталь + вырез слоями)...")
            done = nx_compare.compare_many([{"part": stem + "_part.step",
                                             "sim": last_sim,
                                             "out_prt": out_prt}])
            log(f"сравнение: {done[0]}" if done
                else "warn: файл сравнения не построился")
        except Exception as e:
            log(f"warn: сравнение не построилось ({e})")

    with open(journal_path, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False, indent=1)
    log(f"журнал: {journal_path}")


if __name__ == "__main__":
    main()
