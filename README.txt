AI 3D Pipeline — Быстрый старт
================================

Состав архива:
  server_3d.py        — основной API-сервер (FastAPI, порт 8765)
  auto_3d.py          — интерактивный CLI: текст → STL
  pipeline_3d.py      — CLI: текст → STL + G-Code
  stl_to_gcode.py     — конвертер STL → G-Code (standalone)
  requirements.txt    — Python-зависимости
  AI_3D_Pipeline_Standup_Guide.docx — полная документация

Запуск стенда:
--------------
1. pip install -r requirements.txt
2. Открыть Blender с аддоном blender-mcp (порт 9876)
3. python3 server_3d.py
4. curl http://localhost:8765/health

Подробнее — см. документацию в .docx файле.
