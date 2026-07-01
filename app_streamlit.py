#!/usr/bin/env python3
"""
app_streamlit.py — Веб-интерфейс AI 3D Pipeline
Запуск: streamlit run app_streamlit.py -- --config config.yaml
"""

import sys
import os
import re
import datetime
import socket
from pathlib import Path

import streamlit as st

import config

# Загружаем конфиг из --config один раз за сессию.
# Streamlit передаёт аргументы после "--" в sys.argv скрипта.
if "--config" in sys.argv and "config_loaded" not in st.session_state:
    _idx = sys.argv.index("--config")
    if _idx + 1 < len(sys.argv):
        _path = sys.argv[_idx + 1]
        try:
            config.load(_path)
            st.session_state.config_loaded = _path
        except Exception as e:
            st.error(f"Ошибка загрузки конфига {_path!r}: {e}")

from server_3d import ask_llm, clean_code, run_in_blender, generate_gcode, read_stl_bounds

# ── Страница ───────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="AI 3D Pipeline",
    page_icon="🔧",
    layout="wide",
)

# ── Боковая панель ─────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Конфигурация")

    cfg_path = st.text_input(
        "Файл конфига (YAML)",
        value=st.session_state.get("config_loaded", ""),
        placeholder="config.yaml",
    )
    if st.button("Загрузить конфиг", use_container_width=True):
        if not cfg_path:
            st.warning("Укажите путь к файлу")
        else:
            try:
                config.load(cfg_path)
                st.session_state.config_loaded = cfg_path
                st.success("Загружено")
                st.rerun()
            except FileNotFoundError:
                st.error("Файл не найден")
            except Exception as e:
                st.error(str(e))

    st.divider()
    st.caption("**Текущие настройки**")
    st.caption(f"Модель: `{config.OPENAI_MODEL}`")
    st.caption(f"API: `{config.OPENAI_BASE_URL}`")
    st.caption(f"Blender: `{config.BLENDER_HOST}:{config.BLENDER_PORT}`")
    st.caption(f"Output: `{config.OUTPUT_DIR}`")

    st.divider()
    st.caption("**Статус компонентов**")

    if st.button("Проверить", use_container_width=True):
        try:
            s = socket.socket()
            s.settimeout(2)
            s.connect((config.BLENDER_HOST, config.BLENDER_PORT))
            s.close()
            blender_ok = True
        except Exception:
            blender_ok = False

        openai_ok = bool(config.OPENAI_API_KEY)

        c1, c2 = st.columns(2)
        c1.metric("Blender", "✅" if blender_ok else "❌")
        c2.metric("OpenAI", "✅" if openai_ok else "❌")
        if not blender_ok:
            st.caption("Запустите Blender с аддоном blender-mcp")
        if not openai_ok:
            st.caption("Задайте OPENAI_API_KEY в конфиге")

# ── Основная область ───────────────────────────────────────────────────────────

st.title("🔧 AI 3D Pipeline")
st.caption("Текст → Blender 3D → STL + G-Code для фрезерного станка")

st.divider()

description = st.text_area(
    "Описание детали",
    placeholder="Пластина 100×50×5 мм с 4 отверстиями d6 мм по углам",
    height=120,
)

col_name, col_btn = st.columns([3, 1])
with col_name:
    name_raw = st.text_input("Имя детали (латиница, без пробелов)", value="detail")
with col_btn:
    st.write("")  # выравнивание
    st.write("")
    run = st.button(
        "▶ Создать",
        type="primary",
        use_container_width=True,
        disabled=not description.strip(),
    )

if run:
    if not config.OPENAI_API_KEY:
        st.error("OPENAI_API_KEY не задан — загрузите конфиг или задайте переменную окружения")
        st.stop()

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    ts        = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name      = re.sub(r"[^a-zA-Z0-9_]", "_", name_raw) or f"detail_{ts}"
    stl_path  = os.path.join(config.OUTPUT_DIR, f"{name}.stl")
    gcode_path = os.path.join(config.OUTPUT_DIR, f"{name}.gcode")

    generated_code = None

    with st.status("Генерирую деталь...", expanded=True) as status:

        st.write("⏳ Шаг 1/3 — LLM генерирует Blender-код...")
        try:
            raw_code = ask_llm(description, stl_path)
            generated_code = clean_code(raw_code)
            st.write(f"✅ Код получен ({len(generated_code)} символов)")
        except Exception as e:
            status.update(label="Ошибка на шаге 1", state="error")
            st.error(f"OpenAI: {e}")
            st.stop()

        st.write("⏳ Шаг 2/3 — Blender создаёт 3D-модель...")
        try:
            result = run_in_blender(generated_code)
            if result.get("status") != "success":
                raise RuntimeError(result.get("message", "неизвестная ошибка"))
            if not os.path.exists(stl_path):
                raise RuntimeError("STL-файл не создан")
            st.write(f"✅ STL создан ({os.path.getsize(stl_path):,} байт)")
        except Exception as e:
            status.update(label="Ошибка на шаге 2", state="error")
            st.error(f"Blender: {e}")
            st.stop()

        st.write("⏳ Шаг 3/3 — Генерирую G-Code...")
        try:
            gcode_lines = generate_gcode(stl_path, gcode_path)
            st.write(f"✅ G-Code готов ({gcode_lines} строк)")
        except Exception as e:
            status.update(label="Ошибка на шаге 3", state="error")
            st.error(f"G-Code: {e}")
            st.stop()

        status.update(label="Готово!", state="complete", expanded=False)

    # Метрики
    st.success(f"Деталь **{name}** создана")

    xmin, xmax, ymin, ymax, zmin, zmax = read_stl_bounds(stl_path)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("STL", f"{os.path.getsize(stl_path) // 1024} КБ")
    m2.metric("G-Code", f"{gcode_lines} строк")
    m3.metric("X", f"{xmax - xmin:.1f} мм")
    m4.metric("Y", f"{ymax - ymin:.1f} мм")
    m5.metric("Z", f"{zmax - zmin:.1f} мм")

    # Скачать
    dl1, dl2 = st.columns(2)
    with dl1:
        with open(stl_path, "rb") as f:
            st.download_button(
                "⬇ Скачать STL",
                f.read(),
                file_name=f"{name}.stl",
                mime="application/octet-stream",
                use_container_width=True,
            )
    with dl2:
        with open(gcode_path, encoding="utf-8") as f:
            st.download_button(
                "⬇ Скачать G-Code",
                f.read(),
                file_name=f"{name}.gcode",
                mime="text/plain",
                use_container_width=True,
            )

    # Детали
    with st.expander("Сгенерированный Blender-код"):
        st.code(generated_code, language="python")

    with st.expander("G-Code (первые 25 строк)"):
        with open(gcode_path, encoding="utf-8") as f:
            st.code("".join(f.readlines()[:25]), language="text")

# ── История ────────────────────────────────────────────────────────────────────

st.divider()
st.subheader("📁 Ранее созданные детали")

output_path = Path(config.OUTPUT_DIR)
if output_path.exists():
    stl_files = sorted(
        output_path.glob("*.stl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if stl_files:
        rows = []
        for f in stl_files[:20]:
            gcode = f.with_suffix(".gcode")
            rows.append({
                "Имя":     f.stem,
                "STL":     f"{f.stat().st_size // 1024} КБ",
                "G-Code":  f"{gcode.stat().st_size // 1024} КБ" if gcode.exists() else "—",
                "Создан":  datetime.datetime.fromtimestamp(
                               f.stat().st_mtime).strftime("%d.%m.%Y %H:%M"),
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption("Пока ничего не создано")
else:
    st.caption(f"Папка `{config.OUTPUT_DIR}` ещё не существует")
