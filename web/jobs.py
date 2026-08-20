#!/usr/bin/env python3
"""
web/jobs.py — запуск ЛЛМ-петли из веб-морды и разбор её прогресса.

ДВА ВИДА ОБРАБОТКИ, две разные петли и два разных набора этапов:
  * `mill`  — `auto_fix.py`: генерация → симуляция NX → сверка → агент правит
    ПАРАМЕТРЫ → заново;
  * `lathe` — `auto_fix_lathe.py`: две программы (перехват) → ДВА прогона в
    NX ISV → сборка итоговой детали → сверка → агент меняет НАБОР ИНСТРУМЕНТА
    → заново.
Всё, что их различает, собрано в таблицах `PHASES` / `RULES` / `OUTPUTS` и в
`_command`; остальной код общий.

Петля запускается ПОДПРОЦЕССОМ, а не импортом. Причины:
  * петля правит глобальный `config` — в одном процессе с сервером это
    протекло бы между задачами;
  * FreeCAD и NX всё равно поднимаются отдельными процессами, лишний слой
    изоляции ничего не стоит;
  * убить зависшую задачу можно, не роняя сервер.

Прогресс берётся из stdout петли. Она уже печатает осмысленные вехи
(`[autofix] ...`, `[loop] ...`, `[worker] ...`, `[lathe-sim] ...`), поэтому
парсер — небольшая таблица шаблонов. Важная тонкость: петля объявляет этап,
когда он ЗАКОНЧИЛСЯ («G-Code: 7205 строк»), а показать надо то, что идёт
СЕЙЧАС. Поэтому парсер — автомат: увидел конец генерации, значит началась
симуляция.

ОДНА ЗАДАЧА ЗА РАЗ. Не из лени: NX берёт лицензию, а FreeCAD и симуляция
съедают машину целиком. Вторая параллельная задача не ускорит, а сорвёт обе.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "runs", "web")
# Загруженные файлы — в ASCII-путь: см. пояснение в web/server.py.
UPLOADS = os.path.join(tempfile.gettempdir(), "cam_web_uploads")

# ── этапы, которые показываем ───────────────────────────────────────────────
# where: once — до цикла; cycle — внутри итерации; back — дуга возврата;
# exit — после выхода из цикла. По этому полю фронт рисует схему процесса,
# и она у точения своя: два прогона ISV вместо одного, сборки нет.
PHASES = {
    "mill": [
        {"id": "prepare", "title": "подготовка", "chip": "подготовка",
         "where": "once"},
        {"id": "convert", "title": "конвертация .prt → STEP",
         "chip": ".prt → STEP", "where": "once"},
        {"id": "generate", "title": "генерация программы", "chip": "генерация",
         "where": "cycle"},
        {"id": "simulate", "title": "симуляция на станке NX",
         "chip": "симуляция NX", "where": "cycle"},
        {"id": "diff", "title": "сверка с моделью", "chip": "сверка",
         "where": "cycle"},
        {"id": "llm", "title": "агент анализирует", "chip": "агент",
         "where": "cycle"},
        {"id": "apply", "title": "агент правит параметры",
         "chip": "правки — и заново", "where": "back"},
        {"id": "compare", "title": "сборка файла для NX", "chip": "сборка",
         "where": "exit"},
        {"id": "done", "title": "готово", "chip": "готово", "where": "exit"},
    ],
    "lathe": [
        {"id": "prepare", "title": "подготовка", "chip": "подготовка",
         "where": "once"},
        {"id": "convert", "title": "конвертация .prt → STEP",
         "chip": ".prt → STEP", "where": "once"},
        {"id": "generate", "title": "генерация двух программ",
         "chip": "генерация", "where": "cycle"},
        {"id": "sim1", "title": "установ 1 на станке NX", "chip": "ISV установ 1",
         "where": "cycle"},
        {"id": "sim2", "title": "установ 2 на станке NX", "chip": "ISV установ 2",
         "where": "cycle"},
        {"id": "diff", "title": "сборка итога и сверка", "chip": "итог и сверка",
         "where": "cycle"},
        {"id": "llm", "title": "агент анализирует", "chip": "агент",
         "where": "cycle"},
        {"id": "apply", "title": "агент меняет набор инструмента",
         "chip": "новый набор — и заново", "where": "back"},
        {"id": "done", "title": "готово", "chip": "готово", "where": "exit"},
    ],
}
PHASE_TITLE = {k: {p["id"]: p["title"] for p in v} for k, v in PHASES.items()}

# Шаблон → (новый этап, тип события). None в этапе = этап не менять.
RULES = {
    "mill": [
        (re.compile(r"^\[autofix\] NX: .*→ STEP"), "convert", "step"),
        (re.compile(r"^\[autofix\] ── итерация (\d+)/(\d+)"), "generate", "iter"),
        (re.compile(r"^\[autofix\] G-Code: (\d+) строк"), "simulate", "gcode"),
        (re.compile(r"^\[autofix\] симуляция: (.+?) \(машинное время (.+?)\)"),
         "diff", "sim"),
        (re.compile(r"^\[autofix\] diff: недорез ([\d.]+) мм³ \((\d+) зон\), "
                    r"зарез ([\d.]+) мм³ \((\d+) зон\)"), "llm", "diff"),
        (re.compile(r"^\[autofix\] расхождения в допуске"), "done", "ok"),
        (re.compile(r"^\[autofix\] спрашиваю ЛЛМ"), "llm", "asking"),
        (re.compile(r"^\[autofix\] ЛЛМ: (\S+) — (.*)$"), "apply", "verdict"),
        (re.compile(r"^\[autofix\] ЛЛМ считает результат приемлемым"),
         "done", "ok"),
        (re.compile(r"^\[autofix\] параметрами не лечится"), "done", "unfixable"),
        (re.compile(r"^\[autofix\] (параметр|мёртвая зона|доп\. зона съёма|"
                    r"фреза операции|операция отключена|операция включена|"
                    r"снята зона|сняты ВСЕ зоны|снято назначение|ПОЛНЫЙ откат)"),
         None, "action"),
        (re.compile(r"^\[autofix\] сборка сравнения"), "compare", "step"),
        (re.compile(r"^\[autofix\] достигнут лимит итераций"), "done", "limit"),
        (re.compile(r"^\[autofix\] журнал:"), None, "step"),
    ],
    # Точение: часть вех печатает петля (`[loop]`), часть — сам прогон
    # (run_lathe.py и nx/nx_lathe_sim.py). Петля отдаёт их своим stdout по
    # флагу --stream, иначе они уходили бы только в файл, и страница пять
    # минут показывала бы «генерация».
    "lathe": [
        (re.compile(r"^\[loop\] ── итерация (\d+)/(\d+) ── набор: (.+)$"),
         "generate", "iter"),
        (re.compile(r"^NX:\s+.*→ STEP"), "convert", "step"),
        (re.compile(r"^Инструмент: (.+)$"), None, "tools"),
        (re.compile(r"^Извлечение осевого профиля"), "generate", "step"),
        (re.compile(r"^✅ Профиль: (.+)$"), None, "step"),
        (re.compile(r"^Два установа: (.+)$"), None, "step"),
        (re.compile(r"^✅ Программа(?: установа 2)?: (\d+) строк"),
         None, "gcode"),
        (re.compile(r"^Симуляция на виртуальном токарном"), "sim1", "step"),
        (re.compile(r"^\[lathe-sim\] запускаю NX"), None, "step"),
        (re.compile(r"^\[lathe-sim\] прогон завершён за (.+)$"), None, "step"),
        (re.compile(r"^\s+установ 1 → (\S+)(.*)$"), "sim2", "setup"),
        (re.compile(r"^\s+установ 2 → (\S+)(.*)$"), "diff", "setup"),
        (re.compile(r"^✅ ИТОГ двух установов → (\S+)\s+\((.+)\)$"),
         "diff", "full"),
        (re.compile(r"^\s+сверка итога с моделью: недорез ([\d.]+) мм³, "
                    r"зарез ([\d.]+) мм³"), "diff", "raw_diff"),
        (re.compile(r"^\s+с поправкой на плёнку моста ([\d.]+) мм: "
                    r"недорез ([\d.]+) мм³, зарез ([\d.]+) мм³"),
         "diff", "film"),
        (re.compile(r"^\[loop\] худшее отклонение по радиусу: "
                    r"([+-][\d.]+) мм(?: \(пояс z ([^)]*)\))?; "
                    r"объём по существу ([\d.]+) мм³ \(недорез ([\d.]+) \+ "
                    r"зарез ([\d.]+); не в счёт ([\d.]+) мм³\)"),
         "diff", "metric"),
        (re.compile(r"^\[loop\]\s+не в счёт: (.+)$"), None, "step"),
        (re.compile(r"^\[loop\] итерация заняла (.+)$"), None, "step"),
        (re.compile(r"^\[loop\] в допуске"), "done", "ok"),
        (re.compile(r"^\[loop\] спрашиваю ЛЛМ"), "llm", "asking"),
        (re.compile(r"^\[loop\] ЛЛМ: (\S+) — (.*)$"), "apply", "verdict"),
        (re.compile(r"^\[loop\] набором инструмента не лечится"),
         "done", "unfixable"),
        (re.compile(r"^\[loop\] достигнут лимит итераций"), "done", "limit"),
        (re.compile(r"^\[loop\] (прогон не удался|повторяю тем же набором|"
                    r"повтор тоже не прошёл)"), None, "action"),
        # петля останавливается по-разному: набор повторился, ЛЛМ назвала
        # несуществующий инструмент, объявила ok при непройденном допуске —
        # все эти строки кончаются «останавливаюсь» либо начинаются с ⚠
        (re.compile(r"^\[loop\] (⚠.*|.*останавливаюсь.*)$"), "done", "stop"),
        (re.compile(r"^\[loop\] журнал:"), None, "step"),
    ],
}

# Что показать в списке результатов и как подписать.
OUTPUTS = {
    "mill": [
        ("_compare.prt", "Деталь и результат слоями — открыть в NX", True),
        ("_sim.stp", "Результат симуляции (что реально вырезалось)", True),
        (".gcode", "Управляющая программа", True),
        ("_part.step", "Деталь в координатах программы", False),
        ("_stock.stp", "Заготовка в координатах программы", False),
        ("_diff.json", "Сверка: зоны недореза и зареза", False),
        ("_autofix.json", "Журнал итераций петли", False),
    ],
    "lathe": [
        ("_full.step", "ИТОГ обоих установов, солид — накладывать на деталь",
         True),
        ("_part.step", "Деталь в координатах программы — эталон для наложения",
         True),
        # `_2.gcode` обязан стоять ДО `.gcode`: он кончается и на `.gcode` тоже,
        # а файл забирает первый подошедший суффикс
        ("_2.gcode", "Программа установа 2 (после перехвата)", True),
        (".gcode", "Программа установа 1", True),
        ("_nxdiff.md", "Сверка по поясам: отклонение по радиусу", True),
        ("_setup1_nxsim.stp", "Результат ISV, установ 1", False),
        ("_setup2_nxsim.stp", "Результат ISV, установ 2 (в своей раме)", False),
        ("_stock.stp", "Заготовка: прокат по ГОСТ", False),
        ("_nxdiff.json", "Сверка целиком, машиночитаемая", False),
        ("_loop.json", "Журнал итераций: наборы инструмента и ответы агента",
         False),
    ],
}

# Один файл может попасть под два суффикса (`out_2.gcode` — и `.gcode`, и
# `_2.gcode`), и в списке он задвоился бы. Порядок таблицы — порядок полезности,
# поэтому берём ПЕРВОЕ совпадение и больше файл не показываем.

_lock = threading.Lock()
_jobs = {}
_active = None


class Job:
    def __init__(self, jid, path, kind, llm, llm_model):
        self.id = jid
        self.dir = path
        self.kind = kind
        self.llm = llm
        self.llm_model = llm_model
        self.status = "running"        # running | ok | failed | stopped
        self.phase = "prepare"
        self.iter = 0
        self.iters = 0
        self.error = ""
        self.events = []
        self.metrics = []              # по итерациям: недорез/зарез
        self.verdicts = []
        self.started = time.time()
        self.finished = None
        self.proc = None
        self._wake = threading.Event()

    # ── события ──
    def title(self, phase=None):
        return PHASE_TITLE[self.kind].get(phase or self.phase,
                                          phase or self.phase)

    def emit(self, kind, **data):
        ev = {"n": len(self.events), "t": round(time.time() - self.started, 1),
              "kind": kind, "phase": self.phase, "phase_title": self.title(),
              "iter": self.iter, "iters": self.iters, **data}
        self.events.append(ev)
        self._wake.set()
        return ev

    def since(self, n):
        return self.events[n:]

    def wait(self, timeout=1.0):
        self._wake.wait(timeout)
        self._wake.clear()

    def state(self):
        return {"id": self.id, "kind": self.kind, "status": self.status,
                "phase": self.phase, "phase_title": self.title(),
                "iter": self.iter, "iters": self.iters,
                "llm": self.llm, "llm_model": self.llm_model,
                "elapsed": round((self.finished or time.time()) - self.started),
                "error": self.error, "metrics": self.metrics,
                "verdicts": self.verdicts, "outputs": self.outputs(),
                "tail": self.tail() if self.status == "failed" else []}

    def tail(self, n=25):
        """Последние осмысленные строки лога — их показываем при падении.

        Без этого пользователь видел только «не получилось» и пустую строку:
        причина уходила в блок, который в этот момент скрыт."""
        path = os.path.join(self.dir, "web.log")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = [l.rstrip() for l in f if l.strip()]
        except OSError:
            return []
        return lines[-n:]

    def outputs(self):
        """Файлы результата — по суффиксу, в порядке полезности."""
        if not os.path.isdir(self.dir):
            return []
        names = os.listdir(self.dir)
        out, seen = [], set()
        for suffix, title, primary in OUTPUTS[self.kind]:
            for n in sorted(names):
                if n.endswith(suffix) and n not in seen:
                    seen.add(n)
                    p = os.path.join(self.dir, n)
                    out.append({"name": n, "title": title, "primary": primary,
                                "size": os.path.getsize(p)})
        return out


def get(jid):
    return _jobs.get(jid)


def active():
    return _jobs.get(_active) if _active else None


def _parse(job, line):
    """Строка лога → событие. Возвращает True, если строка что-то значила."""
    for rx, phase, kind in RULES[job.kind]:
        m = rx.match(line)
        if not m:
            continue
        if phase:
            job.phase = phase
        if kind == "iter":
            job.iter, job.iters = int(m.group(1)), int(m.group(2))
            # у точения в той же строке идёт набор инструмента этой итерации
            tools = m.group(3) if (m.lastindex or 0) >= 3 else ""
            job.emit("iter", text=f"итерация {job.iter} из {job.iters}"
                                  + (f" — набор: {tools}" if tools else ""))
        elif kind == "gcode":
            job.emit("step", text=f"программа готова: {m.group(1)} строк")
        elif kind == "sim":
            job.emit("step", text=f"симуляция прошла, машинное время "
                                  f"{m.group(2)}")
        elif kind == "diff":
            u, uz, o, oz = (float(m.group(1)), int(m.group(2)),
                            float(m.group(3)), int(m.group(4)))
            job.metrics.append({"iter": job.iter, "undercut": u, "overcut": o,
                                "undercut_zones": uz, "overcut_zones": oz})
            job.emit("metric", undercut=u, overcut=o,
                     text=f"недорез {u:g} мм³ ({uz} зон), "
                          f"зарез {o:g} мм³ ({oz} зон)")
        elif kind == "metric":                        # точение: приёмка по dr
            dr, at = float(m.group(1)), m.group(2)
            total, u, o = float(m.group(3)), float(m.group(4)), float(m.group(5))
            job.metrics.append({"iter": job.iter, "dr": dr, "at": at,
                                "undercut": u, "overcut": o, "total": total,
                                "floor": float(m.group(6))})
            job.emit("metric", dr=dr, undercut=u, overcut=o,
                     text=f"худшее отклонение по радиусу {dr:+.3f} мм"
                          + (f" (пояс z {at})" if at else "")
                          + f"; по существу {total:g} мм³")
        elif kind == "verdict":
            job.verdicts.append({"iter": job.iter, "verdict": m.group(1),
                                 "text": m.group(2)})
            job.emit("verdict", verdict=m.group(1), text=m.group(2))
        elif kind == "asking":
            job.emit("step", text="отправил данные агенту, жду разбора")
        elif kind == "action":
            job.emit("action", text=re.sub(r"^\[(autofix|loop)\] ", "", line))
        elif kind == "tools":
            job.emit("action", text="раскладка: " + m.group(1))
        elif kind == "setup":
            job.emit("step", text=f"установ готов → {os.path.basename(m.group(1))}"
                                  f"{m.group(2)}")
        elif kind == "full":
            job.emit("step", text=f"итог двух установов собран ({m.group(2)})")
        elif kind == "raw_diff":
            job.emit("step", text=f"сверка сырая: недорез {m.group(1)} мм³, "
                                  f"зарез {m.group(2)} мм³")
        elif kind == "film":
            job.emit("step", text=f"поправка на плёнку симулятора {m.group(1)} мм: "
                                  f"недорез {m.group(2)} мм³, зарез "
                                  f"{m.group(3)} мм³")
        elif kind == "ok":
            job.emit("step", text="расхождения в допуске")
        elif kind == "unfixable":
            job.emit("step", text=re.sub(r"^\[(autofix|loop)\] ", "", line))
        elif kind == "stop":
            job.emit("action", text=re.sub(r"^\[(autofix|loop)\] ", "", line))
        elif kind == "limit":
            job.emit("step", text="достигнут лимит итераций")
        else:
            job.emit("step", text=re.sub(r"^\[(autofix|loop)\] ", "", line))
        return True
    return False


def _pump(job, cmd):
    """Читает stdout петли, разбирает прогресс, ждёт конца."""
    global _active
    try:
        job.proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        log_path = os.path.join(job.dir, "web.log")
        with open(log_path, "w", encoding="utf-8") as lf:
            for line in job.proc.stdout:
                line = line.rstrip("\n")
                lf.write(line + "\n")
                lf.flush()
                if not _parse(job, line):
                    # неразобранные строки тоже показываем — в них детали
                    # работы FreeCAD и NX, по ним видно, что процесс жив
                    job.emit("log", text=line)
        code = job.proc.wait()
        if job.status == "stopped":
            pass
        elif code == 0:
            job.status = "ok"
            job.phase = "done"
        else:
            job.status = "failed"
            job.error = f"петля завершилась с кодом {code}"
    except Exception as e:                                  # noqa: BLE001
        job.status = "failed"
        job.error = str(e)
    finally:
        job.finished = time.time()
        done = {"ok": "готово", "failed": "НЕ ПОЛУЧИЛОСЬ",
                "stopped": "остановлено"}.get(job.status, job.status)
        job.emit("done", status=job.status, error=job.error,
                 text=done + (f": {job.error}" if job.error else ""),
                 tail=job.tail())
        with _lock:
            if _active == job.id:
                _active = None


def _command(kind, model_path, stock_path, cfg_path, gcode, run, llm,
             llm_model, stock_align):
    """Команда запуска петли для своего вида обработки.

    sys.executable, а не "python": на этой машине в PATH другой интерпретатор,
    без pyyaml — петля падала на первой же строке чтения конфига.
    """
    py = [sys.executable, "-X", "utf8", "-u"]
    if kind == "lathe":
        cmd = py + [os.path.join(ROOT, "auto_fix_lathe.py"),
                    model_path, "--gcode", gcode, "--config", cfg_path,
                    "--iters", str(run["iters"]),
                    "--ok-dr", f"{run['ok_dr']:g}",
                    "--llm", llm, "--stream"]
        if run.get("tools"):
            cmd += ["--tools", ",".join(run["tools"])]
        if llm_model:
            cmd += ["--llm-model", llm_model]
        return cmd
    cmd = py + [os.path.join(ROOT, "auto_fix.py"),
                model_path, "--gcode", gcode, "--config", cfg_path,
                "--iters", str(run["iters"]), "--llm", llm]
    if llm_model:
        cmd += ["--llm-model", llm_model]
    if stock_path:
        cmd += ["--stock", stock_path]
        if stock_align:
            cmd += ["--stock-align"]
    return cmd


def start(model_path, stock_path, cfg, run, llm, llm_model, stock_align=False,
          kind="mill"):
    """Заводит задачу и запускает петлю. Возвращает Job."""
    global _active
    if kind not in PHASES:
        raise RuntimeError(f"неизвестный вид обработки {kind!r}")
    with _lock:
        cur = _jobs.get(_active) if _active else None
        if cur and cur.status == "running":
            raise RuntimeError(
                "уже идёт обработка. NX берёт лицензию, а FreeCAD и симуляция "
                "занимают машину целиком — вторая задача параллельно не "
                "ускорит, а сорвёт обе. Дождитесь конца или остановите текущую.")
        jid = time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        path = os.path.join(RUNS, jid)
        os.makedirs(path, exist_ok=True)
        job = Job(jid, path, kind, llm, llm_model)
        _jobs[jid] = job
        _active = jid

    # конфиг задачи — обычный YAML пайплайна, чтобы прогон можно было потом
    # повторить руками той же командой
    cfg_path = os.path.join(path, "cfg.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("# Собрано веб-мордой. Это обычный конфиг пайплайна:\n"
                "# прогон повторяется командой из cmd.txt рядом.\n")
        for k, v in cfg.items():
            f.write(f"{k}: {json.dumps(v, ensure_ascii=False)}\n")

    gcode = os.path.join(path, "out.gcode")
    cmd = _command(kind, model_path, stock_path, cfg_path, gcode, run, llm,
                   llm_model, stock_align)

    with open(os.path.join(path, "cmd.txt"), "w", encoding="utf-8") as f:
        f.write(" ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n")

    job.emit("step", text=("вид обработки: "
                           + ("точение, два установа" if kind == "lathe"
                              else "фрезеровка, 3 оси")))
    job.emit("step", text=f"деталь: {os.path.basename(model_path)}"
                          + ("" if kind == "lathe" else
                             (f", заготовка: {os.path.basename(stock_path)}"
                              if stock_path else ", заготовка: бокс от габарита")))
    if kind == "lathe" and run.get("tools"):
        job.emit("step", text=f"инструмент генератору ({len(run['tools'])}): "
                              + ", ".join(run["tools"]))
    job.emit("step", text=f"агент: {llm}" + (f" / {llm_model}" if llm_model else ""))
    threading.Thread(target=_pump, args=(job, cmd), daemon=True).start()
    return job


def stop(jid):
    job = _jobs.get(jid)
    if not job or job.status != "running":
        return False
    job.status = "stopped"
    job.error = "остановлено пользователем"
    try:
        job.proc.terminate()
    except Exception:                                        # noqa: BLE001
        pass
    return True
