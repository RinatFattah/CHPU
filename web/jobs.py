#!/usr/bin/env python3
"""
web/jobs.py — запуск ЛЛМ-петли из веб-морды и разбор её прогресса.

Петля запускается ПОДПРОЦЕССОМ (`auto_fix.py`), а не импортом. Причины:
  * `auto_fix` правит глобальный `config` — в одном процессе с сервером это
    протекло бы между задачами;
  * FreeCAD и NX всё равно поднимаются отдельными процессами, лишний слой
    изоляции ничего не стоит;
  * убить зависшую задачу можно, не роняя сервер.

Прогресс берётся из stdout петли. Она уже печатает осмысленные вехи
(`[autofix] ...`, `[worker] ...`, `[nx-sim] ...`), поэтому парсер — небольшая
таблица шаблонов. Важная тонкость: петля объявляет этап, когда он ЗАКОНЧИЛСЯ
(«G-Code: 7205 строк»), а показать надо то, что идёт СЕЙЧАС. Поэтому парсер —
автомат: увидел конец генерации, значит началась симуляция.

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
PHASES = [
    ("prepare", "подготовка"),
    ("convert", "конвертация .prt → STEP"),
    ("generate", "генерация программы"),
    ("simulate", "симуляция на станке NX"),
    ("diff", "сверка с моделью"),
    ("llm", "агент анализирует"),
    ("apply", "агент правит параметры"),
    ("compare", "сборка файла для NX"),
    ("done", "готово"),
]
PHASE_TITLE = dict(PHASES)

# Шаблон → (новый этап, тип события). None в этапе = этап не менять.
RULES = [
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
    (re.compile(r"^\[autofix\] ЛЛМ считает результат приемлемым"), "done", "ok"),
    (re.compile(r"^\[autofix\] параметрами не лечится"), "done", "unfixable"),
    (re.compile(r"^\[autofix\] (параметр|мёртвая зона|доп\. зона съёма|"
                r"фреза операции|операция отключена|операция включена|"
                r"снята зона|сняты ВСЕ зоны|снято назначение|ПОЛНЫЙ откат)"),
     None, "action"),
    (re.compile(r"^\[autofix\] сборка сравнения"), "compare", "step"),
    (re.compile(r"^\[autofix\] достигнут лимит итераций"), "done", "limit"),
    (re.compile(r"^\[autofix\] журнал:"), None, "step"),
]

# Что показать в списке результатов и как подписать.
OUTPUTS = [
    ("_compare.prt", "Деталь и результат слоями — открыть в NX", True),
    ("_sim.stp", "Результат симуляции (что реально вырезалось)", True),
    (".gcode", "Управляющая программа", True),
    ("_part.step", "Деталь в координатах программы", False),
    ("_stock.stp", "Заготовка в координатах программы", False),
    ("_diff.json", "Сверка: зоны недореза и зареза", False),
    ("_autofix.json", "Журнал итераций петли", False),
]

_lock = threading.Lock()
_jobs = {}
_active = None


class Job:
    def __init__(self, jid, path, llm, llm_model):
        self.id = jid
        self.dir = path
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
    def emit(self, kind, **data):
        ev = {"n": len(self.events), "t": round(time.time() - self.started, 1),
              "kind": kind, "phase": self.phase,
              "phase_title": PHASE_TITLE.get(self.phase, self.phase),
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
        return {"id": self.id, "status": self.status, "phase": self.phase,
                "phase_title": PHASE_TITLE.get(self.phase, self.phase),
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
        out = []
        for suffix, title, primary in OUTPUTS:
            for n in sorted(names):
                if n.endswith(suffix):
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
    for rx, phase, kind in RULES:
        m = rx.match(line)
        if not m:
            continue
        if phase:
            job.phase = phase
        if kind == "iter":
            job.iter, job.iters = int(m.group(1)), int(m.group(2))
            job.emit("iter", text=f"итерация {job.iter} из {job.iters}")
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
        elif kind == "verdict":
            job.verdicts.append({"iter": job.iter, "verdict": m.group(1),
                                 "text": m.group(2)})
            job.emit("verdict", verdict=m.group(1), text=m.group(2))
        elif kind == "asking":
            job.emit("step", text="отправил данные агенту, жду разбора")
        elif kind == "action":
            job.emit("action", text=line.replace("[autofix] ", ""))
        elif kind == "ok":
            job.emit("step", text="расхождения в допуске")
        elif kind == "unfixable":
            job.emit("step", text="агент: параметрами не лечится")
        elif kind == "limit":
            job.emit("step", text="достигнут лимит итераций")
        else:
            job.emit("step", text=line.replace("[autofix] ", ""))
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


def start(model_path, stock_path, cfg, run, llm, llm_model, stock_align=False):
    """Заводит задачу и запускает петлю. Возвращает Job."""
    global _active
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
        job = Job(jid, path, llm, llm_model)
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
    # sys.executable, а не "python": на этой машине в PATH другой интерпретатор,
    # без pyyaml — петля падала на первой же строке чтения конфига
    cmd = [sys.executable, "-X", "utf8", "-u", os.path.join(ROOT, "auto_fix.py"),
           model_path, "--gcode", gcode, "--config", cfg_path,
           "--iters", str(run["iters"]), "--llm", llm]
    if llm_model:
        cmd += ["--llm-model", llm_model]
    if stock_path:
        cmd += ["--stock", stock_path]
        if stock_align:
            cmd += ["--stock-align"]

    with open(os.path.join(path, "cmd.txt"), "w", encoding="utf-8") as f:
        f.write(" ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n")

    job.emit("step", text=f"деталь: {os.path.basename(model_path)}"
                          + (f", заготовка: {os.path.basename(stock_path)}"
                             if stock_path else ", заготовка: бокс от габарита"))
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
