"""
config.py — Конфигурация AI 3D Pipeline

Приоритет значений (от низшего к высшему):
  1. Дефолты в этом файле
  2. Переменные окружения (OPENAI_API_KEY, OPENAI_BASE_URL)
  3. YAML-файл конфига, переданный через --config

Формат YAML-файла (все ключи необязательны), см. config.example.yaml.
"""

import os
import sys

# ── OpenAI API ──────────────────────────────────────────────────────────────────
# Ключ берётся из переменной окружения: export OPENAI_API_KEY=sk-...
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
# base_url позволяет использовать совместимые API (LM Studio, vLLM, Azure и др.)
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL       = "gpt-4o"
OPENAI_TEMPERATURE = 0.1
# ВАЖНО: reasoning-модели (deepseek-reasoner, deepseek-v4-flash, o1 и т.п.) тратят
# основную часть бюджета на внутренние рассуждения. При малом лимите на сам код
# не остаётся токенов → пустой или обрезанный ответ. Держите запас.
OPENAI_MAX_TOKENS  = 8000

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


def load(path: str) -> None:
    """Загружает конфиг из YAML-файла, переопределяя текущие значения."""
    import yaml
    module = sys.modules[__name__]
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    unknown = []
    for key, value in data.items():
        if hasattr(module, key) and not key.startswith("_"):
            setattr(module, key, value)
        else:
            unknown.append(key)
    if unknown:
        print(f"[config] Неизвестные ключи (проигнорированы): {', '.join(unknown)}")

    # Раскрываем ~ в путях: значения из YAML не проходят через expanduser сами.
    module.OUTPUT_DIR = os.path.expanduser(module.OUTPUT_DIR)
