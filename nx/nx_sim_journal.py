"""
nx_sim_journal.py — выполняется ВНУТРИ Siemens NX (через ugraf -auto), не в
обычном Python. Собирает CAM-проект, подключает виртуальный станок, прогоняет
внешний G-Code (.mpf) через стойку (CSE) со съёмом материала и — благодаря
SimulationOptionsBuilder.SaveAsPartfile=True — ISV САМ сохраняет обработанную
заготовку (IPW) отдельным .prt-файлом рядом с рабочим. Журнал находит этот
файл, пишет его путь маркером DONE и закрывает NX.

КЛЮЧЕВОЕ: SaveAsPartfile — единственный НАДЁЖНЫЙ способ достать результат съёма
через API. Кнопка ленты «Создать фасетное тело для ЗвПО» (UG_CAM_ISV_EXPORT_IPW)
программного эквивалента НЕ имеет (проверено двумя записанными журналами — оба
показывают её как комментарий без вызовов API), а лента NX на Qt не видна
Windows UI Automation. SaveAsPartfile обходит и то, и другое.

Batch-запуск (run_journal) НЕ исполняет CSE — движку нужен живой событийный цикл
GUI; поэтому запуск идёт через ugraf -auto, а журнал во время прогона прокачивает
очередь сообщений Windows. Прочие грабли — см. guide.md (WORKPIECE требует и
деталь, и заготовку; K-компоненты PART/BLANK заполняются явно; MCS не трогаем —
заготовка уже в координатах программы).

Параметры (env NX_SIM_PARAMS, JSON): stock_step, mpf, machine, tool_diameter,
tool_number, work_prt, log_path, sim_timeout.
"""

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import NXOpen
import NXOpen.CAM
import NXOpen.SIM

_LOG_PATH = None  # stdout GUI-процесса не виден хосту — пишем маркеры в файл


def log(msg):
    line = f"[nxsim] {msg}"
    print(line, flush=True)
    if _LOG_PATH:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def solid_bodies(part):
    return [b for b in part.Bodies if b.IsSolidBody]


def assign_geometry(work_part, geometry, body):
    """Назначает тело в секцию геометрии WORKPIECE (деталь или заготовка)."""
    geometry.InitializeData(False)
    gset = geometry.GeometryList.FindItem(0)
    opts = work_part.ScRuleFactory.CreateRuleOptions()
    opts.SetSelectedFromInactive(False)
    rule = work_part.ScRuleFactory.CreateRuleBodyDumb([body], True, opts)
    opts.Dispose()
    gset.ScCollector.ReplaceRules([rule], False)


def assign_k_component(kin, name, body):
    """Геометрия в K-компонент станка (PART/BLANK): без этого стойка отвечает
    «No part and blank geometry has been specified»."""
    try:
        setup_comp = kin.ComponentCollection.FindObject("SETUP")
        comp = kin.ComponentCollection.FindObject(name)
    except Exception as e:
        log(f"warn: K-компонент {name} не найден ({e})")
        return
    builder = kin.ComponentCollection.CreateComponentBuilder(setup_comp, comp)
    try:
        builder.AddGeometry(body)
        builder.Commit()
        log(f"K-компонент {name}: геометрия назначена")
    except Exception as e:
        log(f"warn: K-компонент {name} не назначился: {e}")
    finally:
        builder.Destroy()


def find_ipw_prt(work_prt):
    """Находит .prt обработанной заготовки, который ISV сохранил рядом с рабочим
    (имя вида <stem>_<n>_ipw.prt). Возвращает путь к новейшему или None."""
    import glob
    d = os.path.dirname(work_prt)
    stem = os.path.splitext(os.path.basename(work_prt))[0]
    hits = glob.glob(os.path.join(d, f"{stem}*_ipw.prt"))
    if not hits:
        hits = glob.glob(os.path.join(d, "*_ipw.prt"))
    return max(hits, key=os.path.getmtime) if hits else None


def main():
    global _LOG_PATH
    with open(os.environ["NX_SIM_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)
    _LOG_PATH = p.get("log_path")

    session = NXOpen.Session.GetSession()
    base = session.GetEnvironmentVariableValue("UGII_BASE_DIR")

    # ── 1. STEP заготовки → новый .prt (сшивание поверхностей в солид) ──
    imp = session.DexManager.CreateStep242Importer()
    imp.ImportTo = NXOpen.Step242Importer.ImportToOption.NewPart
    imp.SetMode(NXOpen.BaseImporter.Mode.NativeFileSystem)
    imp.SewSurfaces = True          # листовые тела стойка молча отбраковывает
    imp.Optimize = True
    imp.ObjectTypes.Solids = True
    imp.ObjectTypes.Surfaces = True
    imp.SettingsFile = os.path.join(base, "translators", "step242", "step242ug.def")
    imp.InputFile = p["stock_step"]
    imp.OutputFile = p["work_prt"]
    imp.FileOpenFlag = False
    imp.ProcessHoldFlag = True
    imp.Commit()
    imp.Destroy()
    log(f"заготовка импортирована: {os.path.basename(p['work_prt'])}")

    part, status = session.Parts.OpenActiveDisplay(
        p["work_prt"], NXOpen.DisplayPartOption.AllowAdditional)
    status.Dispose()
    work_part = session.Parts.Work

    bodies = solid_bodies(work_part)
    if not bodies:
        raise RuntimeError("после импорта STEP нет твёрдого тела (солида) — "
                           "поверхности не сшились; проверьте файл заготовки")
    body = bodies[0]
    log(f"твёрдое тело найдено ({len(bodies)} шт., взято первое)")

    # ── 2. CAM-сессия и проект ──
    session.ApplicationSwitchImmediate("UG_APP_MANUFACTURING")
    session.IsCamSessionInitialized()
    session.CreateCamSession()
    session.CAMSession.SpecifyConfiguration(
        os.path.join(base, "mach", "resource", "configuration", "cam_general.dat"))
    setup = work_part.CreateCamSetup("mill_planar")
    work_part.CreateKinematicConfigurator()
    log("CAM-проект создан (mill_planar)")

    # MCS_MAIN не трогаем: заготовка уже в координатах программы.

    # ── 3. WORKPIECE: деталь И заготовка = наше тело ──
    workpiece = setup.CAMGroupCollection.FindObject("WORKPIECE")
    geom_builder = setup.CAMGroupCollection.CreateMillGeomBuilder(workpiece)
    assign_geometry(work_part, geom_builder.PartGeometry, body)
    assign_geometry(work_part, geom_builder.BlankGeometry, body)
    geom_builder.Commit()
    geom_builder.Destroy()
    log("WORKPIECE: деталь и заготовка назначены")

    # ── 4. Станок из библиотеки, ноль станка → в MCS ──
    generic_machine = setup.CAMGroupCollection.FindObject("GENERIC_MACHINE")
    machine_builder = setup.CAMGroupCollection.CreateMachineGroupBuilder(generic_machine)
    mount = setup.CreateNcmctPartMountingBuilder(p["machine"])
    mount.CreateMachineSpindleObjects = False
    mount.Positioning = \
        NXOpen.CAM.NcmctPartMountingBuilder.PositioningTypes.OrientMachineZeroToMainMcs
    mount.Commit()
    machine_builder.ReplaceMachine(
        NXOpen.CAM.MachineGroupBuilder.RetrieveToolPocketInformation.Yes, mount)
    mount.Destroy()
    machine_builder.Destroy()
    log(f"станок подключён: {p['machine']}")

    # ── 5. Инструмент в кармане POCKET_01 (смена берёт тело из кармана по T) ──
    pocket = setup.CAMGroupCollection.FindObject("POCKET_01")
    tool = setup.CAMGroupCollection.CreateToolWithUserName(
        pocket, "mill_planar", "MILL",
        NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue, "MILL", "Mill")
    tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
    tb.TlDiameterBuilder.Value = float(p["tool_diameter"])
    for prop, val in (("TlNumberBuilder", int(p["tool_number"])),
                      ("TlHeightBuilder", 75.0)):
        try:
            getattr(tb, prop).Value = val
        except Exception as e:
            log(f"warn: {prop}={val} не применилось: {e}")
    tb.Commit()
    tb.Destroy()
    log(f"инструмент: фреза Ø{p['tool_diameter']} в POCKET_01, T{p['tool_number']}")

    # ── 6. K-компоненты PART/BLANK ──
    kin = work_part.KinematicConfigurator
    assign_k_component(kin, "PART", body)
    assign_k_component(kin, "BLANK", body)

    # ── 7. Симуляция машинного кода (CSE) со съёмом материала + SaveAsPartfile ──
    session.BeginTaskEnvironment()
    channels = kin.CreateNcChannelSelectionData()
    pm = kin.CreateNcProgramManagerBuilder()
    src = pm.GetExternalFileSource()
    pm.Destroy()
    prog = src.AddMainProgram("Main", p["mpf"])
    channels.AssignProgram("Main", prog)
    cpb = kin.CreateIsvControlPanelBuilder(
        NXOpen.SIM.IsvControlPanelBuilder.VisualizationType.MachineCodeSimulateCse,
        channels)
    so = cpb.SimulationOptionsBuilder
    so.EnableMaterialRemoval = True
    try:
        so.EnableIpw = NXOpen.CAM.SimulationOptionsBuilderIpwEnable.MotionBased
    except Exception as e:
        log(f"warn: EnableIpw: {e}")
    # КЛЮЧЕВОЕ: ISV сам сохранит вырезанный IPW отдельным .prt в конце прогона
    so.SaveAsPartfile = True
    so.Commit()
    cpb.ApplySimulationOptions()
    cpb.SetSpeed(10)

    import time
    done = {"end": False}

    def _on_sim_end(*args):
        done["end"] = True

    try:
        cpb.AddSimEnd(_on_sim_end)
    except Exception as e:
        log(f"warn: AddSimEnd: {e}")

    def machine_time():
        try:
            return str(cpb.MachineTime)
        except Exception:
            return ""

    # CSE двигается событиями оконной очереди — качаем её, пока журнал ждёт
    import ctypes

    class _MSG(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_size_t),
                    ("time", ctypes.c_uint), ("pt_x", ctypes.c_long),
                    ("pt_y", ctypes.c_long)]

    _user32 = ctypes.windll.user32

    def pump_messages(seconds):
        msg = _MSG()
        end = time.time() + seconds
        while time.time() < end:
            while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.05)

    with open(p["mpf"], encoding="ascii", errors="replace") as f:
        n_lines = sum(1 for _ in f)
    log(f"исполнение программы ({n_lines} строк)...")
    cpb.PlayForward()

    deadline = time.time() + float(p.get("sim_timeout", 1500))
    last, last_change = machine_time(), time.time()
    while time.time() < deadline:
        if done["end"]:
            log("событие SimEnd")
            break
        pump_messages(2)
        t = machine_time()
        if t != last:
            last, last_change = t, time.time()
        elif t not in ("", "00:00:00.000") and time.time() - last_change > 20:
            log("машинное время стабилизировалось")
            break
    mtime = machine_time()
    if mtime in ("", "00:00:00.000"):
        raise RuntimeError("стойка не исполнила программу (машинное время 0) — "
                           "см. синтаксис .mpf и TO_INI")
    log(f"машинное время: {mtime}")

    # ── 8. Выйти из симуляции, сохранить (это фиксирует IPW-файл), найти его ──
    try:
        cpb.Destroy()
        session.DeleteUndoMarksSetInTaskEnvironment()
        session.EndTaskEnvironment()
    except Exception as e:
        log(f"warn: выход из среды симуляции: {e}")
    try:
        sv = part.Save(NXOpen.BasePart.SaveComponents.TrueValue,
                       NXOpen.BasePart.CloseAfterSave.FalseValue)
        sv.Dispose()
    except Exception as e:
        log(f"warn: сохранение work part: {e}")

    ipw = find_ipw_prt(p["work_prt"])
    if not ipw:
        raise RuntimeError("ISV не сохранил IPW-файл (*_ipw.prt) — проверьте, "
                           "что съём материала реально шёл и лицензия ug_isv_full "
                           "доступна")
    log(f"DONE ipw={ipw} machine_time={mtime}")


if os.environ.get("NX_SIM_PARAMS"):
    try:
        main()
    except Exception:
        import traceback
        log("ERROR:\n" + traceback.format_exc())
    os._exit(0)   # NX закрываем в любом случае — данные (если есть) уже на диске
