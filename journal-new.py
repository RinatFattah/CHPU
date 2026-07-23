# NX CAM 2406
# Journal created by ринат on Tue Jul 21 08:41:15 2026 Горное время США (лето)

#
import math
import NXOpen
import NXOpen.CAM
import NXOpen.SIM
def main() : 

    theSession  = NXOpen.Session.GetSession() #type: NXOpen.Session
    workPart = theSession.Parts.Work
    displayPart = theSession.Parts.Display
    nCGroup1 = workPart.CAMSetup.CAMGroupCollection.FindObject("NC_PROGRAM")
    theSession.CAMSession.PathDisplay.ShowToolPath(nCGroup1)
    
    markId1 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Generate Tool Paths")
    
    objects1 = [NXOpen.CAM.CAMObject.Null] * 1 
    objects1[0] = nCGroup1
    workPart.CAMSetup.GenerateToolPath(objects1)
    
    scaleAboutPoint1 = NXOpen.Point3d(-88.596017178424802, 22.205845195274673, 0.0)
    viewCenter1 = NXOpen.Point3d(88.596017178429577, -22.205845195279938, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint1, viewCenter1)
    
    scaleAboutPoint2 = NXOpen.Point3d(-67.54593696344773, 19.610110731321601, 0.0)
    viewCenter2 = NXOpen.Point3d(67.545936963452576, -19.610110731326888, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint2, viewCenter2)
    
    scaleAboutPoint3 = NXOpen.Point3d(-84.432421204310273, 24.512638414152686, 0.0)
    viewCenter3 = NXOpen.Point3d(84.432421204315091, -24.512638414157951, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint3, viewCenter3)
    
    scaleAboutPoint4 = NXOpen.Point3d(-105.54052650538846, 30.640798017691456, 0.0)
    viewCenter4 = NXOpen.Point3d(105.54052650539319, -30.640798017696781, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint4, viewCenter4)
    
    scaleAboutPoint5 = NXOpen.Point3d(-131.92565813173616, 38.30099752211499, 0.0)
    viewCenter5 = NXOpen.Point3d(131.92565813174096, -38.300997522120284, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint5, viewCenter5)
    
    markId2 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Enter Simulate Machine")
    
    theSession.BeginTaskEnvironment()
    
    theSession.CAMSession.PathDisplay.HideToolPath(nCGroup1)
    
    kinematicConfigurator1 = workPart.KinematicConfigurator
    
    ncChannelSelectionData1 = kinematicConfigurator1.CreateNcChannelSelectionData()
    
    markId3 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Начало")
    
    theSession.SetUndoMarkName(markId3, "Диалоговое окно Геометрия заготовки")
    
    # ----------------------------------------------
    #   Начало меню Геометрия заготовки
    # ----------------------------------------------
    markId4 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Геометрия заготовки")
    
    theSession.DeleteUndoMark(markId4, None)
    
    markId5 = theSession.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "Геометрия заготовки")
    
    theSession.DeleteUndoMark(markId5, None)
    
    theSession.SetUndoMarkName(markId3, "Геометрия заготовки")
    
    theSession.DeleteUndoMark(markId3, None)
    
    isvControlPanelBuilder1 = kinematicConfigurator1.CreateIsvControlPanelBuilder(NXOpen.SIM.IsvControlPanelBuilder.VisualizationType.MachineCodeSimulateCse, ncChannelSelectionData1)
    
    # ----------------------------------------------
    #   Меню: Симуляция->ЗвПО->Создать фасетное тело для ЗвПО
    # ----------------------------------------------
    scaleAboutPoint6 = NXOpen.Point3d(-123.73812605273274, 96.908924889896454, 0.0)
    viewCenter6 = NXOpen.Point3d(123.73812605273747, -96.908924889901783, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint6, viewCenter6)
    
    scaleAboutPoint7 = NXOpen.Point3d(-154.09444202361402, 119.40150948546372, 0.0)
    viewCenter7 = NXOpen.Point3d(154.09444202361871, -119.40150948546903, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint7, viewCenter7)
    
    scaleAboutPoint8 = NXOpen.Point3d(-197.67743852466492, 121.78664859746148, 0.0)
    viewCenter8 = NXOpen.Point3d(197.6774385246697, -121.78664859746681, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint8, viewCenter8)
    
    scaleAboutPoint9 = NXOpen.Point3d(-388.03683659206615, -38.397125856028119, 0.0)
    viewCenter9 = NXOpen.Point3d(388.03683659207104, 38.397125856022669, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint9, viewCenter9)
    
    scaleAboutPoint10 = NXOpen.Point3d(-472.62344619842804, -52.513716244272644, 0.0)
    viewCenter10 = NXOpen.Point3d(472.62344619843293, 52.513716244267258, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint10, viewCenter10)
    
    scaleAboutPoint11 = NXOpen.Point3d(-575.25105832096654, -67.053804344164632, 0.0)
    viewCenter11 = NXOpen.Point3d(575.2510583209712, 67.053804344159417, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint11, viewCenter11)
    
    scaleAboutPoint12 = NXOpen.Point3d(-692.59521592325007, -82.052681631674403, 0.0)
    viewCenter12 = NXOpen.Point3d(692.59521592325461, 82.052681631669174, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint12, viewCenter12)
    
    scaleAboutPoint13 = NXOpen.Point3d(-823.83539218896215, -84.920114054286714, 0.0)
    viewCenter13 = NXOpen.Point3d(823.83539218896613, 84.920114054281555, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(0.80000000000000004, scaleAboutPoint13, viewCenter13)
    
    scaleAboutPoint14 = NXOpen.Point3d(-1247.6088184923215, -329.47901394438418, 0.0)
    viewCenter14 = NXOpen.Point3d(1247.608818492326, 329.47901394437923, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint14, viewCenter14)
    
    scaleAboutPoint15 = NXOpen.Point3d(-1004.7042065383464, -272.40608014816047, 0.0)
    viewCenter15 = NXOpen.Point3d(1004.7042065383508, 272.4060801481557, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint15, viewCenter15)
    
    scaleAboutPoint16 = NXOpen.Point3d(-807.29251282773771, -221.45401171559004, 0.0)
    viewCenter16 = NXOpen.Point3d(807.29251282774248, 221.45401171558512, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint16, viewCenter16)
    
    scaleAboutPoint17 = NXOpen.Point3d(-645.83401026218951, -178.57486841129713, 0.0)
    viewCenter17 = NXOpen.Point3d(645.83401026219462, 178.57486841129204, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint17, viewCenter17)
    
    scaleAboutPoint18 = NXOpen.Point3d(-516.66720820975127, -143.98922196009778, 0.0)
    viewCenter18 = NXOpen.Point3d(516.66720820975627, 143.98922196009272, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint18, viewCenter18)
    
    scaleAboutPoint19 = NXOpen.Point3d(-413.33376656780035, -115.19137756807862, 0.0)
    viewCenter19 = NXOpen.Point3d(413.33376656780536, 115.19137756807366, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint19, viewCenter19)
    
    scaleAboutPoint20 = NXOpen.Point3d(-186.83589710649304, -20.598928694529064, 0.0)
    viewCenter20 = NXOpen.Point3d(186.83589710649787, 20.598928694524055, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint20, viewCenter20)
    
    scaleAboutPoint21 = NXOpen.Point3d(-110.15006080862388, 9.5405564479887595, 0.0)
    viewCenter21 = NXOpen.Point3d(110.15006080862877, -9.5405564479937528, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint21, viewCenter21)
    
    scaleAboutPoint22 = NXOpen.Point3d(-58.977985314852553, 20.121900872124524, 0.0)
    viewCenter22 = NXOpen.Point3d(58.97798531485742, -20.121900872129476, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint22, viewCenter22)
    
    scaleAboutPoint23 = NXOpen.Point3d(-42.00157699285122, 19.057984274287957, 0.0)
    viewCenter23 = NXOpen.Point3d(42.001576992856016, -19.057984274292885, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint23, viewCenter23)
    
    scaleAboutPoint24 = NXOpen.Point3d(-33.305215236621613, 15.542433777088753, 0.0)
    viewCenter24 = NXOpen.Point3d(33.30521523662641, -15.542433777093683, 0.0)
    workPart.ModelingViews.WorkView.ZoomAboutPoint(1.25, scaleAboutPoint24, viewCenter24)
    
    # ----------------------------------------------
    #   Меню: Задача->Завершить симуляцию...
    # ----------------------------------------------
    isvControlPanelBuilder1.Destroy()
    
    theSession.DeleteUndoMarksSetInTaskEnvironment()
    
    theSession.EndTaskEnvironment()
    
    # ----------------------------------------------
    #   Меню: Инструменты->Автоматизация->Журнал->Остановка записи
    # ----------------------------------------------
    
if __name__ == '__main__':
    main()