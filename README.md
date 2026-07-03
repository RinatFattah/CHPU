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
- Blender 3.x или 4.x
- blender-mcp аддон (установка описана ниже)
- OpenAI API key или совместимый API-ключ (`OPENAI_BASE_URL`)
- для веб-интерфейса: Streamlit (входит в `requirements.txt`)
- для `stl_to_gcode.py`: FreeCAD не обязателен, есть fallback на простой генератор

## Установка

```bash
python -m venv .venv
. .venv/Scripts/activate   # Windows
# source .venv/bin/activate  # Linux / macOS
pip install -r requirements.txt
```

Создайте локальный конфиг:

```bash
copy config.example.yaml config.yaml   # Windows
# cp config.example.yaml config.yaml   # Linux / macOS
```

Заполните минимум в `config.yaml`:

- `OPENAI_API_KEY` — ключ от API (см. раздел ниже)
- `OPENAI_BASE_URL` — URL endpoint (см. раздел ниже)
- `OPENAI_MODEL` — имя модели
- `OUTPUT_DIR` — папка для STL и G-Code файлов

`config.yaml` уже исключён из git и предназначен для локальных секретов.

## Настройка Blender MCP

Blender MCP — это аддон для Blender, который открывает JSON-сокет (по умолчанию на порту 9876) и принимает Python-код для выполнения внутри Blender. Пайплайн общается с ним напрямую через этот сокет.

### Шаги установки

1. **Скачать аддон** — `blender-mcp` лежит на GitHub: <https://github.com/ahujasid/blender-mcp>
   Нужен файл `addon.py` из репозитория (или zip).

2. **Установить в Blender**:
   - Открыть Blender → `Edit` → `Preferences` → `Add-ons` → `Install`
   - Указать скачанный `addon.py`
   - Поставить галочку напротив аддона `Blender MCP`

3. **Запустить сервер** — в боковой панели Blender (клавиша `N`) появится вкладка `MCP`:
   - Нажать `Start MCP Server`
   - В консоли Blender увидите `MCP Server started on port 9876`

4. **Оставить Blender открытым** — пока сервер работает, Blender должен быть запущен. Его окно можно свернуть, но не закрывать.

### Проверка

После запуска сервера проверьте подключение:

```bash
python -c "import socket,json; s=socket.socket(); s.connect(('localhost',9876)); print('OK')"
```

Или через `/health` endpoint после запуска `server_3d.py`.

### Настройка порта

По умолчанию — `9876`. Если порт занят, измените в настройках аддона и синхронизируйте с конфигом:

```yaml
BLENDER_HOST: "localhost"
BLENDER_PORT: 9876
```

## OpenAI-совместимый API

Пайплайн не привязан к конкретному провайдеру — он использует стандартный OpenAI Chat Completions API. Любой endpoint, совместимый с форматом OpenAI, подойдёт: сам OpenAI, DeepSeek, LM Studio, vLLM, Azure OpenAI и другие.

### Конфигурация

```yaml
OPENAI_API_KEY: "sk-..."        # ключ от выбранного провайдера
OPENAI_BASE_URL: "https://api.openai.com/v1"  # или другой endpoint
OPENAI_MODEL: "gpt-4o"          # имя модели согласно API провайдера
```

### Примеры провайдеров

| Провайдер | `OPENAI_BASE_URL` | Примечание |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | дефолт, модели `gpt-4o`, `gpt-4.1` и др. |
| DeepSeek | `https://api.deepseek.com/v1` | модели `deepseek-chat`, `deepseek-v4-flash` |
| LM Studio (локально) | `http://localhost:1234/v1` | любой ключ подойдёт (`lm-studio`) |
| vLLM (локально) | `http://localhost:8000/v1` | имя модели из конфига vLLM |

### Reasoning-модели и OPENAI_MAX_TOKENS

Некоторые модели (DeepSeek R1, deepseek-v4-flash, OpenAI o1/o3) перед ответом генерируют внутренние рассуждения (`reasoning_content`). Эти рассуждения тратят токены из лимита `max_tokens`, и при малом значении на сам код токенов не остаётся — пайплайн получает пустой или обрезанный ответ.

**Важно:** для reasoning-моделей установите `OPENAI_MAX_TOKENS: 8000` или выше. Значение по умолчанию в `config.py` уже выставлено в 8000 с учётом этого.

```yaml
OPENAI_MAX_TOKENS: 8000   # reasoning-модели без этого возвращают пустой ответ
```

## Конфигурация

Приоритет значений:

1. дефолты в `config.py`
2. переменные окружения
3. YAML-файл, переданный через `--config`

Поддерживаются ключи:

| Ключ | По умолчанию | Описание |
|---|---|---|
| `OPENAI_API_KEY` | `""` | API-ключ (лучше через env) |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | endpoint |
| `OPENAI_MODEL` | `gpt-4o` | имя модели |
| `OPENAI_TEMPERATURE` | `0.1` | температура генерации |
| `OPENAI_MAX_TOKENS` | `8000` | лимит токенов (не занижать для reasoning-моделей) |
| `BLENDER_HOST` | `localhost` | хост Blender MCP |
| `BLENDER_PORT` | `9876` | порт Blender MCP |
| `SERVER_HOST` | `0.0.0.0` | хост FastAPI сервера |
| `SERVER_PORT` | `8765` | порт FastAPI сервера |
| `OUTPUT_DIR` | `~/details` | папка для STL и G-Code |
| `TOOL_DIAMETER` | `6.0` | диаметр фрезы, мм |
| `FEED_RATE` | `800` | рабочая подача, мм/мин |
| `SPINDLE_SPEED` | `12000` | скорость шпинделя, об/мин |
| `DEPTH_OF_CUT` | `1.0` | глубина за проход, мм |
| `SAFE_HEIGHT` | `10.0` | безопасная высота, мм |

`OUTPUT_DIR` поддерживает `~` и разворачивается в домашнюю директорию при загрузке конфига.

## Быстрый старт

### 1. Поднять Blender MCP

Открыть Blender с установленным аддоном, нажать `Start MCP Server` в боковой панели. Blender должен остаться открытым.

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

Если путь к конфигу содержит пробелы или кириллицу — обязательно в кавычках:

```bash
streamlit run app_streamlit.py -- --config "C:\Users\user\Documents\config.yaml"
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

## Известные грабли

| Проблема | Причина | Решение |
|---|---|---|
| Пустой ответ модели, ошибка «шаг 2» | Reasoning-модель потратила все токены на рассуждения | Поднять `OPENAI_MAX_TOKENS` до 8000+ |
| `bpy.ops.export_mesh.stl` не найден | Оператор удалён в Blender 4.1+ | Prompt уже содержит fallback на `wm.stl_export` |
| `~/details` не создаётся | `~` не разворачивается без `os.path.expanduser` | Исправлено в `config.load()`, не трогать |
| Кириллица в пути к конфигу «съедает» слеши | Shell интерпретирует `\` как escape без кавычек | Всегда заключать путь в кавычки |
| Streamlit: конфиг не загружается | Аргументы до `--` уходят в Streamlit, не в скрипт | Разделитель `--` перед `--config` обязателен |

## Рекомендуемый сценарий handoff коллеге

1. Установить Python-зависимости (`pip install -r requirements.txt`).
2. Установить Blender и аддон blender-mcp (см. раздел выше).
3. Скопировать `config.example.yaml` в `config.yaml`.
4. Прописать ключ OpenAI (или DeepSeek), `OPENAI_BASE_URL`, `OPENAI_MODEL` и `OUTPUT_DIR`.
5. Запустить Blender и стартовать MCP-сервер.
6. Запустить `python server_3d.py --config config.yaml`.
7. Проверить `GET /health` — должно быть `"blender": "✅"` и `"openai": "✅"`.
8. Сгенерировать тестовую деталь через `/make` или Streamlit.
9. Проверить STL и G-Code в `OUTPUT_DIR`.

## Ограничения и риски

- Качество результата сильно зависит от prompt и выбранной модели.
- Генерация Blender-кода ограничена инструкциями в prompt, а не строгой схемой.
- G-Code строится по габаритам STL и не учитывает сложную геометрию, стратегии обработки, крепёж, врезание и реальные CAM-ограничения.
- Автотестов в репозитории пока нет; базовая верификация сейчас сводится к `compileall` и ручному запуску entrypoint-ов.
- В `stl_to_gcode.py` ветка с FreeCAD обозначена как задел, но на практике чаще используется fallback-генератор.

## Что стоит улучшить дальше

- добавить smoke-тесты для API и CLI
- вынести повторяющуюся pipeline-логику в общий модуль
- отделить demo G-Code generator от полноценного CAM-контура
- формализовать контракт Blender MCP и ожидаемый формат ответа
