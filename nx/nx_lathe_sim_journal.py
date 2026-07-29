"""
nx_lathe_sim_journal.py — ТОКАРНАЯ симуляция в NX ISV. Выполняется ВНУТРИ NX
(через ugraf -auto), не в обычном Python. Токарный аналог nx_sim_journal.py.

Отличия от фрезерного журнала — все найдены разведкой NXOpen API:
  * setup создаётся как "turning" (не "mill_planar");
  * группа заготовки называется WORKPIECE_MAIN (не WORKPIECE); рядом лежат
    TURNING_WORKPIECE_MAIN, MCS_MAIN_SPINDLE и методы FACING/ROUGHING/...;
  * POCKET_01 у токарного станка НЕТ (там револьвер, а не карманы) — резец
    создаётся в GENERIC_MACHINE;
  * тип инструмента "OD_80_L" шаблона "turning" (наружный резец);
  * билдер берётся методом CreateMillToolBuilder (имя вводит в заблуждение,
    но объект возвращается TurnToolBuilder) и имеет NoseRadiusBuilder,
    NoseAngleBuilder, InsertPositionBuilder — то есть настоящий резец.

Остальное как во фрезерном: CSE не исполняется под run_journal, нужен живой
цикл событий GUI, поэтому запуск через ugraf -auto с прокачкой очереди
сообщений; результат съёма достаётся через SimulationOptionsBuilder
.SaveAsPartfile = True (лицензия ug_isv_full).

Параметры (env NX_LATHE_SIM_PARAMS, JSON): stock_step, mpf, machine,
nose_radius, tool_number, work_prt, log_path, sim_timeout.
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

_LOG_PATH = None


def log(msg):
    line = f"[nxlathe] {msg}"
    print(line, flush=True)
    if _LOG_PATH:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def assign_geometry(work_part, geometry, body):
    geometry.InitializeData(False)
    gset = geometry.GeometryList.FindItem(0)
    opts = work_part.ScRuleFactory.CreateRuleOptions()
    opts.SetSelectedFromInactive(False)
    rule = work_part.ScRuleFactory.CreateRuleBodyDumb([body], True, opts)
    opts.Dispose()
    gset.ScCollector.ReplaceRules([rule], False)


def assign_k_component(kin, name, body, parents=("SETUP",)):
    """Геометрия в K-компонент станка (PART/BLANK).

    Родитель у токарного станка иной, чем у фрезерного: там заготовка стоит на
    столе (SETUP), здесь — зажата в патроне на шпинделе, поэтому пробуем
    CHUCK_HOLDER/SPINDLE прежде, чем SETUP. Если деталь не привязана к
    вращающемуся узлу, ISV не считает съём и сохраняет заготовку целой.
    """
    parent = None
    for pname in parents:
        try:
            parent = kin.ComponentCollection.FindObject(pname)
            log(f"K-компонент {name}: родитель {pname}")
            break
        except Exception:
            continue
    try:
        comp = kin.ComponentCollection.FindObject(name)
    except Exception as e:
        log(f"warn: K-компонент {name} не найден ({e})")
        return
    if parent is None:
        log(f"warn: родитель для {name} не найден")
        return
    builder = kin.ComponentCollection.CreateComponentBuilder(parent, comp)
    try:
        builder.AddGeometry(body)
        builder.Commit()
        log(f"K-компонент {name}: геометрия назначена")
    except Exception as e:
        log(f"warn: K-компонент {name} не назначился: {e}")
    finally:
        builder.Destroy()


def find_ipw_prt(work_prt):
    import glob
    d = os.path.dirname(work_prt)
    stem = os.path.splitext(os.path.basename(work_prt))[0]
    hits = glob.glob(os.path.join(d, f"{stem}*_ipw.prt"))
    if not hits:
        hits = glob.glob(os.path.join(d, "*_ipw.prt"))
    return max(hits, key=os.path.getmtime) if hits else None


def first_existing(collection, names):
    """Первая из групп, которая нашлась (имена в turning отличаются от mill)."""
    for n in names:
        try:
            return collection.FindObject(n), n
        except Exception:
            continue
    return None, None


def main():
    global _LOG_PATH
    with open(os.environ["NX_LATHE_SIM_PARAMS"], encoding="utf-8") as f:
        p = json.load(f)
    _LOG_PATH = p.get("log_path")

    session = NXOpen.Session.GetSession()
    base = session.GetEnvironmentVariableValue("UGII_BASE_DIR")

    # ── 1. STEP заготовки → .prt ──
    imp = session.DexManager.CreateStep242Importer()
    imp.ImportTo = NXOpen.Step242Importer.ImportToOption.NewPart
    imp.SetMode(NXOpen.BaseImporter.Mode.NativeFileSystem)
    imp.SewSurfaces = True
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

    bodies = [b for b in work_part.Bodies if b.IsSolidBody]
    if not bodies:
        raise RuntimeError("после импорта STEP нет солида — поверхности не сшились")
    body = bodies[0]
    log(f"твёрдое тело найдено ({len(bodies)} шт.)")

    # ── 2. CAM-сессия, ТОКАРНЫЙ setup ──
    session.ApplicationSwitchImmediate("UG_APP_MANUFACTURING")
    session.IsCamSessionInitialized()
    session.CreateCamSession()
    session.CAMSession.SpecifyConfiguration(
        os.path.join(base, "mach", "resource", "configuration", "cam_general.dat"))
    setup = work_part.CreateCamSetup("turning")
    work_part.CreateKinematicConfigurator()
    log("CAM-проект создан (turning)")

    # ── 3. WORKPIECE_MAIN: деталь И заготовка = наше тело ──
    wp, wp_name = first_existing(setup.CAMGroupCollection,
                                 ["WORKPIECE_MAIN", "WORKPIECE"])
    if wp is None:
        raise RuntimeError("группа заготовки не найдена (ни WORKPIECE_MAIN, ни WORKPIECE)")
    geom_builder = setup.CAMGroupCollection.CreateMillGeomBuilder(wp)
    assign_geometry(work_part, geom_builder.PartGeometry, body)
    assign_geometry(work_part, geom_builder.BlankGeometry, body)
    geom_builder.Commit()
    geom_builder.Destroy()
    log(f"{wp_name}: деталь и заготовка назначены")

    # ── 4. Станок из библиотеки ──
    generic_machine = setup.CAMGroupCollection.FindObject("GENERIC_MACHINE")
    machine_builder = setup.CAMGroupCollection.CreateMachineGroupBuilder(generic_machine)
    mount = setup.CreateNcmctPartMountingBuilder(p["machine"])
    mount.CreateMachineSpindleObjects = False
    # Способ постановки детали на станок. Для ФРЕЗЕРНОГО подходил
    # OrientMachineZeroToMainMcs (деталь на стол), для ТОКАРНОГО деталь надо
    # зажать в патроне — иначе она остаётся в начале координат, резец ездит по
    # программе внутри станка мимо неё, и IPW сохраняется нетронутым.
    pos_name = p.get("positioning", "OrientMachineZeroToMainMcs")
    try:
        mount.Positioning = getattr(
            NXOpen.CAM.NcmctPartMountingBuilder.PositioningTypes, pos_name)
        log(f"постановка детали: {pos_name}")
    except Exception as e:
        log(f"warn: positioning={pos_name} не применился ({e}), беру дефолт")
        mount.Positioning = (NXOpen.CAM.NcmctPartMountingBuilder
                             .PositioningTypes.OrientMachineZeroToMainMcs)
    mount.Commit()
    machine_builder.ReplaceMachine(
        NXOpen.CAM.MachineGroupBuilder.RetrieveToolPocketInformation.Yes, mount)
    mount.Destroy()
    machine_builder.Destroy()
    log(f"станок подключён: {p['machine']}")

    # ── 5. Резец: у токарного станка карманов нет, создаём в GENERIC_MACHINE ──
    parent, parent_name = first_existing(setup.CAMGroupCollection,
                                         ["POCKET_01", "GENERIC_MACHINE"])
    tool = setup.CAMGroupCollection.CreateToolWithUserName(
        parent, "turning", "OD_80_L",
        NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue, "TURN_OD", "TurnOD")
    tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)   # → TurnToolBuilder
    applied = []
    for prop, val in (("NoseRadiusBuilder", float(p.get("nose_radius", 0.4))),
                      ("TlNumberBuilder", int(p.get("tool_number", 1))),
                      ("NoseAngleBuilder", float(p.get("nose_angle", 80.0))),
                      ("InsertLengthBuilder", float(p.get("insert_length", 12.0)))):
        try:
            getattr(tb, prop).Value = val
            applied.append(f"{prop}={val}")
        except Exception as e:
            log(f"warn: {prop}={val} не применилось: {e}")
    tb.Commit()
    tb.Destroy()
    log(f"резец OD_80_L в {parent_name}: {', '.join(applied)}")

    # ── 6. K-компоненты PART/BLANK ──
    kin = work_part.KinematicConfigurator
    parents = tuple(p.get("k_parents", ["CHUCK_HOLDER", "SPINDLE", "SETUP"]))
    assign_k_component(kin, "PART", body, parents)
    assign_k_component(kin, "BLANK", body, parents)

    # ── 7. Симуляция машинного кода (CSE) со съёмом ──
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

    # Форма инструмента для СЪЁМА. По умолчанию ISV ждёт Assembly — твердотельную
    # сборку инструмента, которой у параметрически созданного резца нет; тогда
    # резать нечем, и IPW сохраняется равным необработанной заготовке (ровно
    # этот симптом и наблюдался: объём IPW = объёму прутка, 454 треугольника).
    # Parameter заставляет строить форму по параметрам резца (радиус при
    # вершине, угол, размер пластины, державка).
    for prop, enum_name in (("Mrshape", "SimulationOptionsBuilderToolShapeMR"),
                            ("ToolShape", "SimulationOptionsBuilderToolShapeType"),
                            ("Collshape", "SimulationOptionsBuilderToolShapeColl")):
        try:
            before = getattr(so, prop)
            setattr(so, prop, getattr(NXOpen.CAM, enum_name).Parameter)
            log(f"{prop}: {before} → Parameter")
        except Exception as e:
            log(f"warn: {prop} → Parameter: {type(e).__name__}: {str(e)[:90]}")

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
    started = time.time()
    # Если стойка не приняла программу, машинное время так и остаётся нулём, и
    # ждать полный таймаут бессмысленно: даём ей startup_grace секунд и падаем
    # с понятной ошибкой, а не через 25 минут молчания.
    grace = float(p.get("startup_grace", 90))
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
        if (t in ("", "00:00:00.000")) and time.time() - started > grace:
            raise RuntimeError(
                f"за {grace:.0f} с стойка не начала исполнять программу "
                f"(машинное время 0). Обычно причина в .mpf: смена инструмента, "
                f"рабочая СК (G54) или таблица TO_INI.SPF")
    mtime = machine_time()
    if mtime in ("", "00:00:00.000"):
        raise RuntimeError("стойка не исполнила программу (машинное время 0) — "
                           "проверьте синтаксис .mpf и таблицу инструментов")
    log(f"машинное время: {mtime}")

    # ── 8. Выход из симуляции, сохранение, поиск IPW ──
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
        raise RuntimeError("ISV не сохранил IPW (*_ipw.prt) — проверьте, что съём "
                           "реально шёл и доступна лицензия ug_isv_full")
    log(f"DONE ipw={ipw} machine_time={mtime}")


if os.environ.get("NX_LATHE_SIM_PARAMS"):
    try:
        main()
    except Exception:
        import traceback
        log("ERROR:\n" + traceback.format_exc())
    os._exit(0)
