"""
config.py — Конфигурация AI 3D Pipeline

Чувствительные данные (API-ключ) передаются через переменные окружения.
Всё остальное задаётся здесь и применяется во всех скриптах pipeline.
"""

import os

# ── OpenAI API ──────────────────────────────────────────────────────────────────
# Ключ берётся из переменной окружения: export OPENAI_API_KEY=sk-...
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
# base_url позволяет использовать совместимые API (LM Studio, vLLM, Azure и др.)
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL       = "gpt-4o"
OPENAI_TEMPERATURE = 0.1
OPENAI_MAX_TOKENS  = 1500

# ── Blender MCP ─────────────────────────────────────────────────────────────────
BLENDER_HOST = "localhost"
BLENDER_PORT = 9876

# ── API Server ──────────────────────────────────────────────────────────────────
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8765
# Папка для сохранения STL и G-Code файлов
OUTPUT_DIR  = os.path.join(os.path.expanduser("~"), "details")

# ── G-Code / параметры фрезерования ────────────────────────────────────────────
TOOL_DIAMETER  = 6.0    # мм — диаметр концевой фрезы
FEED_RATE      = 800    # мм/мин — рабочая подача
SPINDLE_SPEED  = 12000  # об/мин — скорость шпинделя
DEPTH_OF_CUT   = 1.0    # мм — глубина за один проход
SAFE_HEIGHT    = 10.0   # мм — безопасная высота при холостых перемещениях
