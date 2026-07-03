# AI 3D Pipeline

Пайплайн для генерации простой 3D-детали по текстовому описанию:

`текст -> Blender Python -> Blender MCP -> STL -> G-Code`

Репозиторий собран как рабочий стенд для быстрого прототипирования деталей под фрезерование. Основная идея: LLM генерирует `bpy`-скрипт, Blender строит STL, после чего пайплайн формирует базовый G-Code.

## Что внутри

- `server_3d.py` - FastAPI API-сервер (`/health`, `/make`, `/list`, скачивание STL/G-Code)
- `app_streamlit.py` - Streamlit UI для ручной работы через браузер
- `auto_3d.py` - интерактивный CLI: текст -> STL
- `pipeline_3d.py` - интерактивный CLI: текст -> STL -> G-Code
- `stl_to_gcode.py` - standalone-конвертер STL -> G-Code
- `config.py` - дефолтные настройки и загрузка YAML-конфига
- `config.example.yaml` - шаблон локального конфига
- `AI_3D_Pipeline_Standup_Guide.docx` - исходная развёрнутая документация

## Архитектура

1. Пользователь задаёт описание детали.
2. Скрипт отправляет prompt в OpenAI API или совместимый endpoint.
3. LLM возвращает Python-код для Blender (`bpy`).
4. Код выполняется в Blender через `blender-mcp` по сокету (`host:port` из конфига).
5. Blender экспортирует STL.
6. Пайплайн читает геометрию STL и строит простой контурный G-Code по габаритам модели.

Важно: G-Code здесь не является полноценным CAM-процессингом. Это базовая генерация по bounding box модели, подходящая для демонстрации и простых сценариев, но не для сложной производственной обработки без дополнительной проверки.

## Требования

- Python 3.10+
- установленный Blender
- запущенный `blender-mcp` аддон/сервер на порту `9876` (по умолчанию)
- OpenAI API key или совместимый API (`OPENAI_BASE_URL`)
- для веб-интерфейса: Streamlit
- для `stl_to_gcode.py`: FreeCAD не обязателен, есть fallback на простой генератор

## Установка

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
```

Создайте локальный конфиг:

```bash
copy config.example.yaml config.yaml
```

Заполните минимум:

- `OPENAI_API_KEY`
- при необходимости `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `OUTPUT_DIR`

`config.yaml` уже исключён из git и предназначен для локальных секретов.

## Конфигурация

Приоритет значений:

1. дефолты в `config.py`
2. переменные окружения
3. YAML-файл, переданный через `--config`

Поддерживаются ключи:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `OPENAI_TEMPERATURE`
- `OPENAI_MAX_TOKENS`
- `BLENDER_HOST`
- `BLENDER_PORT`
- `SERVER_HOST`
- `SERVER_PORT`
- `OUTPUT_DIR`
- `TOOL_DIAMETER`
- `FEED_RATE`
- `SPINDLE_SPEED`
- `DEPTH_OF_CUT`
- `SAFE_HEIGHT`

`OUTPUT_DIR` поддерживает `~` и разворачивается в домашнюю директорию при загрузке конфига.

## Быстрый старт

### 1. Поднять Blender MCP

Нужно открыть Blender с подключённым `blender-mcp` и убедиться, что он слушает порт `9876`.

### 2. Запустить API

```bash
python server_3d.py --config config.yaml
```

Проверка:

```bash
curl http://localhost:8765/health
```

Swagger:

- `http://localhost:8765/docs`

### 3. Сделать первую генерацию

```bash
curl -X POST http://localhost:8765/make ^
  -H "Content-Type: application/json" ^
  -d "{\"description\":\"Пластина 100x50x5 мм с 4 отверстиями d6 мм по углам\",\"name\":\"plate_demo\"}"
```

### 4. Скачать результаты

- `GET /file/stl/{name}`
- `GET /file/gcode/{name}`
- `GET /list`

## Другие способы запуска

### Streamlit UI

```bash
streamlit run app_streamlit.py -- --config config.yaml
```

Что умеет:

- загрузка YAML-конфига из UI
- проверка доступности Blender и OpenAI
- запуск генерации
- скачивание STL и G-Code
- просмотр последних сгенерированных деталей

### CLI: текст -> STL

```bash
python auto_3d.py --config config.yaml
```

### CLI: текст -> STL -> G-Code

```bash
python pipeline_3d.py --config config.yaml
```

### CLI: готовый STL -> G-Code

```bash
python stl_to_gcode.py part.stl part.gcode --config config.yaml
```

## API

### `GET /health`

Проверяет:

- доступность Blender MCP
- наличие `OPENAI_API_KEY`
- текущую модель

### `POST /make`

Тело запроса:

```json
{
  "description": "Пластина 100x50x5 мм с 4 отверстиями d6 мм по углам",
  "name": "plate_demo"
}
```

Ответ содержит:

- имя детали
- путь к STL
- путь к G-Code
- размер STL
- размер G-Code
- число строк G-Code
- длину сгенерированного Blender-кода

### `GET /file/stl/{name}`

Скачивание STL.

### `GET /file/gcode/{name}`

Скачивание G-Code.

### `GET /list`

Список ранее созданных деталей из `OUTPUT_DIR`.

## Ограничения и риски

- Качество результата сильно зависит от prompt и выбранной модели.
- Генерация Blender-кода ограничена инструкциями в prompt, а не строгой схемой.
- G-Code строится по габаритам STL и не учитывает сложную геометрию, стратегии обработки, крепёж, врезание и реальные CAM-ограничения.
- Автотестов в репозитории пока нет; базовая верификация сейчас сводится к `compileall` и ручному запуску entrypoint-ов.
- В `stl_to_gcode.py` ветка с FreeCAD обозначена как задел, но на практике чаще используется fallback-генератор.

## Рекомендуемый сценарий handoff коллеге

1. Установить Python-зависимости.
2. Скопировать `config.example.yaml` в `config.yaml`.
3. Прописать ключ OpenAI и путь для выходных файлов.
4. Проверить, что Blender MCP запущен.
5. Запустить `server_3d.py --config config.yaml`.
6. Проверить `GET /health`.
7. Сгенерировать тестовую деталь через `/make` или Streamlit.
8. Проверить STL и G-Code в `OUTPUT_DIR`.

## Что стоит улучшить дальше

- добавить smoke-тесты для API и CLI
- вынести повторяющуюся pipeline-логику в общий модуль
- отделить demo G-Code generator от полноценного CAM-контура
- формализовать контракт Blender MCP и ожидаемый формат ответа
