"""
nx_lathe_sim_journal.py — ТОКАРНАЯ симуляция в NX ISV. Выполняется ВНУТРИ NX
(через ugraf -auto), не в обычном Python. Токарный аналог nx_sim_journal.py.

Отличия от фрезерного журнала — все найдены разведкой NXOpen API:
  * setup создаётся как "turning" (не "mill_planar");
  * группа заготовки называется WORKPIECE_MAIN (не WORKPIECE); рядом лежат
    TURNING_WORKPIECE_MAIN, MCS_MAIN_SPINDLE и методы FACING/ROUGHING/...;
  * резец создаётся в кармане револьвера POCKET_01 (если его нет — в
    GENERIC_MACHINE) и НАСЛЕДУЕТ оттуда геометрию;
  * тип инструмента — наружный резец шаблона "turning"; ПОДТИП определяет форму
    съёма, и от него напрямую зависит зарез — см. блок 5;
  * билдер берётся методом CreateMillToolBuilder (имя вводит в заблуждение,
    но объект возвращается TurnToolBuilder) и имеет NoseRadiusBuilder,
    NoseAngleBuilder, InsertPositionBuilder — то есть настоящий резец.

Остальное как во фрезерном: CSE не исполняется под run_journal, нужен живой
цикл событий GUI, поэтому запуск через ugraf -auto с прокачкой очереди
сообщений; результат съёма достаётся через SimulationOptionsBuilder
.SaveAsPartfile = True (лицензия ug_isv_full).

Параметры (env NX_LATHE_SIM_PARAMS, JSON): stock_step, mpf, machine, work_prt,
log_path, sim_timeout, positioning; резец — tool_subtype, tool_number,
nose_radius, nose_angle, insert_size, relief_angle, orient_angle, tool_params.
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


def find_ipw_prt(work_prt, since):
    """IPW ЭТОГО прогона: файл своего стема и не старше начала прогона.

    Фолбэк «взять любой *_ipw.prt» был опасен: когда ISV ничего не сохранил,
    подбирался файл соседнего прогона, экспортный журнал крутил его фасеты
    ещё раз и на выход шёл мусор, выглядящий как результат. Лучше честно
    упасть.
    """
    import glob
    d = os.path.dirname(work_prt)
    stem = os.path.splitext(os.path.basename(work_prt))[0]
    hits = [f for f in glob.glob(os.path.join(d, f"{stem}*_ipw.prt"))
            if os.path.getmtime(f) >= since - 1.0]
    return max(hits, key=os.path.getmtime) if hits else None


def first_existing(collection, names):
    """Первая из групп, которая нашлась (имена в turning отличаются от mill)."""
    for n in names:
        try:
            return collection.FindObject(n), n
        except Exception:
            continue
    return None, None


# Карманы револьвера. Резец создаётся В КАРМАНЕ и наследует оттуда геометрию;
# в занятый карман второй инструмент не встаёт («Input parameter is out of
# range»), поэтому для T2 ищется следующий свободный.
POCKETS = [f"POCKET_{i:02d}" for i in range(1, 13)] + ["GENERIC_MACHINE"]


def make_tool(setup, subtype, uname, props, used, report, pocket_no=None):
    """Резец подтипа `subtype` в кармане револьвера с номером pocket_no.

    НОМЕР КАРМАНА ОБЯЗАН СОВПАДАТЬ С НОМЕРОМ ИНСТРУМЕНТА в программе: стойка
    поворачивает револьвер на станцию по номеру T, а не ищет инструмент по
    TlNumber. Если положить T3 в POCKET_02, то команда T2 возьмёт левый резец —
    проверено, отрезка тогда идёт полноразмерной пластиной и протаскивает
    концевой конус на 3 мм.

    props — [(имя свойства билдера, значение)]; report — какие свойства
    перечитать ПОСЛЕ Commit новым билдером и напечатать: только так видно, что
    реально сохранилось в резце, а что осталось унаследованным от кармана.
    """
    # Карман задан — берём ТОЛЬКО его, без запасных. Уехать в соседний карман
    # хуже, чем не создать резец вовсе: стойка поворачивает револьвер по номеру
    # T, и в чужом кармане команда возьмёт не тот инструмент (именно так отрезка
    # однажды пошла полноразмерной пластиной).
    order = [f"POCKET_{pocket_no:02d}"] if pocket_no else list(POCKETS)
    for pname in order:
        if pname in used:
            continue
        try:
            parent = setup.CAMGroupCollection.FindObject(pname)
        except Exception:
            continue
        try:
            tool = setup.CAMGroupCollection.CreateToolWithUserName(
                parent, "turning", subtype,
                NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue,
                uname, uname)
        except Exception as e:
            log(f"warn: {subtype} в {pname} не создался ({e})")
            continue
        used.add(pname)
        tb = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
        for prop, val in props:
            # часть свойств — билдеры (значение в .Value), часть простые
            # атрибуты (HolderUse, AdapterUse); пробуем оба вида
            try:
                getattr(tb, prop).Value = val
            except Exception:
                try:
                    setattr(tb, prop.replace("Builder", ""), val)
                except Exception as e:
                    log(f"warn: {prop}={val} не применилось: "
                        f"{type(e).__name__}: {e}")
        tb.Commit()
        tb.Destroy()
        check = []
        try:
            tb2 = setup.CAMGroupCollection.CreateMillToolBuilder(tool)
            for prop in report:
                try:
                    check.append(f"{prop.replace('Builder', '')}="
                                 f"{getattr(tb2, prop).Value:g}")
                except Exception:
                    check.append(f"{prop.replace('Builder', '')}=н/д")
            tb2.Destroy()
        except Exception as e:
            check.append(f"<перечитать не удалось: {e}>")
        log(f"резец {subtype} ({uname}) в {pname}: {', '.join(check)}")
        return tool
    log(f"warn: резец {subtype} создать НЕ УДАЛОСЬ — свободных карманов нет")
    return None


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

    # ── 5. Резцы: проходной T1 и (если нужен) канавочный T2 ──
    #
    # ФОРМУ СЪЁМА ЗАДАЁТ ПОДТИП, а не числовые параметры. Проверено прогонами:
    # OrientAngle / NoseAngle / ReliefAngle / CuttingEdgeAngle записываются в
    # резец (перечитываются после Commit), но на срезаемый объём НЕ влияют —
    # ISV строит форму по геометрии пластины ШАБЛОНА подтипа. Из числовых
    # действует только Size (масштаб кромки) и NoseRadius.
    #
    # Решает ВСПОМОГАТЕЛЬНЫЙ угол в плане φ₁ = 180 − φ − ε (φ — угол в плане
    # подтипа, ε — угол при вершине пластины). Именно он говорит, насколько
    # резец волочит по уже обточенной поверхности, врезаясь глубже:
    #   OD_80_R: φ=95°,   ε=80° → φ₁ = 5°    — подрез до 6 мм длиной (Size)
    #   OD_55_R: φ=107.5°, ε=55° → φ₁ = 17.5° — подрез ~2 мм ← наш DCMT
    #   OD_80_L: φ=5°     — левый, кромка идёт впереди хода: конус в 5°
    # Замерено на пробной программе: наклон подреза совпал с φ₁ до 0.2°.
    # Наша программа точит справа налево (к патрону) пластиной DCMT (ромб 55°),
    # поэтому OD_55_R.
    used = set()
    subtype = p.get("tool_subtype", "OD_55_R")
    props = [("NoseRadiusBuilder", float(p.get("nose_radius", 0.4))),
             ("TlNumberBuilder", int(p.get("tool_number", 1))),
             ("NoseAngleBuilder", float(p.get("nose_angle", 55.0))),
             ("SizeBuilder", float(p.get("insert_size", 6.35))),
             ("ReliefAngleBuilder", float(p.get("relief_angle", 40.0)))]
    if p.get("orient_angle") is not None:      # ручной override, обычно не нужен
        props.append(("OrientAngleBuilder", float(p["orient_angle"])))
    # Сквозной проход остальных свойств билдера из конфига: {"CuttingEdgeAngle":
    # 95, ...} → CuttingEdgeAngleBuilder.Value. Нужен, чтобы подбирать геометрию
    # резца под ISV без правки кода (полный список свойств — см.
    # nx/research/turn_tool_probe_journal.py).
    for name, val in (p.get("tool_params") or {}).items():
        props.append((name if name.endswith("Builder") else name + "Builder", val))
    make_tool(setup, subtype, "TURN_OD", props, used,
              ("OrientAngleBuilder", "NoseAngleBuilder", "NoseRadiusBuilder",
               "SizeBuilder", "ReliefAngleBuilder"),
              pocket_no=int(p.get("tool_number", 1)))
    log("(форму съёма задаёт ПОДТИП: φ₁ = 180 − φ − ε — вспомогательный угол "
        "в плане; чем он меньше, тем сильнее резец волочит по готовой стенке)")

    # Левый проходной T3 — зеркальный подтип. Он берёт участки, куда правый не
    # заходит (за уступом, остающимся у него позади). Тип тот же токарный,
    # поэтому ISV его проводит нормально — в отличие от канавочного.
    lt = int(p.get("left_tool_number") or 0)
    if lt:
        make_tool(setup, p.get("left_subtype", "OD_55_L"), "TURN_OD_L",
                  [("NoseRadiusBuilder", float(p.get("nose_radius", 0.4))),
                   ("TlNumberBuilder", lt),
                   ("NoseAngleBuilder", float(p.get("nose_angle", 55.0))),
                   ("SizeBuilder", float(p.get("insert_size", 6.35)))],
                  used,
                  ("OrientAngleBuilder", "NoseAngleBuilder", "NoseRadiusBuilder",
                   "SizeBuilder"), pocket_no=lt)

    # Чистовой проходной T8 — 35°-ромб. Острее чернового, поэтому глубже заходит
    # в уступы; в программе он ведёт операцию Finish. Тип токарный, как у T1 и T3,
    # так что ISV проводит его штатно. Подтип берётся из конфига: если в станке
    # нет OD_35_R, ISV откажет — тогда ставим тот же подтип, что у чернового,
    # и говорим об этом в лог (на G-код это не влияет, только на симуляцию).
    ft = int(p.get("finish_tool_number") or 0)
    if ft:
        fprops = [("NoseRadiusBuilder", float(p.get("finish_nose_radius", 0.4))),
                  ("TlNumberBuilder", ft),
                  ("NoseAngleBuilder", float(p.get("finish_nose_angle", 35.0))),
                  ("SizeBuilder", float(p.get("finish_insert_size", 6.35)))]
        fsub = p.get("finish_subtype", "OD_35_R")
        before = len(used)
        make_tool(setup, fsub, "TURN_OD_FIN", fprops, used,
                  ("NoseAngleBuilder", "NoseRadiusBuilder", "SizeBuilder"),
                  pocket_no=ft)
        if len(used) == before and fsub != subtype:
            log(f"подтип {fsub} не создался — ставлю {subtype} (как у чернового); "
                f"в ISV чистовой будет волочить сильнее настоящего, на G-код не влияет")
            make_tool(setup, subtype, "TURN_OD_FIN", fprops, used,
                      ("NoseAngleBuilder", "NoseRadiusBuilder", "SizeBuilder"),
                      pocket_no=ft)

    # Канавочный резец T2. Программа отдаёт ему канавки и отрезку — то, куда
    # проходной физически не лезет. Если его не создать, стойка на T2 возьмёт
    # что попало и съём пойдёт чужой формой.
    gw = float(p.get("groove_width") or 0.0)
    if gw > 0:
        # У канавочного СВОЙ билдер (GrooveToolBuilder) и свой набор свойств:
        # InsertWidth (ширина пластины), InsertLength, Radius (радиус уголка),
        # SideAngle, Thickness. NoseWidth/NoseRadius у него НЕ СУЩЕСТВУЮТ
        # (None) — их выставлять бессмысленно. Геометрию правим по флагу:
        # шаблонная заведомо валидна, а наша могла оказаться вырожденной.
        gsub = p.get("groove_subtype", "OD_55_R")
        gprops = [("TlNumberBuilder", int(p.get("groove_tool_number", 2)))]
        if p.get("groove_geometry", True):
            if gsub.startswith("OD_GROOVE"):
                gprops.append(("InsertWidthBuilder", gw))
                if p.get("groove_orient_angle") is not None:
                    gprops.append(("OrientAngleBuilder",
                                   float(p["groove_orient_angle"])))
            else:
                # ПОДСТАНОВКА: настоящий канавочный ISV не тянет (роняет прогон
                # сразу после смены инструмента, без сообщения — проверено на
                # OD_GROOVE_L и OD_GROOVE_L_FNR, в POCKET_01 и POCKET_02, со
                # штатной и с нашей геометрией, с державкой и без). Вместо него
                # ставим УЗКУЮ ПРОХОДНУЮ пластину шириной с канавочную: врезание
                # моделируется, погрешность по стенкам канавки ≤ w·tg(φ₁).
                gprops.append(("SizeBuilder", gw))
                gprops.append(("NoseRadiusBuilder",
                               float(p.get("nose_radius", 0.4))))
        for name, val in (p.get("groove_params") or {}).items():
            gprops.append((name if name.endswith("Builder") else name + "Builder",
                           val))
        if not gsub.startswith("OD_GROOVE"):
            log(f"ВНИМАНИЕ: канавочный T{p.get('groove_tool_number', 2)} "
                f"подменён узкой проходной пластиной {gsub} шириной {gw} мм — "
                f"ISV параметрический канавочный не проводит")
        make_tool(setup, gsub, "TURN_GROOVE", gprops, used,
                  pocket_no=int(p.get("groove_tool_number", 2)),
                  report=
                  ("InsertWidthBuilder", "SizeBuilder", "RadiusBuilder",
                   "NoseRadiusBuilder", "ThicknessBuilder", "OrientAngleBuilder"))

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
    t_start = time.time()
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
            # прогресс в лог: по нему видно, ГДЕ программа встала (например на
            # смене инструмента), а не только что она встала
            log(f"  машинное время {t} (+{time.time() - last_change:.0f} с)")
            last, last_change = t, time.time()
        elif (t not in ("", "00:00:00.000")
              and time.time() - last_change > float(p.get("settle", 45))):
            log(f"машинное время стабилизировалось на {t}")
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

    # ISV пишет IPW не мгновенно после выхода из среды симуляции — ждём файл,
    # а не проверяем один раз
    ipw = None
    for _ in range(30):
        ipw = find_ipw_prt(p["work_prt"], t_start)
        if ipw:
            break
        time.sleep(2)
    if not ipw:
        import glob as _g
        seen = sorted(_g.glob(os.path.join(os.path.dirname(p["work_prt"]),
                                           "*_ipw.prt")),
                      key=os.path.getmtime)[-3:]
        log("в TEMP лежат (последние): "
            + ", ".join(f"{os.path.basename(f)}"
                        f"@{time.strftime('%H:%M:%S', time.localtime(os.path.getmtime(f)))}"
                        for f in seen) or "ничего")
        log(f"прогон стартовал в "
            f"{time.strftime('%H:%M:%S', time.localtime(t_start))}")
        raise RuntimeError(
            "ISV не сохранил IPW этого прогона (*_ipw.prt рядом с "
            f"{os.path.basename(p['work_prt'])}). Обычно это значит, что "
            "программа не дошла до конца — стойка встала (смена инструмента, "
            "лимиты осей), и симуляцию свернули по таймауту. Смотрите ход "
            "машинного времени выше.")
    log(f"DONE ipw={ipw} machine_time={mtime}")


if os.environ.get("NX_LATHE_SIM_PARAMS"):
    try:
        main()
    except Exception:
        import traceback
        log("ERROR:\n" + traceback.format_exc())
    os._exit(0)
