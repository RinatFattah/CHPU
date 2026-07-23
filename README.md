# CAM: CAD-модель → G-Code (FreeCAD)

Генерация управляющей программы (G-Code) для 3-осевого фрезерного станка с ЧПУ
**по готовой CAD-модели детали**. Траектория на вход не подаётся — она строится
по модели. Стратегия — **3D-обработка по поверхности**: фреза следует за фактической
геометрией (наклоны, конусы, купола, рельеф).

```
деталь.prt (Siemens NX)
   │  экспорт из NX: File → Export → STEP (AP214/AP242)
   ▼
деталь.step ──► python run_cam.py деталь.step ──► деталь.gcode
                     (FreeCAD Path/CAM, headless)
```

Siemens NX `.prt` — закрытый формат, FreeCAD его не читает; мост — экспорт STEP из NX
(одна операция, геометрия без потерь).

## Требования

- Python 3.10+ и `pyyaml` (`pip install -r requirements.txt`)
- **FreeCAD 1.0+** — CAM-движок (установка ниже)

## Установка FreeCAD (AppImage, без root)

FreeCAD **отсутствует в репозиториях Ubuntu 24.04**, а snap-версия ломается в headless
из-за рассинхрона Qt. Рабочий способ — официальный AppImage:

```bash
mkdir -p ~/freecad-appimage && cd ~/freecad-appimage
# взять свежий Linux-x86_64 AppImage со страницы https://github.com/FreeCAD/FreeCAD/releases
wget https://github.com/FreeCAD/FreeCAD/releases/download/1.1.1/FreeCAD_1.1.1-Linux-x86_64-py311.AppImage
chmod +x FreeCAD_*.AppImage
./FreeCAD_*.AppImage --appimage-extract     # распаковка в squashfs-root/
```

`freecadcmd` окажется в `~/freecad-appimage/squashfs-root/usr/bin/freecadcmd` — этот
путь ищется автоматически. Другое расположение — укажите в конфиге: `FREECAD_CMD: "/путь"`.

Проверка:

```bash
python -c "import freecad_cam; print(freecad_cam.find_freecadcmd())"
```

## Быстрый старт

```bash
cp config.example.yaml config.yaml   # и подставить свою фрезу/подачу/стойку

python run_cam.py деталь.step --config config.yaml
# → деталь.gcode рядом с моделью
```

Флаги CLI: `--config FILE`, `--origin corner-top|center-top|model` (ноль программы),
`--rough MM` (припуск; `0` = черновая до номинала), `--no-rough` (без черновой),
`--no-finish`; для мешей — `--mm` / `--meters` / `--scale N`.

## Параметры

Все параметры генерации (инструмент, режимы резания, стратегия, ноль программы,
постпроцессор) задаются в `config.yaml`. **Полный справочник с рекомендациями —
[README_CAM.md](README_CAM.md).** Минимум, который нужно выставить под свой станок:

```yaml
TOOL_DIAMETER: 6.0        # фреза, которая реально стоит в шпинделе
FEED_RATE: 800            # подача под материал
SPINDLE_SPEED: 12000
POSTPROCESSOR: "grbl"     # диалект стойки: grbl / linuxcnc / fanuc / mach3_mach4 …
```

## Как это устроено

| Файл | Роль |
|---|---|
| `run_cam.py` | CLI: модель → G-Code |
| `freecad_cam.py` | хост: находит `freecadcmd`, передаёт параметры, разбирает результат |
| `freecad_worker.py` | исполняется внутри FreeCAD: модель → тело → Path Job → Surface → постпроцессор |
| `config.py` | дефолты параметров + загрузка YAML |

FreeCAD работает **отдельным headless-процессом** — его Qt/OpenCASCADE не грузятся
в основной Python. Ноль программы по умолчанию: X0 Y0 = угол габарита детали,
Z0 = её верхняя плоскость (`ORIGIN: corner-top`).

## Проверка перед станком

1. Прогнать G-Code в симуляторе: [CAMotics](https://camotics.org) или
   [ncviewer.com](https://ncviewer.com) — траектория, глубины, зарезы.
2. Сверить `POSTPROCESSOR` с контроллером станка.
3. Выставить ноль на станке по соглашению `ORIGIN` (угол детали, Z0 = верх).
4. Контроля столкновений нет — пробный проход над заготовкой обязателен.

## Ограничения

- 3 оси, обработка сверху: поднутрения, обратная сторона, боковые элементы — недоступны.
- Один инструмент на программу; черновая и чистовая не разделяются.
- Оснастка (прижимы) не моделируется; заготовка = габарит модели.
- Это автоматический генератор для типовых деталей, а не замена профессиональной
  CAM-системы (NX, T-FLEX): многоось, стратегии, контроль столкновений — там.
