# NX CAM 2406
# Journal created by ринат on Mon Jul 20 21:16:44 2026 Горное время США (лето)

#
import math
import NXOpen
import NXOpen.Assemblies
import NXOpen.CAM
import NXOpen.MenuBar
import NXOpen.SIM
def main() : 

    theSession  = NXOpen.Session.GetSession() #type: NXOpen.Session
    # ----------------------------------------------
    #   Меню: Файл->Импорт->STEP242...
    # ----------------------------------------------
    markId1 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Начало")
    
    step242Importer1 = theSession.DexManager.CreateStep242Importer()
    
    step242Importer1.SimplifyGeometry = True
    
    step242Importer1.Messages = NXOpen.Step242Importer.MessageEnum.Informational
    
    step242Importer1.ImportTo = NXOpen.Step242Importer.ImportToOption.NewPart
    
    step242Importer1.OutputFile = "C:\\Users\\ринат\\Downloads\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented (2)_1.prt"
    
    step242Importer1.ObjectTypes.Curves = True
    
    step242Importer1.ObjectTypes.Surfaces = True
    
    step242Importer1.ObjectTypes.Solids = True
    
    step242Importer1.ObjectTypes.PmiData = True
    
    step242Importer1.SewSurfaces = True
    
    step242Importer1.SimplifyGeometry = False
    
    step242Importer1.Optimize = True
    
    step242Importer1.Messages = NXOpen.Step242Importer.MessageEnum.Error
    
    step242Importer1.SettingsFile = "C:\\Program Files\\Siemens\\NX2406\\translators\\step242\\step242ug.def"
    
    step242Importer1.OutputFile = ""
    
    theSession.SetUndoMarkName(markId1, "Диалоговое окно Импорт файла STEP242")
    
    step242Importer1.SetMode(NXOpen.BaseImporter.Mode.NativeFileSystem)
    
    step242Importer1.OutputFile = "C:\\Users\\ринат\\CHPU\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1.prt"
    
    step242Importer1.InputFile = "C:\\Users\\ринат\\CHPU\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented.stp"
    
    markId2 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Импорт файла STEP242")
    
    theSession.DeleteUndoMark(markId2, None)
    
    markId3 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Импорт файла STEP242")
    
    step242Importer1.FileOpenFlag = False
    
    step242Importer1.ProcessHoldFlag = True
    
    nXObject1 = step242Importer1.Commit()
    
    theSession.DeleteUndoMark(markId3, None)
    
    theSession.SetUndoMarkName(markId1, "Импорт файла STEP242")
    
    step242Importer1.Destroy()
    
    # ----------------------------------------------
    #   Меню: Файл->Открыть...
    # ----------------------------------------------
    basePart1, partLoadStatus1 = theSession.Parts.OpenActiveDisplay("C:\\Users\\ринат\\CHPU\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1.prt", NXOpen.DisplayPartOption.AllowAdditional)
    
    workPart = theSession.Parts.Work
    displayPart = theSession.Parts.Display
    partLoadStatus1.Dispose()
    theSession.ApplicationSwitchImmediate("UG_APP_MODELING")
    
    scaleAboutPoint1 = NXOpen.Point3d(14.773412630018084, -71.678409427125047, 0.0)
    viewCenter1 = NXOpen.Point3d(-14.773412630018084, 71.678409427125047, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint1, viewCenter1)
    
    scaleAboutPoint2 = NXOpen.Point3d(11.81873010401447, -57.34272754170005, 0.0)
    viewCenter2 = NXOpen.Point3d(-11.81873010401447, 57.34272754170005, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint2, viewCenter2)
    
    scaleAboutPoint3 = NXOpen.Point3d(9.4549840832115759, -45.874182033360043, 0.0)
    viewCenter3 = NXOpen.Point3d(-9.4549840832115759, 45.874182033360043, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint3, viewCenter3)
    
    scaleAboutPoint4 = NXOpen.Point3d(7.5639872665693106, -36.699345626688036, 0.0)
    viewCenter4 = NXOpen.Point3d(-7.5639872665692103, 36.699345626688036, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint4, viewCenter4)
    
    scaleAboutPoint5 = NXOpen.Point3d(6.0511898132554487, -29.359476501350425, 0.0)
    viewCenter5 = NXOpen.Point3d(-6.0511898132553679, 29.359476501350425, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint5, viewCenter5)
    
    scaleAboutPoint6 = NXOpen.Point3d(6.9924860064285239, 34.962430032142507, 0.0)
    viewCenter6 = NXOpen.Point3d(-6.99248600642846, -34.962430032142507, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint6, viewCenter6)
    
    scaleAboutPoint7 = NXOpen.Point3d(5.5939888051428444, 27.969944025714025, 0.0)
    viewCenter7 = NXOpen.Point3d(-5.5939888051427404, -27.969944025714, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint7, viewCenter7)
    
    scaleAboutPoint8 = NXOpen.Point3d(4.4751910441142968, 22.375955220571203, 0.0)
    viewCenter8 = NXOpen.Point3d(-4.4751910441141929, -22.375955220571203, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint8, viewCenter8)
    
    scaleAboutPoint9 = NXOpen.Point3d(3.5801528352914538, 17.900764176456963, 0.0)
    viewCenter9 = NXOpen.Point3d(-3.5801528352913214, -17.900764176456963, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint9, viewCenter9)
    
    scaleAboutPoint10 = NXOpen.Point3d(2.8641222682331628, 14.32061134116557, 0.0)
    viewCenter10 = NXOpen.Point3d(-2.8641222682330572, -14.32061134116557, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint10, viewCenter10)
    
    scaleAboutPoint11 = NXOpen.Point3d(2.291297814586541, 11.456489072932456, 0.0)
    viewCenter11 = NXOpen.Point3d(-2.2912978145864353, -11.456489072932456, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint11, viewCenter11)
    
    scaleAboutPoint12 = NXOpen.Point3d(-35.391738551458957, -24.76951688794011, 0.0)
    viewCenter12 = NXOpen.Point3d(35.391738551459092, 24.76951688794011, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint12, viewCenter12)
    
    scaleAboutPoint13 = NXOpen.Point3d(-44.709682997444013, -31.19690101398529, 0.0)
    viewCenter13 = NXOpen.Point3d(44.709682997444148, 31.19690101398529, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint13, viewCenter13)
    
    scaleAboutPoint14 = NXOpen.Point3d(-56.621494071992984, -39.583638527632004, 0.0)
    viewCenter14 = NXOpen.Point3d(56.621494071993141, 39.58363852763199, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint14, viewCenter14)
    
    # ----------------------------------------------
    #   Меню: Приложение->Обработка->Обработка
    # ----------------------------------------------
    markId4 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Enter Обработка")
    
    theSession.ApplicationSwitchImmediate("UG_APP_MANUFACTURING")
    
    result1 = theSession.IsCamSessionInitialized()
    
    theSession.CreateCamSession()
    
    theSession.CAMSession.PathDisplay.SetAnimationSpeed(5)
    
    theSession.CAMSession.PathDisplay.SetIpwResolution(NXOpen.CAM.PathDisplay.IpwResolutionType.Medium)
    
    markId5 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Инициализация установки")
    
    theSession.CAMSession.SpecifyConfiguration("C:\\Program Files\\Siemens\\NX2406\\mach\\resource\\configuration\\cam_general.dat")
    
    cAMSetup1 = workPart.CreateCamSetup("mill_planar")
    
    kinematicConfigurator1 = workPart.CreateKinematicConfigurator()
    
    kinematicConfigurator2 = workPart.KinematicConfigurator
    
    orientGeometry1 = cAMSetup1.CAMGroupCollection.FindObject("MCS_MAIN")
    theSession.CAMSession.PathDisplay.ShowToolPath(orientGeometry1)
    
    markId6 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Изменить MCS_MAIN")
    
    markId7 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    millOrientGeomBuilder1 = cAMSetup1.CAMGroupCollection.CreateMillOrientGeomBuilder(orientGeometry1)
    
    csyspurposemode1 = millOrientGeomBuilder1.GetCsysPurposeMode()
    
    specialoutputmode1 = millOrientGeomBuilder1.GetSpecialOutputMode()
    
    toolaxismode1 = millOrientGeomBuilder1.GetToolAxisMode()
    
    origin1 = NXOpen.Point3d(0.0, 0.0, 0.0)
    normal1 = NXOpen.Vector3d(0.0, 0.0, 1.0)
    plane1 = workPart.Planes.CreatePlane(origin1, normal1, NXOpen.SmartObject.UpdateOption.AfterModeling)
    
    unit1 = workPart.UnitCollection.FindObject("MilliMeter")
    expression1 = workPart.Expressions.CreateSystemExpressionWithUnits("0", unit1)
    
    expression2 = workPart.Expressions.CreateSystemExpressionWithUnits("0", unit1)
    
    lowerlimitmode1 = millOrientGeomBuilder1.GetLowerLimitMode()
    
    origin2 = NXOpen.Point3d(0.0, 0.0, 0.0)
    normal2 = NXOpen.Vector3d(0.0, 0.0, 1.0)
    plane2 = workPart.Planes.CreatePlane(origin2, normal2, NXOpen.SmartObject.UpdateOption.AfterModeling)
    
    expression3 = workPart.Expressions.CreateSystemExpressionWithUnits("0", unit1)
    
    expression4 = workPart.Expressions.CreateSystemExpressionWithUnits("0", unit1)
    
    theSession.SetUndoMarkName(markId7, "Диалоговое окно MCS Main")
    
    # ----------------------------------------------
    #   Начало меню MCS Main
    # ----------------------------------------------
    origin3 = NXOpen.Point3d(0.0, 0.0, -20.0)
    xDirection1 = NXOpen.Vector3d(1.0, 0.0, 0.0)
    yDirection1 = NXOpen.Vector3d(0.0, 1.0, 0.0)
    xform1 = workPart.Xforms.CreateXform(origin3, xDirection1, yDirection1, NXOpen.SmartObject.UpdateOption.AfterModeling, 1.0)
    
    cartesianCoordinateSystem1 = workPart.CoordinateSystems.CreateCoordinateSystem(xform1, NXOpen.SmartObject.UpdateOption.AfterModeling)
    
    millOrientGeomBuilder1.Mcs = cartesianCoordinateSystem1
    
    rotMatrix1 = NXOpen.Matrix3x3()
    
    rotMatrix1.Xx = 0.9694717676857908
    rotMatrix1.Xy = 0.052117080923659902
    rotMatrix1.Xz = 0.23960029535913999
    rotMatrix1.Yx = -0.24233407421508166
    rotMatrix1.Yy = 0.054609358796066848
    rotMatrix1.Yz = 0.9686547446878081
    rotMatrix1.Zx = 0.037399039219070791
    rotMatrix1.Zy = -0.99714674336723419
    rotMatrix1.Zz = 0.065571976160638007
    translation1 = NXOpen.Point3d(-50.383659350137179, -0.10730217592594737, 39.37306590190471)
    workPart.ModelingViews.WorkView.SetRotationTranslationScale(rotMatrix1, translation1, 1.4411046783090966)
    
    origin4 = NXOpen.Point3d(0.0, 0.0, -1.0)
    xDirection2 = NXOpen.Vector3d(1.0, 0.0, 0.0)
    yDirection2 = NXOpen.Vector3d(0.0, 1.0, 0.0)
    xform2 = workPart.Xforms.CreateXform(origin4, xDirection2, yDirection2, NXOpen.SmartObject.UpdateOption.AfterModeling, 1.0)
    
    cartesianCoordinateSystem2 = workPart.CoordinateSystems.CreateCoordinateSystem(xform2, NXOpen.SmartObject.UpdateOption.AfterModeling)
    
    millOrientGeomBuilder1.Mcs = cartesianCoordinateSystem2
    
    origin5 = NXOpen.Point3d(0.0, 0.0, -21.0)
    xDirection3 = NXOpen.Vector3d(1.0, 0.0, 0.0)
    yDirection3 = NXOpen.Vector3d(0.0, 1.0, 0.0)
    xform3 = workPart.Xforms.CreateXform(origin5, xDirection3, yDirection3, NXOpen.SmartObject.UpdateOption.AfterModeling, 1.0)
    
    cartesianCoordinateSystem3 = workPart.CoordinateSystems.CreateCoordinateSystem(xform3, NXOpen.SmartObject.UpdateOption.AfterModeling)
    
    millOrientGeomBuilder1.Mcs = cartesianCoordinateSystem3
    
    rotMatrix2 = NXOpen.Matrix3x3()
    
    rotMatrix2.Xx = 0.96881884638271276
    rotMatrix2.Xy = 0.00067912803408097175
    rotMatrix2.Xz = 0.24776921051411019
    rotMatrix2.Yx = -0.24770352676409788
    rotMatrix2.Yy = 0.025841750818764581
    rotMatrix2.Yz = 0.96849118051908456
    rotMatrix2.Zx = -0.0057450606872168951
    rotMatrix2.Zy = -0.99966581551023204
    rotMatrix2.Zz = 0.025204197625911147
    translation2 = NXOpen.Point3d(-49.337772206439517, 0.70992069457892715, 41.425483508349728)
    workPart.ModelingViews.WorkView.SetRotationTranslationScale(rotMatrix2, translation2, 1.4411046783090966)
    
    markId8 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "MCS Main")
    
    theSession.DeleteUndoMark(markId8, None)
    
    markId9 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "MCS Main")
    
    nXObject2 = millOrientGeomBuilder1.Commit()
    
    theSession.DeleteUndoMark(markId9, None)
    
    theSession.SetUndoMarkName(markId7, "MCS Main")
    
    millOrientGeomBuilder1.Destroy()
    
    markId10 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "")
    
    nErrs1 = theSession.UpdateManager.DoUpdate(markId10)
    
    theSession.DeleteUndoMark(markId10, "")
    
    try:
        # Выражение используется
        workPart.Expressions.Delete(expression4)
    except NXOpen.NXException as ex:
        ex.AssertErrorCode(1050029)
        
    try:
        # Выражение используется
        workPart.Expressions.Delete(expression2)
    except NXOpen.NXException as ex:
        ex.AssertErrorCode(1050029)
        
    try:
        # Выражение используется
        workPart.Expressions.Delete(expression3)
    except NXOpen.NXException as ex:
        ex.AssertErrorCode(1050029)
        
    plane2.DestroyPlane()
    
    try:
        # Выражение используется
        workPart.Expressions.Delete(expression1)
    except NXOpen.NXException as ex:
        ex.AssertErrorCode(1050029)
        
    plane1.DestroyPlane()
    
    theSession.DeleteUndoMark(markId7, None)
    
    theSession.CAMSession.Utils.SetInspectionIntent(False)
    
    scaleAboutPoint15 = NXOpen.Point3d(-14.596007713110966, -19.553142408129911, 0.0)
    viewCenter15 = NXOpen.Point3d(14.596007713111131, 19.553142408129911, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint15, viewCenter15)
    
    scaleAboutPoint16 = NXOpen.Point3d(-18.24500964138873, -24.44142801016239, 0.0)
    viewCenter16 = NXOpen.Point3d(18.245009641388894, 24.44142801016239, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint16, viewCenter16)
    
    scaleAboutPoint17 = NXOpen.Point3d(-23.953746934842108, -31.125527454256098, 0.0)
    viewCenter17 = NXOpen.Point3d(23.953746934842339, 31.125527454256073, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint17, viewCenter17)
    
    scaleAboutPoint18 = NXOpen.Point3d(-31.017950746464749, -39.982676395732206, 0.0)
    viewCenter18 = NXOpen.Point3d(31.017950746465008, 39.982676395732142, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint18, viewCenter18)
    
    rotMatrix3 = NXOpen.Matrix3x3()
    
    rotMatrix3.Xx = 0.34612004572339283
    rotMatrix3.Xy = 0.92475174806199056
    rotMatrix3.Xz = -0.15822489818209534
    rotMatrix3.Yx = -0.20031518468889123
    rotMatrix3.Yy = 0.23760242560463257
    rotMatrix3.Yz = 0.95048351596955793
    rotMatrix3.Zx = 0.91655591249606649
    rotMatrix3.Zy = -0.29728654830498669
    rotMatrix3.Zz = 0.26748077961867761
    translation3 = NXOpen.Point3d(-61.141966173236185, -34.406450264768694, -14.489060501992427)
    workPart.ModelingViews.WorkView.SetRotationTranslationScale(rotMatrix3, translation3, 0.59027647623540602)
    
    scaleAboutPoint19 = NXOpen.Point3d(-31.600657913667174, -43.703037540178109, 0.0)
    viewCenter19 = NXOpen.Point3d(31.600657913667415, 43.703037540178066, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint19, viewCenter19)
    
    scaleAboutPoint20 = NXOpen.Point3d(-25.280526330933675, -34.962430032142514, 0.0)
    viewCenter20 = NXOpen.Point3d(25.280526330933931, 34.96243003214245, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint20, viewCenter20)
    
    scaleAboutPoint21 = NXOpen.Point3d(-20.224421064746888, -27.969944025714014, 0.0)
    viewCenter21 = NXOpen.Point3d(20.224421064747197, 27.969944025713961, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint21, viewCenter21)
    
    scaleAboutPoint22 = NXOpen.Point3d(-16.17953685179749, -22.375955220571214, 0.0)
    viewCenter22 = NXOpen.Point3d(16.17953685179776, 22.375955220571154, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint22, viewCenter22)
    
    orientGeometry2 = nXObject2
    theSession.CAMSession.PathDisplay.HideToolPath(orientGeometry2)
    
    featureGeometry1 = cAMSetup1.CAMGroupCollection.FindObject("WORKPIECE")
    theSession.CAMSession.PathDisplay.ShowToolPath(featureGeometry1)
    
    markId11 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Изменить WORKPIECE")
    
    markId12 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    millGeomBuilder1 = cAMSetup1.CAMGroupCollection.CreateMillGeomBuilder(featureGeometry1)
    
    theSession.SetUndoMarkName(markId12, "Диалоговое окно Заготовка")
    
    # ----------------------------------------------
    #   Начало меню Заготовка
    # ----------------------------------------------
    millGeomBuilder1.BlankGeometry.InitializeData(False)
    
    markId13 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    geometrySetList1 = millGeomBuilder1.BlankGeometry.GeometryList
    
    blankIpwSetList1 = millGeomBuilder1.BlankGeometry.BlankIpwMultipleSource.SetList
    
    theSession.SetUndoMarkName(markId13, "Диалоговое окно Геометрия заготовки")
    
    taggedObject1 = geometrySetList1.FindItem(0)
    
    geometrySet1 = taggedObject1
    taggedObject2 = blankIpwSetList1.FindItem(0)
    
    blankIpwSet1 = taggedObject2
    theSession.CAMSession.PathDisplay.HideToolPath(featureGeometry1)
    
    # ----------------------------------------------
    #   Начало меню Геометрия заготовки
    # ----------------------------------------------
    partLoadStatus2 = workPart.LoadThisPartFully()
    
    partLoadStatus2.Dispose()
    selectionIntentRuleOptions1 = workPart.ScRuleFactory.CreateRuleOptions()
    
    selectionIntentRuleOptions1.SetSelectedFromInactive(False)
    
    bodies1 = [NXOpen.Body.Null] * 1 
    body1 = workPart.Bodies.FindObject("UNPARAMETERIZED_FEATURE(1)")
    bodies1[0] = body1
    bodyDumbRule1 = workPart.ScRuleFactory.CreateRuleBodyDumb(bodies1, True, selectionIntentRuleOptions1)
    
    selectionIntentRuleOptions1.Dispose()
    scCollector1 = geometrySet1.ScCollector
    
    rules1 = [None] * 1 
    rules1[0] = bodyDumbRule1
    scCollector1.ReplaceRules(rules1, False)
    
    markId14 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Геометрия заготовки")
    
    theSession.DeleteUndoMark(markId14, None)
    
    markId15 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Геометрия заготовки")
    
    theSession.DeleteUndoMark(markId15, None)
    
    theSession.SetUndoMarkName(markId13, "Геометрия заготовки")
    
    theSession.DeleteUndoMark(markId13, None)
    
    markId16 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Заготовка")
    
    theSession.DeleteUndoMark(markId16, None)
    
    markId17 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Заготовка")
    
    nXObject3 = millGeomBuilder1.Commit()
    
    theSession.DeleteUndoMark(markId17, None)
    
    theSession.SetUndoMarkName(markId12, "Заготовка")
    
    millGeomBuilder1.Destroy()
    
    theSession.DeleteUndoMark(markId12, None)
    
    markId18 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    featureGeometry2 = nXObject3
    millGeomBuilder2 = cAMSetup1.CAMGroupCollection.CreateMillGeomBuilder(featureGeometry2)
    
    theSession.SetUndoMarkName(markId18, "Диалоговое окно Заготовка")
    
    # ----------------------------------------------
    #   Начало меню Заготовка
    # ----------------------------------------------
    markId19 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Заготовка")
    
    theSession.DeleteUndoMark(markId19, None)
    
    markId20 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Заготовка")
    
    nXObject4 = millGeomBuilder2.Commit()
    
    theSession.DeleteUndoMark(markId20, None)
    
    theSession.SetUndoMarkName(markId18, "Заготовка")
    
    millGeomBuilder2.Destroy()
    
    theSession.DeleteUndoMark(markId18, None)
    
    theSession.CAMSession.Utils.SetInspectionIntent(False)
    
    featureGeometry3 = nXObject4
    theSession.CAMSession.PathDisplay.HideToolPath(featureGeometry3)
    
    nCGroup1 = cAMSetup1.CAMGroupCollection.FindObject("GENERIC_MACHINE")
    theSession.CAMSession.PathDisplay.ShowToolPath(nCGroup1)
    
    markId21 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Изменить GENERIC_MACHINE")
    
    markId22 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    machineGroupBuilder1 = cAMSetup1.CAMGroupCollection.CreateMachineGroupBuilder(nCGroup1)
    
    theSession.SetUndoMarkName(markId22, "Диалоговое окно Базовый Станок")
    
    # ----------------------------------------------
    #   Начало меню Базовый Станок
    # ----------------------------------------------
    theSession.CAMSession.PathDisplay.HideToolPath(nCGroup1)
    
    # ----------------------------------------------
    #   Начало меню Выбор класса библиотеки
    # ----------------------------------------------
    # ----------------------------------------------
    #   Начало меню Результат поиска
    # ----------------------------------------------
    ncmctPartMountingBuilder1 = cAMSetup1.CreateNcmctPartMountingBuilder("sim01_mill_3ax_sinumerik")
    
    ncmctPartMountingBuilder1.CreateMachineSpindleObjects = False
    
    markId23 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    ncmctPartMountingBuilder1.Positioning = NXOpen.CAM.NcmctPartMountingBuilder.PositioningTypes.UseAssemblyPositioning
    
    theSession.SetUndoMarkName(markId23, "Диалоговое окно Крепление детали")
    
    part1 = ncmctPartMountingBuilder1.LoadMachinePreview()
    
    position1 = NXOpen.Matrix4x4()
    
    position1.Rxx = 1.0
    position1.Rxy = 0.0
    position1.Rxz = 0.0
    position1.Xt = 0.0
    position1.Ryx = 0.0
    position1.Ryy = 1.0
    position1.Ryz = 0.0
    position1.Yt = 0.0
    position1.Rzx = 0.0
    position1.Rzy = 0.0
    position1.Rzz = 1.0
    position1.Zt = 0.0
    position1.Sx = 0.0
    position1.Sy = 0.0
    position1.Sz = 0.0
    position1.Ss = 1.0
    ncmctPartMountingBuilder1.ShowMachinePreview(workPart, part1, position1)
    
    # ----------------------------------------------
    #   Начало меню Крепление детали
    # ----------------------------------------------
    scaleAboutPoint23 = NXOpen.Point3d(-45.440401371005969, -22.674301290178839, 0.0)
    viewCenter23 = NXOpen.Point3d(45.440401371006246, 22.674301290178789, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint23, viewCenter23)
    
    scaleAboutPoint24 = NXOpen.Point3d(-56.800501713757498, -28.572373589344771, 0.0)
    viewCenter24 = NXOpen.Point3d(56.800501713757768, 28.572373589344711, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint24, viewCenter24)
    
    scaleAboutPoint25 = NXOpen.Point3d(-71.00062714219689, -35.715466986680987, 0.0)
    viewCenter25 = NXOpen.Point3d(71.000627142197203, 35.715466986680909, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint25, viewCenter25)
    
    ncmctPartMountingBuilder1.Positioning = NXOpen.CAM.NcmctPartMountingBuilder.PositioningTypes.OrientMachineZeroToMainMcs
    
    markId24 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Крепление детали")
    
    theSession.DeleteUndoMark(markId24, None)
    
    markId25 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Крепление детали")
    
    nXObject5 = ncmctPartMountingBuilder1.Commit()
    
    theSession.DeleteUndoMark(markId25, None)
    
    theSession.SetUndoMarkName(markId23, "Крепление детали")
    
    ncmctPartMountingBuilder1.RemoveMachinePreview()
    
    theSession.DeleteUndoMark(markId23, None)
    
    machineGroupBuilder1.ReplaceMachine(NXOpen.CAM.MachineGroupBuilder.RetrieveToolPocketInformation.Yes, ncmctPartMountingBuilder1)
    
    ncmctPartMountingBuilder1.Destroy()
    
    markId26 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Базовый Станок")
    
    theSession.DeleteUndoMark(markId26, None)
    
    markId27 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Базовый Станок")
    
    theSession.DeleteUndoMark(markId27, None)
    
    theSession.SetUndoMarkName(markId22, "Базовый Станок")
    
    machineGroupBuilder1.Destroy()
    
    theSession.DeleteUndoMark(markId22, None)
    
    markId28 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    machineGroupBuilder2 = cAMSetup1.CAMGroupCollection.CreateMachineGroupBuilder(nCGroup1)
    
    theSession.SetUndoMarkName(markId28, "Диалоговое окно 3-Ax Mill Vertical Post Configurator Metric Inch")
    
    # ----------------------------------------------
    #   Начало меню 3-Ax Mill Vertical Post Configurator Metric Inch
    # ----------------------------------------------
    markId29 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "3-Ax Mill Vertical Post Configurator Metric Inch")
    
    theSession.DeleteUndoMark(markId29, None)
    
    markId30 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "3-Ax Mill Vertical Post Configurator Metric Inch")
    
    nXObject6 = machineGroupBuilder2.Commit()
    
    theSession.DeleteUndoMark(markId30, None)
    
    theSession.SetUndoMarkName(markId28, "3-Ax Mill Vertical Post Configurator Metric Inch")
    
    machineGroupBuilder2.Destroy()
    
    theSession.DeleteUndoMark(markId28, None)
    
    theSession.CAMSession.Utils.SetInspectionIntent(False)
    
    markId31 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Изменить POCKET_01")
    
    markId32 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    nCGroup2 = cAMSetup1.CAMGroupCollection.FindObject("POCKET_01")
    machinePocketGroupBuilder1 = cAMSetup1.CAMGroupCollection.CreateMachinePocketGroupBuilder(nCGroup2)
    
    inheritableIntBuilder1 = machinePocketGroupBuilder1.AdjustIdBuilder
    
    inheritableIntBuilder2 = machinePocketGroupBuilder1.CutcomIdBuilder
    
    inheritableIntBuilder3 = machinePocketGroupBuilder1.PocketIdBuilder
    
    inheritableTextBuilder1 = machinePocketGroupBuilder1.PocketIdStringBuilder
    
    holdingSystemBuilder1 = machinePocketGroupBuilder1.CreateHoldingSystemBuilder(1, "Новый")
    
    holdingSystemBuilderList1 = machinePocketGroupBuilder1.HoldingSystemsList
    
    theSession.SetUndoMarkName(markId32, "Диалоговое окно Карман:")
    
    # ----------------------------------------------
    #   Начало меню Карман:
    # ----------------------------------------------
    machinePocketGroupBuilder1.Destroy()
    
    theSession.UndoToMark(markId32, None)
    
    theSession.DeleteUndoMark(markId32, None)
    
    theSession.DeleteUndoMark(markId32, None)
    
    theSession.CAMSession.Utils.SetInspectionIntent(False)
    
    theSession.CAMSession.PathDisplay.HideToolPath(nCGroup2)
    
    theSession.UndoToMark(markId31, "Изменить POCKET_01")
    
    theSession.DeleteUndoMarksUpToMark(markId31, "Изменить POCKET_01", False)
    
    theSession.CAMSession.PathDisplay.ShowToolPath(nCGroup2)
    
    theSession.CAMSession.PathDisplay.HideToolPath(nCGroup2)
    
    # ----------------------------------------------
    #   Начало меню Создание инструмента
    # ----------------------------------------------
    markId33 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Создание инструмента")
    
    nCGroup3 = cAMSetup1.CAMGroupCollection.CreateToolWithUserName(nCGroup2, "mill_planar", "MILL", NXOpen.CAM.NCGroupCollection.UseDefaultName.TrueValue, "MILL", "Mill")
    
    markId34 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    tool1 = nCGroup3
    millToolBuilder1 = cAMSetup1.CAMGroupCollection.CreateMillToolBuilder(tool1)
    
    theSession.SetUndoMarkName(markId34, "Диалоговое окно Фреза 5 Параметров")
    
    # ----------------------------------------------
    #   Начало меню Фреза 5 Параметров
    # ----------------------------------------------
    markId35 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Фреза 5 Параметров")
    
    millToolBuilder1.TlDiameterBuilder.Value = 12.0
    
    theSession.DeleteUndoMark(markId35, None)
    
    markId36 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Фреза 5 Параметров")
    
    nXObject7 = millToolBuilder1.Commit()
    
    theSession.DeleteUndoMark(markId36, None)
    
    theSession.SetUndoMarkName(markId34, "Фреза 5 Параметров")
    
    millToolBuilder1.Destroy()
    
    theSession.DeleteUndoMark(markId34, None)
    
    theSession.CAMSession.Utils.SetInspectionIntent(False)
    
    nCGroup4 = cAMSetup1.CAMGroupCollection.FindObject("PROGRAM")
    theSession.CAMSession.PathDisplay.ShowToolPath(nCGroup4)
    
    kinematicConfigurator3 = workPart.KinematicConfigurator
    
    markId37 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Начало")
    
    kinematicConfigurator4 = workPart.KinematicConfigurator
    
    kinematicComponent1 = kinematicConfigurator4.ComponentCollection.FindObject("SETUP")
    
    kinematicComponent2 = kinematicConfigurator4.ComponentCollection.FindObject("BLANK")
    
    kinematicComponentBuilder1 = kinematicConfigurator4.ComponentCollection.CreateComponentBuilder(kinematicComponent1, kinematicComponent2)
    
    kinematicJunctionBuilderList1 = kinematicComponentBuilder1.JunctionList
    
    theSession.SetUndoMarkName(markId37, "Диалоговое окно Изменить компонент станка")
    
    kinematicComponent3 = kinematicConfigurator4.ComponentCollection.FindObject("BLANK")
    
    kinematicJunctionBuilder1 = kinematicConfigurator4.CreateJunctionBuilder(kinematicComponent3, NXOpen.SIM.KinematicJunction.Null)
    
    kinematicJunctionBuilder1.Name = "BLANK_JCT"
    
    kinematicJunctionBuilder1.Classification = NXOpen.SIM.KinematicJunctionBuilder.SystemClass.NotSet
    
    kinematicJunctionBuilder1.Csys = NXOpen.CartesianCoordinateSystem.Null
    
    kinematicJunctionBuilderList1.Append(kinematicJunctionBuilder1)
    
    # ----------------------------------------------
    #   Начало меню Изменить компонент станка
    # ----------------------------------------------
    theSession.CAMSession.PathDisplay.HideToolPath(nCGroup4)
    
    kinematicComponentBuilder1.AddGeometry(body1)
    
    markId38 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Изменить компонент станка")
    
    theSession.DeleteUndoMark(markId38, None)
    
    markId39 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Изменить компонент станка")
    
    nXObject8 = kinematicComponentBuilder1.Commit()
    
    theSession.DeleteUndoMark(markId39, None)
    
    theSession.SetUndoMarkName(markId37, "Изменить компонент станка")
    
    kinematicComponentBuilder1.Destroy()
    
    nCGroup5 = cAMSetup1.CAMGroupCollection.FindObject("NC_PROGRAM")
    theSession.CAMSession.PathDisplay.ShowToolPath(nCGroup5)
    
    markId40 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Enter Simulate Machine")
    
    theSession.BeginTaskEnvironment()
    
    theSession.CAMSession.PathDisplay.HideToolPath(nCGroup5)
    
    kinematicConfigurator5 = workPart.KinematicConfigurator
    
    objects1 = [NXOpen.CAM.CAMObject.Null] * 1 
    objects1[0] = nCGroup5
    try:
        #  
        isvControlPanelBuilder1 = kinematicConfigurator5.CreateIsvControlPanelBuilder(NXOpen.SIM.IsvControlPanelBuilder.VisualizationType.ToolPathSimulation, objects1)
    except NXOpen.NXException as ex:
        ex.AssertErrorCode(690029)
        
    # ----------------------------------------------
    #   Меню: Симуляция->Симуляция внешней программы...
    # ----------------------------------------------
    kinematicConfigurator6 = workPart.KinematicConfigurator
    
    ncChannelSelectionData1 = kinematicConfigurator6.CreateNcChannelSelectionData()
    
    markId41 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    theSession.SetUndoMarkName(markId41, "Диалоговое окно Симуляция программы ЧПУ из файла")
    
    markId42 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Симуляция программы ЧПУ из файла")
    
    theSession.DeleteUndoMark(markId42, None)
    
    markId43 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Симуляция программы ЧПУ из файла")
    
    ncProgramManagerBuilder1 = kinematicConfigurator6.CreateNcProgramManagerBuilder()
    
    ncProgramSource1 = ncProgramManagerBuilder1.GetExternalFileSource()
    
    ncProgramManagerBuilder1.Destroy()
    
    ncProgram1 = ncProgramSource1.AddMainProgram("Main", "C:\\NX52\\75.6121.0.0411.003-A-CAM-DMC-635_layer.mpf")
    
    ncChannelSelectionData1.AssignProgram("Main", ncProgram1)
    
    theSession.DeleteUndoMark(markId43, None)
    
    theSession.SetUndoMarkName(markId41, "Симуляция программы ЧПУ из файла")
    
    theSession.DeleteUndoMark(markId41, None)
    
    isvControlPanelBuilder2 = kinematicConfigurator6.CreateIsvControlPanelBuilder(NXOpen.SIM.IsvControlPanelBuilder.VisualizationType.MachineCodeSimulateCse, ncChannelSelectionData1)
    
    # ----------------------------------------------
    #   Меню: Симуляция->ЗвПО->Удаление материала
    # ----------------------------------------------
    simulationOptionsBuilder1 = isvControlPanelBuilder2.SimulationOptionsBuilder
    
    simulationOptionsBuilder1.EnableMaterialRemoval = True
    
    nXObject9 = simulationOptionsBuilder1.Commit()
    
    isvControlPanelBuilder2.ApplySimulationOptions()
    
    scaleAboutPoint26 = NXOpen.Point3d(-55.04341548650104, 98.791276654925682, 0.0)
    viewCenter26 = NXOpen.Point3d(55.043415486501431, -98.791276654925753, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint26, viewCenter26)
    
    scaleAboutPoint27 = NXOpen.Point3d(-68.80426935812639, 123.04085953619374, 0.0)
    viewCenter27 = NXOpen.Point3d(68.804269358126703, -123.04085953619382, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint27, viewCenter27)
    
    scaleAboutPoint28 = NXOpen.Point3d(-86.005336697657981, 153.80107442024223, 0.0)
    viewCenter28 = NXOpen.Point3d(86.005336697658393, -153.80107442024229, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint28, viewCenter28)
    
    scaleAboutPoint29 = NXOpen.Point3d(-107.50667087207249, 192.25134302530279, 0.0)
    viewCenter29 = NXOpen.Point3d(107.50667087207299, -192.25134302530279, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint29, viewCenter29)
    
    scaleAboutPoint30 = NXOpen.Point3d(-141.3870305035808, 256.0724855869812, 0.0)
    viewCenter30 = NXOpen.Point3d(141.3870305035812, -256.07248558698126, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint30, viewCenter30)
    
    scaleAboutPoint31 = NXOpen.Point3d(-176.73378812947618, 320.09060698372633, 0.0)
    viewCenter31 = NXOpen.Point3d(176.73378812947675, -320.0906069837265, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint31, viewCenter31)
    
    scaleAboutPoint32 = NXOpen.Point3d(-220.91723516184521, 400.11325872965801, 0.0)
    viewCenter32 = NXOpen.Point3d(220.91723516184547, -400.11325872965801, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint32, viewCenter32)
    
    scaleAboutPoint33 = NXOpen.Point3d(-276.1465439523065, 517.24043062274154, 0.0)
    viewCenter33 = NXOpen.Point3d(276.14654395230679, -517.24043062274154, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint33, viewCenter33)
    
    scaleAboutPoint34 = NXOpen.Point3d(-347.32053709171657, 655.09996688376157, 0.0)
    viewCenter34 = NXOpen.Point3d(347.32053709171737, -655.09996688376145, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint34, viewCenter34)
    
    scaleAboutPoint35 = NXOpen.Point3d(-717.35049391635334, 418.1204927296439, 0.0)
    viewCenter35 = NXOpen.Point3d(717.35049391635437, -418.1204927296439, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint35, viewCenter35)
    
    rotMatrix4 = NXOpen.Matrix3x3()
    
    rotMatrix4.Xx = 0.82270082922569854
    rotMatrix4.Xy = 0.42369801322802919
    rotMatrix4.Xz = 0.37900308597421334
    rotMatrix4.Yx = -0.53734683406868033
    rotMatrix4.Yy = 0.36201443299023151
    rotMatrix4.Yz = 0.76171118557044415
    rotMatrix4.Zx = 0.18553092870926097
    rotMatrix4.Zy = -0.83031653234979552
    rotMatrix4.Zz = 0.52550236022200392
    translation4 = NXOpen.Point3d(-410.35780775887497, 636.52583640070179, -120.61578171938896)
    workPart.ModelingViews.WorkView.SetRotationTranslationScale(rotMatrix4, translation4, 0.079225567532162228)
    
    scaleAboutPoint36 = NXOpen.Point3d(-612.82037073394201, 395.74503505161982, 0.0)
    viewCenter36 = NXOpen.Point3d(612.8203707339427, -395.74503505161982, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint36, viewCenter36)
    
    scaleAboutPoint37 = NXOpen.Point3d(-1027.2672808597315, 124.23388442126796, 0.0)
    viewCenter37 = NXOpen.Point3d(1027.267280859732, -124.23388442126796, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint37, viewCenter37)
    
    scaleAboutPoint38 = NXOpen.Point3d(-1297.4425832704994, 148.61311442866759, 0.0)
    viewCenter38 = NXOpen.Point3d(1297.442583270501, -148.61311442866699, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint38, viewCenter38)
    
    scaleAboutPoint39 = NXOpen.Point3d(-1613.4541777157269, 189.94091872203299, 0.0)
    viewCenter39 = NXOpen.Point3d(1613.4541777157283, -189.94091872203262, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint39, viewCenter39)
    
    scaleAboutPoint40 = NXOpen.Point3d(-1171.4762706894585, 1432.3841260768668, 0.0)
    viewCenter40 = NXOpen.Point3d(1171.4762706894603, -1432.3841260768663, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint40, viewCenter40)
    
    scaleAboutPoint41 = NXOpen.Point3d(-937.18101655156624, 1145.9073008614935, 0.0)
    viewCenter41 = NXOpen.Point3d(937.18101655156852, -1145.907300861493, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint41, viewCenter41)
    
    scaleAboutPoint42 = NXOpen.Point3d(-749.74481324125304, 920.06546123815394, 0.0)
    viewCenter42 = NXOpen.Point3d(749.74481324125486, -920.06546123815326, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint42, viewCenter42)
    
    scaleAboutPoint43 = NXOpen.Point3d(-599.79585059300268, 736.05236899052284, 0.0)
    viewCenter43 = NXOpen.Point3d(599.79585059300359, -736.05236899052284, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint43, viewCenter43)
    
    # ----------------------------------------------
    #   Меню: Симуляция->Анимация->Воспроизвести
    # ----------------------------------------------
    scaleAboutPoint44 = NXOpen.Point3d(-191.29346504436057, 41.678464451005858, 0.0)
    viewCenter44 = NXOpen.Point3d(191.29346504436094, -41.678464451006249, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint44, viewCenter44)
    
    scaleAboutPoint45 = NXOpen.Point3d(-153.03477203548846, 33.342771560804692, 0.0)
    viewCenter45 = NXOpen.Point3d(153.03477203548877, -33.342771560804998, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint45, viewCenter45)
    
    scaleAboutPoint46 = NXOpen.Point3d(-134.73899482007249, 7.5234971726942765, 0.0)
    viewCenter46 = NXOpen.Point3d(134.73899482007297, -7.5234971726945226, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint46, viewCenter46)
    
    scaleAboutPoint47 = NXOpen.Point3d(-136.24369425461128, -31.188315552260626, 0.0)
    viewCenter47 = NXOpen.Point3d(136.2436942546118, 31.188315552260431, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint47, viewCenter47)
    
    scaleAboutPoint48 = NXOpen.Point3d(-239.43871729244185, -102.86672497938567, 0.0)
    viewCenter48 = NXOpen.Point3d(239.43871729244231, 102.86672497938542, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint48, viewCenter48)
    
    scaleAboutPoint49 = NXOpen.Point3d(-300.8085676843985, -171.2402672848325, 0.0)
    viewCenter49 = NXOpen.Point3d(300.80856768439901, 171.24026728483219, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint49, viewCenter49)
    
    scaleAboutPoint50 = NXOpen.Point3d(-241.76744485367723, -138.11280453402449, 0.0)
    viewCenter50 = NXOpen.Point3d(241.76744485367774, 138.11280453402421, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint50, viewCenter50)
    
    scaleAboutPoint51 = NXOpen.Point3d(-194.75866473033187, -110.49024362721961, 0.0)
    viewCenter51 = NXOpen.Point3d(194.75866473033227, 110.49024362721933, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint51, viewCenter51)
    
    scaleAboutPoint52 = NXOpen.Point3d(-160.46858912188435, -89.467961979687757, 0.0)
    viewCenter52 = NXOpen.Point3d(160.4685891218848, 89.467961979687502, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint52, viewCenter52)
    
    scaleAboutPoint53 = NXOpen.Point3d(-157.06199337516281, -31.125527454256169, 0.0)
    viewCenter53 = NXOpen.Point3d(157.06199337516324, 31.125527454255913, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint53, viewCenter53)
    
    scaleAboutPoint54 = NXOpen.Point3d(-125.64959470013021, -24.90042196340498, 0.0)
    viewCenter54 = NXOpen.Point3d(125.64959470013066, 24.900421963404689, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint54, viewCenter54)
    
    scaleAboutPoint55 = NXOpen.Point3d(-100.51967576010411, -19.920337570724019, 0.0)
    viewCenter55 = NXOpen.Point3d(100.5196757601046, 19.920337570723721, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint55, viewCenter55)
    
    scaleAboutPoint56 = NXOpen.Point3d(-62.937250868609368, -9.62051325996263, 0.0)
    viewCenter56 = NXOpen.Point3d(62.937250868609922, 9.6205132599623511, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint56, viewCenter56)
    
    scaleAboutPoint57 = NXOpen.Point3d(-37.362107793938051, 10.556860924577027, 0.0)
    viewCenter57 = NXOpen.Point3d(37.362107793938613, -10.556860924577325, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint57, viewCenter57)
    
    scaleAboutPoint58 = NXOpen.Point3d(-47.391125672286364, 12.278088249236349, 0.0)
    viewCenter58 = NXOpen.Point3d(47.39112567228694, -12.278088249236637, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint58, viewCenter58)
    
    scaleAboutPoint59 = NXOpen.Point3d(-61.820748077347027, 13.913254207662693, 0.0)
    viewCenter59 = NXOpen.Point3d(61.820748077347496, -13.913254207663003, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint59, viewCenter59)
    
    scaleAboutPoint60 = NXOpen.Point3d(-79.427469252507962, 15.957211655695625, 0.0)
    viewCenter60 = NXOpen.Point3d(79.427469252508516, -15.957211655695948, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint60, viewCenter60)
    
    simulationOptionsBuilder2 = isvControlPanelBuilder2.SimulationOptionsBuilder
    
    isvControlPanelBuilder2.SetSpeed(6)
    
    simulationOptionsBuilder3 = isvControlPanelBuilder2.SimulationOptionsBuilder
    
    isvControlPanelBuilder2.SetSpeed(5)
    
    simulationOptionsBuilder4 = isvControlPanelBuilder2.SimulationOptionsBuilder
    
    isvControlPanelBuilder2.SetSpeed(6)
    
    simulationOptionsBuilder5 = isvControlPanelBuilder2.SimulationOptionsBuilder
    
    isvControlPanelBuilder2.SetSpeed(7)
    
    simulationOptionsBuilder6 = isvControlPanelBuilder2.SimulationOptionsBuilder
    
    isvControlPanelBuilder2.SetSpeed(8)
    
    simulationOptionsBuilder7 = isvControlPanelBuilder2.SimulationOptionsBuilder
    
    isvControlPanelBuilder2.SetSpeed(9)
    
    simulationOptionsBuilder8 = isvControlPanelBuilder2.SimulationOptionsBuilder
    
    isvControlPanelBuilder2.SetSpeed(10)
    
    simulationOptionsBuilder9 = isvControlPanelBuilder2.SimulationOptionsBuilder
    
    isvControlPanelBuilder2.SetSpeed(10)
    
    isvControlPanelBuilder2.PlayForward()
    
    # ----------------------------------------------
    #   Меню: Симуляция->ЗвПО->Создать фасетное тело для ЗвПО
    # ----------------------------------------------
    # ----------------------------------------------
    #   Меню: Задача->Завершить симуляцию...
    # ----------------------------------------------
    isvControlPanelBuilder2.Destroy()
    
    theSession.DeleteUndoMarksSetInTaskEnvironment()
    
    theSession.EndTaskEnvironment()
    
    scaleAboutPoint61 = NXOpen.Point3d(-193.41395588294171, -61.18425255624949, 0.0)
    viewCenter61 = NXOpen.Point3d(193.41395588294228, 61.184252556249163, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint61, viewCenter61)
    
    scaleAboutPoint62 = NXOpen.Point3d(-241.76744485367718, -77.600906401470226, 0.0)
    viewCenter62 = NXOpen.Point3d(241.76744485367769, 77.600906401469828, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint62, viewCenter62)
    
    scaleAboutPoint63 = NXOpen.Point3d(-302.20930606709652, -98.401871384535795, 0.0)
    viewCenter63 = NXOpen.Point3d(302.20930606709703, 98.401871384535283, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint63, viewCenter63)
    
    scaleAboutPoint64 = NXOpen.Point3d(-377.76163258387089, -124.75326220904221, 0.0)
    viewCenter64 = NXOpen.Point3d(377.76163258387135, 124.75326220904172, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint64, viewCenter64)
    
    scaleAboutPoint65 = NXOpen.Point3d(-514.8807883276686, -176.73378812947632, 0.0)
    viewCenter65 = NXOpen.Point3d(514.88078832766917, 176.73378812947593, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint65, viewCenter65)
    
    scaleAboutPoint66 = NXOpen.Point3d(-411.90463066213493, -141.38703050358114, 0.0)
    viewCenter66 = NXOpen.Point3d(411.90463066213545, 141.38703050358069, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint66, viewCenter66)
    
    scaleAboutPoint67 = NXOpen.Point3d(-329.5237045297078, -113.10962440286498, 0.0)
    viewCenter67 = NXOpen.Point3d(329.52370452970831, 113.10962440286447, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint67, viewCenter67)
    
    scaleAboutPoint68 = NXOpen.Point3d(-263.61896362376626, -90.487699522292047, 0.0)
    viewCenter68 = NXOpen.Point3d(263.61896362376677, 90.487699522291592, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint68, viewCenter68)
    
    scaleAboutPoint69 = NXOpen.Point3d(-210.89517089901301, -72.390159617833675, 0.0)
    viewCenter69 = NXOpen.Point3d(210.89517089901341, 72.390159617833191, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint69, viewCenter69)
    
    scaleAboutPoint70 = NXOpen.Point3d(-160.11000009591368, -50.381758148882426, 0.0)
    viewCenter70 = NXOpen.Point3d(160.11000009591424, 50.381758148881978, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint70, viewCenter70)
    
    scaleAboutPoint71 = NXOpen.Point3d(-126.65364397284812, -39.157921635999827, 0.0)
    viewCenter71 = NXOpen.Point3d(126.65364397284866, 39.157921635999337, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint71, viewCenter71)
    
    # ----------------------------------------------
    #   Меню: Изменить->Показать и скрыть->Скрыть...
    # ----------------------------------------------
    theSession.CAMSession.PathDisplay.HideToolPath(nCGroup5)
    
    markId44 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    theSession.SetUndoMarkName(markId44, "Диалоговое окно Выбор по классу")
    
    markId45 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Выбор по классу")
    
    theSession.DeleteUndoMark(markId45, None)
    
    markId46 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Выбор по классу")
    
    theSession.DeleteUndoMark(markId46, None)
    
    theSession.SetUndoMarkName(markId44, "Выбор по классу")
    
    theSession.DeleteUndoMark(markId44, None)
    
    markId47 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Hide")
    
    objects2 = [NXOpen.DisplayableObject.Null] * 1 
    objects2[0] = body1
    theSession.DisplayManager.BlankObjects(objects2)
    
    workPart.ModelingViews.WorkView.FitAfterShowOrHide(NXOpen.View.ShowOrHideType.HideOnly)
    
    scaleAboutPoint72 = NXOpen.Point3d(-94.208508903019933, -2.8687122077657898, 0.0)
    viewCenter72 = NXOpen.Point3d(94.20850890302043, 2.8687122077652942, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint72, viewCenter72)
    
    scaleAboutPoint73 = NXOpen.Point3d(-75.550404703712871, -3.7637504165886275, 0.0)
    viewCenter73 = NXOpen.Point3d(75.550404703713369, 3.7637504165881319, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint73, viewCenter73)
    
    scaleAboutPoint74 = NXOpen.Point3d(-60.440323762970245, -4.186024853571725, 0.0)
    viewCenter74 = NXOpen.Point3d(60.44032376297077, 4.1860248535712232, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint74, viewCenter74)
    
    scaleAboutPoint75 = NXOpen.Point3d(-48.46976146240624, -5.2288591153386452, 0.0)
    viewCenter75 = NXOpen.Point3d(48.469761462406751, 5.2288591153381478, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint75, viewCenter75)
    
    scaleAboutPoint76 = NXOpen.Point3d(-38.681807208300874, -5.1231069085115744, 0.0)
    viewCenter76 = NXOpen.Point3d(38.681807208301379, 5.1231069085110752, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint76, viewCenter76)
    
    # ----------------------------------------------
    #   Меню: Файл->Экспорт->Деталь...
    # ----------------------------------------------
    # ----------------------------------------------
    #   Начало меню Экспорт детали
    # ----------------------------------------------
    # ----------------------------------------------
    #   Начало меню Экспорт детали
    # ----------------------------------------------
    # ----------------------------------------------
    #   Меню: Файл->Экспорт->STEP...
    # ----------------------------------------------
    markId48 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Начало")
    
    stepCreator1 = theSession.DexManager.CreateStepCreator()
    
    stepCreator1.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap214
    
    stepCreator1.ObjectTypes.Solids = True
    
    stepCreator1.InputFile = "C:\\Users\\ринат\\CHPU\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1.prt"
    
    stepCreator1.OutputFile = "C:\\ProgramData\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1.stp"
    
    theSession.SetUndoMarkName(markId48, "Диалоговое окно Экспорт файла STEP")
    
    stepCreator1.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap242
    
    stepCreator1.ObjectTypes.Curves = True
    
    stepCreator1.ObjectTypes.Surfaces = True
    
    stepCreator1.ObjectTypes.PmiData = True
    
    stepCreator1.ObjectTypes.FacetBodies = True
    
    stepCreator1.SettingsFile = "C:\\Program Files\\Siemens\\NX2406\\translators\\step242\\ugstep242.def"
    
    scaleAboutPoint77 = NXOpen.Point3d(-25.98214219289024, -10.415417347946216, 0.0)
    viewCenter77 = NXOpen.Point3d(25.982142192890741, 10.415417347945708, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint77, viewCenter77)
    
    scaleAboutPoint78 = NXOpen.Point3d(-32.477677741112863, -13.019271684932709, 0.0)
    viewCenter78 = NXOpen.Point3d(32.477677741113339, 13.019271684932201, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint78, viewCenter78)
    
    scaleAboutPoint79 = NXOpen.Point3d(-40.597097176391152, -16.156587154135757, 0.0)
    viewCenter79 = NXOpen.Point3d(40.5970971763916, 16.156587154135238, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint79, viewCenter79)
    
    scaleAboutPoint80 = NXOpen.Point3d(-50.746371470489002, -20.195733942669627, 0.0)
    viewCenter80 = NXOpen.Point3d(50.74637147048945, 20.195733942669111, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint80, viewCenter80)
    
    scaleAboutPoint81 = NXOpen.Point3d(-13.494422225328885, -24.69387468444598, 0.0)
    viewCenter81 = NXOpen.Point3d(13.494422225329281, 24.6938746844455, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint81, viewCenter81)
    
    scaleAboutPoint82 = NXOpen.Point3d(-16.868027781661169, -30.867343355557413, 0.0)
    viewCenter82 = NXOpen.Point3d(16.868027781661539, 30.867343355556919, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint82, viewCenter82)
    
    scaleAboutPoint83 = NXOpen.Point3d(-21.085034727076462, -38.584179194446719, 0.0)
    viewCenter83 = NXOpen.Point3d(21.085034727076899, 38.584179194446229, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint83, viewCenter83)
    
    scaleAboutPoint84 = NXOpen.Point3d(-26.356293408845605, -48.230223993058296, 0.0)
    viewCenter84 = NXOpen.Point3d(26.35629340884606, 48.230223993057848, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint84, viewCenter84)
    
    markId49 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Экспорт файла STEP")
    
    theSession.DeleteUndoMark(markId49, None)
    
    markId50 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Экспорт файла STEP")
    
    stepCreator1.OutputFile = "C:\\ProgramData\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1.stp"
    
    stepCreator1.FileSaveFlag = False
    
    theSession.DeleteUndoMark(markId50, None)
    
    theSession.SetUndoMarkName(markId48, "Экспорт файла STEP")
    
    stepCreator1.Destroy()
    
    # ----------------------------------------------
    #   Меню: Файл->Экспорт->STEP...
    # ----------------------------------------------
    markId51 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Начало")
    
    stepCreator2 = theSession.DexManager.CreateStepCreator()
    
    stepCreator2.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap214
    
    stepCreator2.SettingsFile = "C:\\Program Files\\Siemens\\NX2406\\translators\\step242\\ugstep242.def"
    
    stepCreator2.ObjectTypes.Curves = True
    
    stepCreator2.ObjectTypes.Surfaces = True
    
    stepCreator2.ObjectTypes.Solids = True
    
    stepCreator2.ObjectTypes.FacetBodies = True
    
    stepCreator2.ObjectTypes.PmiData = True
    
    stepCreator2.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap242
    
    stepCreator2.InputFile = "C:\\Users\\ринат\\CHPU\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1.prt"
    
    stepCreator2.OutputFile = "C:\\ProgramData\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1.stp"
    
    theSession.SetUndoMarkName(markId51, "Диалоговое окно Экспорт файла STEP")
    
    scaleAboutPoint85 = NXOpen.Point3d(-56.701889731615438, -62.080725121176251, 0.0)
    viewCenter85 = NXOpen.Point3d(56.701889731615843, 62.080725121175767, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint85, viewCenter85)
    
    scaleAboutPoint86 = NXOpen.Point3d(-45.361511785292322, -51.09893620082385, 0.0)
    viewCenter86 = NXOpen.Point3d(45.361511785292649, 51.098936200823331, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint86, viewCenter86)
    
    scaleAboutPoint87 = NXOpen.Point3d(-36.289209428233811, -40.87914896065913, 0.0)
    viewCenter87 = NXOpen.Point3d(36.289209428234173, 40.879148960658618, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint87, viewCenter87)
    
    scaleAboutPoint88 = NXOpen.Point3d(-26.506900799753378, -30.867343355557409, 0.0)
    viewCenter88 = NXOpen.Point3d(26.506900799753708, 30.867343355556912, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint88, viewCenter88)
    
    scaleAboutPoint89 = NXOpen.Point3d(-21.021923058505674, -24.87747226574297, 0.0)
    viewCenter89 = NXOpen.Point3d(21.021923058506005, 24.87747226574249, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint89, viewCenter89)
    
    scaleAboutPoint90 = NXOpen.Point3d(-16.523782316729307, -20.489490072744786, 0.0)
    viewCenter90 = NXOpen.Point3d(16.523782316729648, 20.48949007274431, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint90, viewCenter90)
    
    markId52 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Экспорт файла STEP")
    
    theSession.DeleteUndoMark(markId52, None)
    
    markId53 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Экспорт файла STEP")
    
    stepCreator2.OutputFile = "C:\\Users\\ринат\\CHPU\\75.6121.0.0411.003-A-CAM-DMC-635_finish.stp"
    
    stepCreator2.FileSaveFlag = False
    
    theSession.DeleteUndoMark(markId53, None)
    
    theSession.SetUndoMarkName(markId51, "Экспорт файла STEP")
    
    stepCreator2.Destroy()
    
    # ----------------------------------------------
    #   Меню: Файл->Экспорт->STEP...
    # ----------------------------------------------
    markId54 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Начало")
    
    stepCreator3 = theSession.DexManager.CreateStepCreator()
    
    stepCreator3.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap214
    
    stepCreator3.SettingsFile = "C:\\Program Files\\Siemens\\NX2406\\translators\\step242\\ugstep242.def"
    
    stepCreator3.ObjectTypes.Curves = True
    
    stepCreator3.ObjectTypes.Surfaces = True
    
    stepCreator3.ObjectTypes.Solids = True
    
    stepCreator3.ObjectTypes.FacetBodies = True
    
    stepCreator3.ObjectTypes.PmiData = True
    
    stepCreator3.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap242
    
    stepCreator3.InputFile = "C:\\Users\\ринат\\CHPU\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1.prt"
    
    stepCreator3.OutputFile = "C:\\Users\\ринат\\CHPU\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1.stp"
    
    theSession.SetUndoMarkName(markId54, "Диалоговое окно Экспорт файла STEP")
    
    markId55 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Экспорт файла STEP")
    
    theSession.DeleteUndoMark(markId55, None)
    
    markId56 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Экспорт файла STEP")
    
    stepCreator3.OutputFile = "C:\\Users\\ринат\\CHPU\\75.6121.0.0411.003-A-CAM-DMC-635_1_zag_oriented_1_finish.stp"
    
    stepCreator3.FileSaveFlag = False
    
    stepCreator3.LayerMask = "1-256"
    
    stepCreator3.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap242ED2
    
    stepCreator3.ProcessHoldFlag = True
    
    nXObject10 = stepCreator3.Commit()
    
    theSession.DeleteUndoMark(markId56, None)
    
    stepCreator3.Destroy()
    
    # ----------------------------------------------
    #   Меню: Инструменты->Автоматизация->Журнал->Остановка записи
    # ----------------------------------------------
    
if __name__ == '__main__':
    main()