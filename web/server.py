#!/usr/bin/env python3
"""
web/server.py — self-hosted веб-морда пайплайна.

    python -m web.server --port 8080 --llm openrouter

Открывается на http://localhost:<порт>. Выбираете деталь и заготовку, правите
параметры (подставлены дефолты пайплайна), жмёте «Начать обработку» — дальше
крутится ЛЛМ-петля `auto_fix.py`, а страница показывает, на каком этапе она
сейчас. В конце — файлы результата на скачивание.

АГЕНТ ВЫБИРАЕТСЯ ПРИ ЗАПУСКЕ СЕРВЕРА (`--llm`), а не в браузере: это настройка
установки, а не параметр детали. Наличие ключа проверяется сразу — иначе
пользователь узнал бы о его отсутствии через пять минут работы NX.

Всё вокруг локальное: сервер слушает 127.0.0.1, файлы кладутся в `runs/web/`,
наружу ничего не ходит, кроме запросов к выбранному ЛЛМ.
"""

import argparse
import asyncio
import json
import os
import re
import sys

import uvicorn
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                               # noqa: E402
from web import jobs, params                                # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_EXT = (".step", ".stp", ".iges", ".igs", ".brep", ".stl", ".obj", ".prt")

app = FastAPI(title="CAM-пайплайн")
AGENT = {"llm": "claude", "llm_model": ""}


def safe_name(name, default="model.step"):
    """Имя загруженного файла → безопасное, с СОХРАНЁННЫМ расширением.

    Расширение решает всё: по нему пайплайн выбирает читатель, а `.prt` вообще
    уходит на конвертацию через NX. Кириллицу и пробелы убираем — FreeCAD/OCCT
    не открывают такие пути (см. `_ascii_safe` в cam/freecad_cam.py).
    """
    base = os.path.basename(name or "")
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in MODEL_EXT:
        raise HTTPException(400, f"формат {ext or '?'} не поддерживается. "
                                 f"Нужен один из: {', '.join(MODEL_EXT)}")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "part"
    return stem[:60] + ext


@app.get("/")
def index():
    return RedirectResponse("/static/index.html")


@app.get("/api/params")
def api_params():
    return {"params": params.current(), "agent": AGENT,
            "active": (jobs.active().state() if jobs.active() else None)}


@app.post("/api/jobs")
async def api_start(model: UploadFile, form: str = Form("{}"),
                    stock: UploadFile | None = None):
    try:
        cfg, run = params.build(json.loads(form or "{}"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except json.JSONDecodeError:
        raise HTTPException(400, "форма пришла не в JSON")

    tmp = os.path.join(jobs.RUNS, "_upload")
    os.makedirs(tmp, exist_ok=True)
    model_path = os.path.join(tmp, safe_name(model.filename))
    with open(model_path, "wb") as f:
        f.write(await model.read())
    stock_path = ""
    if stock is not None and stock.filename:
        stock_path = os.path.join(tmp, "stock_" + safe_name(stock.filename))
        with open(stock_path, "wb") as f:
            f.write(await stock.read())

    try:
        job = jobs.start(model_path, stock_path, cfg, run,
                         AGENT["llm"], AGENT["llm_model"],
                         stock_align=bool(cfg.get("STOCK_ALIGN")))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return job.state()


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "нет такой задачи")
    return job.state()


@app.post("/api/jobs/{jid}/stop")
def api_stop(jid: str):
    if not jobs.stop(jid):
        raise HTTPException(409, "задача уже не выполняется")
    return {"ok": True}


@app.get("/api/jobs/{jid}/events")
async def api_events(jid: str, start: int = 0):
    """Поток событий (SSE). Держим соединение до конца задачи."""
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "нет такой задачи")

    async def gen():
        n = start
        while True:
            new = job.since(n)
            if new:
                n += len(new)
                for ev in new:
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if job.status != "running" and n >= len(job.events):
                yield ("event: state\ndata: "
                       + json.dumps(job.state(), ensure_ascii=False) + "\n\n")
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{jid}/files/{name}")
def api_file(jid: str, name: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "нет такой задачи")
    if os.path.basename(name) != name:
        raise HTTPException(400, "плохое имя файла")
    path = os.path.join(job.dir, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "нет такого файла")
    return FileResponse(path, filename=name,
                        media_type="application/octet-stream")


@app.exception_handler(HTTPException)
def http_error(_request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def check_agent(llm):
    """Проверить, что выбранный агент вообще доступен — СРАЗУ, а не через
    пять минут работы NX."""
    import auto_fix
    if llm == "claude":
        auto_fix.find_claude()
    elif llm == "gigachat" and not auto_fix.gigachat_key():
        raise SystemExit("нет ключа GigaChat: файл .gigachat_key в корне "
                         "репозитория или переменная GIGACHAT_CREDENTIALS")
    elif llm == "openrouter" and not auto_fix.openrouter_key():
        raise SystemExit("нет ключа OpenRouter: файл .openrouter_key в корне "
                         "репозитория или переменная OPENROUTER_API_KEY")


def main():
    ap = argparse.ArgumentParser(description="Веб-морда CAM-пайплайна")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 — пустить в локальную сеть (по умолчанию "
                         "только этот компьютер)")
    ap.add_argument("--llm", default="claude",
                    choices=("claude", "gigachat", "openrouter"),
                    help="кто правит программу в петле; выбирается ЗДЕСЬ, "
                         "в браузере не меняется")
    ap.add_argument("--llm-model", default="", metavar="MODEL",
                    help="модель: opus/sonnet для claude, "
                         "deepseek/deepseek-v4-flash-0731 для openrouter …")
    ap.add_argument("--config", metavar="FILE",
                    help="YAML пайплайна: его значения станут дефолтами формы")
    a = ap.parse_args()

    if a.config:
        config.load(a.config)
        print(f"[web] конфиг: {a.config}")
    check_agent(a.llm)
    AGENT.update(llm=a.llm, llm_model=a.llm_model)
    os.makedirs(jobs.RUNS, exist_ok=True)
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")),
              name="static")

    print(f"[web] агент: {a.llm}" + (f" / {a.llm_model}" if a.llm_model else ""))
    print(f"[web] прогоны: {jobs.RUNS}")
    print(f"[web] откройте http://localhost:{a.port}")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
