import copy
from cereal import car
from selfdrive.car.hyundai.values import DBC, STEER_THRESHOLD, FEATURES, EV_CAR, HYBRID_CAR
from selfdrive.car.interfaces import CarStateBase
from opendbc.can.parser import CANParser
from opendbc.can.can_define import CANDefine
from selfdrive.config import Conversions as CV
from selfdrive.car.hyundai.spdcontroller  import SpdController
from common.numpy_fast import interp
from common.params import Params

GearShifter = car.CarState.GearShifter


class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    can_define = CANDefine(DBC[CP.carFingerprint]["pt"])

    if self.CP.carFingerprint in FEATURES["use_cluster_gears"]:
      self.shifter_values = can_define.dv["CLU15"]["CF_Clu_Gear"]
    elif self.CP.carFingerprint in FEATURES["use_tcu_gears"]:
      self.shifter_values = can_define.dv["TCU12"]["CUR_GR"]
    else:  # preferred and elect gear methods use same definition
      self.shifter_values = can_define.dv["LVR12"]["CF_Lvr_Gear"]

    #Auto detection for setup
    self.no_radar = CP.sccBus == -1
    self.lkas_button_on = True
    self.cruise_main_button = 0
    self.mdps_error_cnt = 0

    self.acc_active = False
    self.cruiseState_standstill = False
    
    self.cruiseState_modeSel = 0
    self.SC = SpdController()

    self.lfahda = None

    self.driverAcc_time = 0
    
    self.steer_anglecorrection = float(int(Params().get("OpkrSteerAngleCorrection", encoding="utf8")) * 0.1)
    self.gear_correction = Params().get_bool("JustDoGearD")
    self.steer_wind_down = Params().get_bool("SteerWindDown")
    self.brake_check = False
    self.cancel_check = False
    self.safety_sign_check = 0
    self.safety_sign = 0
    self.safety_sign_last = 0
    self.safety_dist = 0
    self.safety_block_remain_dist = 0
    self.is_highway = False
    self.on_speed_control = False
    self.safetycam_decel_dist_gain = int(Params().get("SafetyCamDecelDistGain", encoding="utf8"))

  def update(self, cp, cp2, cp_cam):
    cp_mdps = cp2 if self.CP.mdpsBus == 1 else cp
    cp_sas = cp2 if self.CP.sasBus else cp
    cp_scc = cp_cam if ((self.CP.sccBus == 2) or self.CP.radarOffCan) else cp
    cp_fca = cp_cam if (self.CP.fcaBus == 2) else cp

    self.prev_cruise_buttons = self.cruise_buttons
    self.prev_cruise_main_button = self.cruise_main_button
    self.prev_lkas_button_on = self.lkas_button_on

    ret = car.CarState.new_message()

    ret.doorOpen = any([cp.vl["CGW1"]["CF_Gway_DrvDrSw"], cp.vl["CGW1"]["CF_Gway_AstDrSw"],
                        cp.vl["CGW2"]["CF_Gway_RLDrSw"], cp.vl["CGW2"]["CF_Gway_RRDrSw"]])

    ret.seatbeltUnlatched = cp.vl["CGW1"]["CF_Gway_DrvSeatBeltSw"] == 0

    ret.wheelSpeeds.fl = cp.vl["WHL_SPD11"]["WHL_SPD_FL"] * CV.KPH_TO_MS
    ret.wheelSpeeds.fr = cp.vl["WHL_SPD11"]["WHL_SPD_FR"] * CV.KPH_TO_MS
    ret.wheelSpeeds.rl = cp.vl["WHL_SPD11"]["WHL_SPD_RL"] * CV.KPH_TO_MS
    ret.wheelSpeeds.rr = cp.vl["WHL_SPD11"]["WHL_SPD_RR"] * CV.KPH_TO_MS
    ret.vEgoRaw = (ret.wheelSpeeds.fl + ret.wheelSpeeds.fr + ret.wheelSpeeds.rl + ret.wheelSpeeds.rr) / 4.
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    ret.vEgoOP = ret.vEgo

    ret.vEgo = cp.vl["CLU11"]["CF_Clu_Vanz"] * CV.MPH_TO_MS if bool(cp.vl["CLU11"]["CF_Clu_SPEED_UNIT"]) else cp.vl["CLU11"]["CF_Clu_Vanz"] * CV.KPH_TO_MS

    ret.standstill = ret.vEgoRaw < 0.1
    ret.standStill = self.CP.standStill

    ret.steeringAngleDeg = cp_sas.vl["SAS11"]["SAS_Angle"] - self.steer_anglecorrection
    ret.steeringRateDeg = cp_sas.vl["SAS11"]["SAS_Speed"]
    ret.yawRate = cp.vl["ESP12"]["YAW_RATE"]
    ret.leftBlinker, ret.rightBlinker = self.update_blinker_from_lamp(
      50, cp.vl["CGW1"]["CF_Gway_TurnSigLh"], cp.vl["CGW1"]["CF_Gway_TurnSigRh"])
    ret.steeringTorque = cp_mdps.vl["MDPS12"]["CR_Mdps_StrColTq"]
    ret.steeringTorqueEps = cp_mdps.vl["MDPS12"]["CR_Mdps_OutTq"]
    ret.steeringPressed = abs(ret.steeringTorque) > STEER_THRESHOLD

    if self.steer_wind_down:
      ret.steerWarning = cp_mdps.vl["MDPS12"]["CF_Mdps_ToiUnavail"] != 0 or cp_mdps.vl["MDPS12"]["CF_Mdps_ToiFlt"] != 0
    else:
      self.mdps_error_cnt += 1 if cp_mdps.vl["MDPS12"]["CF_Mdps_ToiUnavail"] != 0 else -self.mdps_error_cnt
      ret.steerWarning = self.mdps_error_cnt > 100 #cp_mdps.vl["MDPS12"]["CF_Mdps_ToiUnavail"] != 0

    self.VSetDis = cp_scc.vl["SCC11"]["VSetDis"]
    ret.vSetDis = self.VSetDis
    self.clu_Vanz = cp.vl["CLU11"]["CF_Clu_Vanz"]
    lead_objspd = cp_scc.vl["SCC11"]["ACC_ObjRelSpd"]
    self.lead_objspd = lead_objspd * CV.MS_TO_KPH
    self.Mdps_ToiUnavail = cp_mdps.vl["MDPS12"]["CF_Mdps_ToiUnavail"]
    self.driverOverride = cp.vl["TCS13"]["DriverOverride"]
    if self.driverOverride == 1:
      self.driverAcc_time = 100
    elif self.driverAcc_time:
      self.driverAcc_time -= 1

    # cruise state
    ret.cruiseState.enabled = (cp_scc.vl["SCC12"]["ACCMode"] != 0) if not self.no_radar else \
                                      cp.vl["LVR12"]["CF_Lvr_CruiseSet"] != 0
    ret.cruiseState.available = (cp_scc.vl["SCC11"]["MainMode_ACC"] != 0) if not self.no_radar else \
                                      cp.vl["EMS16"]["CRUISE_LAMP_M"] != 0


    ret.cruiseState.standstill = cp_scc.vl["SCC11"]["SCCInfoDisplay"] == 4. if not self.no_radar else False
    self.cruiseState_standstill = ret.cruiseState.standstill
    self.is_set_speed_in_mph = bool(cp.vl["CLU11"]["CF_Clu_SPEED_UNIT"])
    ret.isMph = self.is_set_speed_in_mph
    
    self.acc_active = ret.cruiseState.enabled
    if self.acc_active:
      self.brake_check = False
      self.cancel_check = False

    self.cruiseState_modeSel, speed_kph = self.SC.update_cruiseSW(self)
    ret.cruiseState.modeSel = self.cruiseState_modeSel

    if ret.cruiseState.enabled and (self.brake_check == False or self.cancel_check == False):
      speed_conv = CV.MPH_TO_MS if self.is_set_speed_in_mph else CV.KPH_TO_MS
      ret.cruiseState.speed = speed_kph * speed_conv if not self.no_radar else \
                                         cp.vl["LVR12"]["CF_Lvr_CruiseSet"] * speed_conv
    else:
      ret.cruiseState.speed = 0

    self.cruise_main_button = cp.vl["CLU11"]["CF_Clu_CruiseSwMain"]
    self.cruise_buttons = cp.vl["CLU11"]["CF_Clu_CruiseSwState"]
    ret.cruiseButtons = self.cruise_buttons

    # TODO: Find brake pressure
    ret.brake = 0
    ret.brakePressed = cp.vl["TCS13"]["DriverBraking"] != 0

    if ret.brakePressed:
      self.brake_check = True
    if self.cruise_buttons == 4:
      self.cancel_check = True

    # TODO: Check this
    ret.brakeLights = bool(cp.vl["TCS13"]["BrakeLight"] or ret.brakePressed)

    if self.CP.carFingerprint in (HYBRID_CAR | EV_CAR):
      if self.CP.carFingerprint in HYBRID_CAR:
        ret.gas = cp.vl["E_EMS11"]["CR_Vcu_AccPedDep_Pos"] / 254.
      else:
        ret.gas = cp.vl["E_EMS11"]["Accel_Pedal_Pos"] / 254.
      ret.gasPressed = ret.gas > 0
    else:
      ret.gas = cp.vl["EMS12"]["PV_AV_CAN"] / 100.
      ret.gasPressed = bool(cp.vl["EMS16"]["CF_Ems_AclAct"])

    ret.espDisabled = (cp.vl["TCS15"]["ESC_Off_Step"] != 0)

    self.parkBrake = cp.vl["TCS13"]["PBRAKE_ACT"] == 1

    # TPMS code added from OPKR
    if cp.vl["TPMS11"]["UNIT"] == 0.0:
      ret.tpmsPressureFl = cp.vl["TPMS11"]["PRESSURE_FL"]
      ret.tpmsPressureFr = cp.vl["TPMS11"]["PRESSURE_FR"]
      ret.tpmsPressureRl = cp.vl["TPMS11"]["PRESSURE_RL"]
      ret.tpmsPressureRr = cp.vl["TPMS11"]["PRESSURE_RR"]
    elif cp.vl["TPMS11"]["UNIT"] == 1.0:
      ret.tpmsPressureFl = cp.vl["TPMS11"]["PRESSURE_FL"] * 5 * 0.145038
      ret.tpmsPressureFr = cp.vl["TPMS11"]["PRESSURE_FR"] * 5 * 0.145038
      ret.tpmsPressureRl = cp.vl["TPMS11"]["PRESSURE_RL"] * 5 * 0.145038
      ret.tpmsPressureRr = cp.vl["TPMS11"]["PRESSURE_RR"] * 5 * 0.145038
    elif cp.vl["TPMS11"]["UNIT"] == 2.0:
      ret.tpmsPressureFl = cp.vl["TPMS11"]["PRESSURE_FL"] / 10 * 14.5038
      ret.tpmsPressureFr = cp.vl["TPMS11"]["PRESSURE_FR"] / 10 * 14.5038
      ret.tpmsPressureRl = cp.vl["TPMS11"]["PRESSURE_RL"] / 10 * 14.5038
      ret.tpmsPressureRr = cp.vl["TPMS11"]["PRESSURE_RR"] / 10 * 14.5038

    # OPKR
    self.safety_dist = cp.vl["NAVI"]["OPKR_S_Dist"]
    self.safety_sign_check = cp.vl["NAVI"]["OPKR_S_Sign"]
    self.safety_block_remain_dist = cp.vl["NAVI"]["OPKR_SBR_Dist"]
    self.is_highway = cp_scc.vl["SCC11"]["Navi_SCC_Camera_Act"] != 0.
    if self.safety_sign_check in [24., 25., 26.] and not self.is_highway and 29 < ret.cruiseState.speed*CV.MS_TO_KPH < 69:
      self.safety_sign = 30.
      self.safety_sign_last = self.safety_sign
    elif self.safety_sign_check in [0., 1., 2.] and 29 < ret.cruiseState.speed*CV.MS_TO_KPH < 79:
      self.safety_sign = 40.
      self.safety_sign_last = self.safety_sign
    elif self.safety_sign_check in [8., 9., 10.] and 29 < ret.cruiseState.speed*CV.MS_TO_KPH < 89:
      self.safety_sign = 50.
      self.safety_sign_last = self.safety_sign
    elif self.safety_sign_check in [16., 17., 18.] and not self.is_highway and 29 < ret.cruiseState.speed*CV.MS_TO_KPH:
      self.safety_sign = 60.
      self.safety_sign_last = self.safety_sign
    elif self.safety_sign_check in [24., 25., 26.] and not self.is_highway and 29 < ret.cruiseState.speed*CV.MS_TO_KPH:
      self.safety_sign = 70.
      self.safety_sign_last = self.safety_sign
    elif self.safety_sign_check in [0., 1., 2.] and 29 < ret.cruiseState.speed*CV.MS_TO_KPH:
      self.safety_sign = 80.
      self.safety_sign_last = self.safety_sign
    elif self.safety_sign_check in [8., 9., 10.] and 29 < ret.cruiseState.speed*CV.MS_TO_KPH:
      self.safety_sign = 90.
      self.safety_sign_last = self.safety_sign
    elif self.safety_sign_check in [16., 17., 18.] and self.is_highway and 29 < ret.cruiseState.speed*CV.MS_TO_KPH:
      self.safety_sign = 100.
      self.safety_sign_last = self.safety_sign
    elif self.safety_sign_check in [24., 25., 26.] and self.is_highway and 29 < ret.cruiseState.speed*CV.MS_TO_KPH:
      self.safety_sign = 110.
      self.safety_sign_last = self.safety_sign
    elif self.safety_block_remain_dist < 255.:
      self.safety_sign = self.safety_sign_last
    else:
      self.safety_sign = 0.

    cam_distance_calc = interp(ret.vEgo*CV.MS_TO_KPH, [30,110], [2.8,4.0])
    consider_speed = interp((ret.vEgo*CV.MS_TO_KPH - self.safety_sign), [0,50], [1, 2.25])
    final_cam_decel_start_dist = cam_distance_calc*consider_speed*ret.vEgo*CV.MS_TO_KPH * (1 + self.safetycam_decel_dist_gain*0.01)
    if self.safety_sign > 29 and self.safety_dist < final_cam_decel_start_dist:
      ret.safetySign = self.safety_sign
      ret.safetyDist = self.safety_dist
      self.on_speed_control = True
    elif self.safety_sign > 29 and self.safety_block_remain_dist < 255.:
      ret.safetySign = self.safety_sign
      ret.safetyDist = self.safety_dist
      self.on_speed_control = True
    elif self.safety_sign > 29 and self.safety_dist < 600.:
      ret.safetySign = self.safety_sign
      ret.safetyDist = self.safety_dist
      self.on_speed_control = False
    else:
      ret.safetySign = 0
      ret.safetyDist = 0
      self.on_speed_control = False

    self.cruiseGapSet = cp_scc.vl["SCC11"]["TauGapSet"]
    ret.cruiseGapSet = self.cruiseGapSet

    # Gear Selection via Cluster - For those Kia/Hyundai which are not fully discovered, we can use the Cluster Indicator for Gear Selection,
    # as this seems to be standard over all cars, but is not the preferred method.
    if self.CP.carFingerprint in FEATURES["use_cluster_gears"]:
      gear = cp.vl["CLU15"]["CF_Clu_Gear"]
    elif self.CP.carFingerprint in FEATURES["use_tcu_gears"]:
      gear = cp.vl["TCU12"]["CUR_GR"]
    elif self.CP.carFingerprint in FEATURES["use_elect_gears"]:
      gear = cp.vl["ELECT_GEAR"]["Elect_Gear_Shifter"]
      ret.electGearStep = cp.vl["ELECT_GEAR"]["Elect_Gear_Step"] # opkr
    else:
      gear = cp.vl["LVR12"]["CF_Lvr_Gear"]
      ret.electGearStep = 0

    if self.gear_correction:
      ret.gearShifter = GearShifter.drive
    else:
      ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(gear))

    if self.CP.fcaBus != -1 or self.CP.carFingerprint in FEATURES["use_fca"]:
      ret.stockAeb = cp_fca.vl["FCA11"]["FCA_CmdAct"] != 0
      ret.stockFcw = cp_fca.vl["FCA11"]["CF_VSM_Warn"] == 2
    elif not self.CP.radarOffCan:
      ret.stockAeb = cp_scc.vl["SCC12"]["AEB_CmdAct"] != 0
      ret.stockFcw = cp_scc.vl["SCC12"]["CF_VSM_Warn"] == 2

    # Blind Spot Detection and Lane Change Assist signals
    if self.CP.bsmAvailable or self.CP.enableBsm:
      self.lca_state = cp.vl["LCA11"]["CF_Lca_Stat"]
      ret.leftBlindspot = cp.vl["LCA11"]["CF_Lca_IndLeft"] != 0
      ret.rightBlindspot = cp.vl["LCA11"]["CF_Lca_IndRight"] != 0

    # save the entire LKAS11, CLU11, SCC12 and MDPS12
    self.lkas11 = copy.copy(cp_cam.vl["LKAS11"])
    self.clu11 = copy.copy(cp.vl["CLU11"])
    self.scc11 = copy.copy(cp_scc.vl["SCC11"])
    self.scc12 = copy.copy(cp_scc.vl["SCC12"])
    self.scc13 = copy.copy(cp_scc.vl["SCC13"])
    self.scc14 = copy.copy(cp_scc.vl["SCC14"])
    self.mdps12 = copy.copy(cp_mdps.vl["MDPS12"])

    self.scc11init = copy.copy(cp.vl["SCC11"])
    self.scc12init = copy.copy(cp.vl["SCC12"])

    if self.CP.carFingerprint in FEATURES["send_hda_mfa"]:
      self.lfahda = copy.copy(cp_cam.vl["LFAHDA_MFC"])

      


    ret.brakeHold = cp.vl["TCS15"]["AVH_LAMP"] == 2 # 0 OFF, 1 ERROR, 2 ACTIVE, 3 READY
    self.brakeHold = ret.brakeHold
    self.brake_error = cp.vl["TCS13"]["ACCEnable"] == 3 # 0 ACC CONTROL ENABLED, 1-3 ACC CONTROL DISABLED
    self.steer_state = cp_mdps.vl["MDPS12"]["CF_Mdps_ToiActive"] #0 NOT ACTIVE, 1 ACTIVE
    self.lead_distance = cp_scc.vl["SCC11"]["ACC_ObjDist"] if not self.no_radar else 0

    ret.radarDistance = cp_scc.vl["SCC11"]["ACC_ObjDist"] if not self.no_radar else 0
    self.lkas_error = cp_cam.vl["LKAS11"]["CF_Lkas_LdwsSysState"] == 7
    if not self.lkas_error:
      self.lkas_button_on = cp_cam.vl["LKAS11"]["CF_Lkas_LdwsSysState"]
    
    ret.cruiseAccStatus = cp_scc.vl["SCC12"]["ACCMode"] == 1
    ret.driverAcc = self.driverOverride == 1

    return ret

  @staticmethod
  def get_can_parser(CP):
    signals = [
      # sig_name, sig_address, default
      ("WHL_SPD_FL", "WHL_SPD11", 0),
      ("WHL_SPD_FR", "WHL_SPD11", 0),
      ("WHL_SPD_RL", "WHL_SPD11", 0),
      ("WHL_SPD_RR", "WHL_SPD11", 0),

      ("YAW_RATE", "ESP12", 0),

      ("CF_Gway_DrvSeatBeltInd", "CGW4", 1),

      ("CF_Gway_DrvSeatBeltSw", "CGW1", 0),
      ("CF_Gway_DrvDrSw", "CGW1", 0),       # Driver Door
      ("CF_Gway_AstDrSw", "CGW1", 0),       # Passenger door
      ("CF_Gway_RLDrSw", "CGW2", 0),        # Rear reft door
      ("CF_Gway_RRDrSw", "CGW2", 0),        # Rear right door
      ("CF_Gway_TurnSigLh", "CGW1", 0),
      ("CF_Gway_TurnSigRh", "CGW1", 0),
      ("CF_Gway_ParkBrakeSw", "CGW1", 0),

      ("CYL_PRES", "ESP12", 0),

      ("AVH_STAT", "ESP11", 0),

      ("CF_Clu_CruiseSwState", "CLU11", 0),
      ("CF_Clu_CruiseSwMain", "CLU11", 0),
      ("CF_Clu_SldMainSW", "CLU11", 0),
      ("CF_Clu_ParityBit1", "CLU11", 0),
      ("CF_Clu_VanzDecimal" , "CLU11", 0),
      ("CF_Clu_Vanz", "CLU11", 0),
      ("CF_Clu_SPEED_UNIT", "CLU11", 0),
      ("CF_Clu_DetentOut", "CLU11", 0),
      ("CF_Clu_RheostatLevel", "CLU11", 0),
      ("CF_Clu_CluInfo", "CLU11", 0),
      ("CF_Clu_AmpInfo", "CLU11", 0),
      ("CF_Clu_AliveCnt1", "CLU11", 0),

      ("ACCEnable", "TCS13", 0),
      ("BrakeLight", "TCS13", 0),
      ("DriverBraking", "TCS13", 0),
      ("DriverOverride", "TCS13", 0),
      ("PBRAKE_ACT", "TCS13", 0),
      ("CF_VSM_Avail", "TCS13", 0),

      ("ESC_Off_Step", "TCS15", 0),
      ("AVH_LAMP", "TCS15", 0),

      ("CF_Lvr_CruiseSet", "LVR12", 0),
      ("CRUISE_LAMP_M", "EMS16", 0),

      ("MainMode_ACC", "SCC11", 1),
      ("SCCInfoDisplay", "SCC11", 0),
      ("AliveCounterACC", "SCC11", 0),
      ("VSetDis", "SCC11", 30),
      ("ObjValid", "SCC11", 0),
      ("DriverAlertDisplay", "SCC11", 0),
      ("TauGapSet", "SCC11", 4),
      ("ACC_ObjStatus", "SCC11", 0),
      ("ACC_ObjLatPos", "SCC11", 0),
      ("ACC_ObjDist", "SCC11", 150), #TK211X value is 204.6
      ("ACC_ObjRelSpd", "SCC11", 0),
      ("Navi_SCC_Curve_Status", "SCC11", 0),
      ("Navi_SCC_Curve_Act", "SCC11", 0),
      ("Navi_SCC_Camera_Act", "SCC11", 0),
      ("Navi_SCC_Camera_Status", "SCC11", 2),

      ("ACCMode", "SCC12", 0),
      ("CF_VSM_Prefill", "SCC12", 0),
      ("CF_VSM_DecCmdAct", "SCC12", 0),
      ("CF_VSM_HBACmd", "SCC12", 0),
      ("CF_VSM_Warn", "SCC12", 0),
      ("CF_VSM_Stat", "SCC12", 0),
      ("CF_VSM_BeltCmd", "SCC12", 0),
      ("ACCFailInfo", "SCC12", 0),
      ("StopReq", "SCC12", 0),
      ("CR_VSM_DecCmd", "SCC12", 0),
      ("aReqRaw", "SCC12", 0), #aReqMax
      ("TakeOverReq", "SCC12", 0),
      ("PreFill", "SCC12", 0),
      ("aReqValue", "SCC12", 0), #aReqMin
      ("CF_VSM_ConfMode", "SCC12", 1),
      ("AEB_Failinfo", "SCC12", 0),
      ("AEB_Status", "SCC12", 2),
      ("AEB_CmdAct", "SCC12", 0),
      ("AEB_StopReq", "SCC12", 0),
      ("CR_VSM_Alive", "SCC12", 0),
      ("CR_VSM_ChkSum", "SCC12", 0),

      ("SCCDrvModeRValue", "SCC13", 2),
      ("SCC_Equip", "SCC13", 1),
      ("AebDrvSetStatus", "SCC13", 0),

      ("JerkUpperLimit", "SCC14", 0),
      ("JerkLowerLimit", "SCC14", 0),
      ("SCCMode2", "SCC14", 0),
      ("ComfortBandUpper", "SCC14", 0),
      ("ComfortBandLower", "SCC14", 0),

      ("UNIT", "TPMS11", 0),
      ("PRESSURE_FL", "TPMS11", 0),
      ("PRESSURE_FR", "TPMS11", 0),
      ("PRESSURE_RL", "TPMS11", 0),
      ("PRESSURE_RR", "TPMS11", 0),

      ("OPKR_S_Dist", "NAVI", 0),
      ("OPKR_S_Sign", "NAVI", 31),
      ("OPKR_SBR_Dist", "NAVI", 0),
    ]

    checks = [
      # address, frequency
      ("TCS13", 50),
      ("TCS15", 10),
      ("CLU11", 50),
      ("ESP12", 100),
      ("CGW1", 10),
      ("CGW2", 5),
      ("CGW4", 5),
      ("WHL_SPD11", 50),
    ]
    if CP.sccBus == 0 and CP.pcmCruise:
      checks += [
        ("SCC11", 50),
        ("SCC12", 50),
      ]
    if CP.fcaBus == 0:
      signals += [
        ("FCA_CmdAct", "FCA11", 0),
        ("CF_VSM_Warn", "FCA11", 0),
      ]
      checks += [("FCA11", 50)]

    if CP.mdpsBus == 0:
      signals += [
        ("CR_Mdps_StrColTq", "MDPS12", 0),
        ("CF_Mdps_Def", "MDPS12", 0),
        ("CF_Mdps_ToiActive", "MDPS12", 0),
        ("CF_Mdps_ToiUnavail", "MDPS12", 0),
        ("CF_Mdps_ToiFlt", "MDPS12", 0),
        ("CF_Mdps_MsgCount2", "MDPS12", 0),
        ("CF_Mdps_Chksum2", "MDPS12", 0),
        ("CF_Mdps_SErr", "MDPS12", 0),
        ("CR_Mdps_StrTq", "MDPS12", 0),
        ("CF_Mdps_FailStat", "MDPS12", 0),
        ("CR_Mdps_OutTq", "MDPS12", 0)
      ]
      checks += [
        ("MDPS12", 50)
      ]
    if CP.sasBus == 0:
      signals += [
        ("SAS_Angle", "SAS11", 0),
        ("SAS_Speed", "SAS11", 0),
      ]
      checks += [
        ("SAS11", 100)
      ]

    if CP.bsmAvailable or CP.enableBsm:
      signals += [
      	("CF_Lca_Stat", "LCA11", 0),
        ("CF_Lca_IndLeft", "LCA11", 0),
        ("CF_Lca_IndRight", "LCA11", 0),
      ]
      checks += [("LCA11", 50)]

    if CP.carFingerprint in (HYBRID_CAR | EV_CAR):
      if CP.carFingerprint in HYBRID_CAR:
        signals += [
          ("CR_Vcu_AccPedDep_Pos", "E_EMS11", 0)
        ]
      else:
        signals += [
          ("Accel_Pedal_Pos", "E_EMS11", 0)
        ]
      checks += [
        ("E_EMS11", 50),
      ]
    else:
      signals += [
        ("PV_AV_CAN", "EMS12", 0),
        ("CF_Ems_AclAct", "EMS16", 0),
      ]
      checks += [
        ("EMS12", 100),
        ("EMS16", 100),
      ]

    if CP.carFingerprint in FEATURES["use_cluster_gears"]:
      signals += [
        ("CF_Clu_Gear", "CLU15", 0),
      ]
      checks += [
        ("CLU15", 5)
      ]
    elif CP.carFingerprint in FEATURES["use_tcu_gears"]:
      signals += [
        ("CUR_GR", "TCU12", 0)
      ]
      checks += [
        ("TCU12", 100)
      ]
    elif CP.carFingerprint in FEATURES["use_elect_gears"]:
      signals += [
        ("Elect_Gear_Shifter", "ELECT_GEAR", 0),
        ("Elect_Gear_Step", "ELECT_GEAR", 0)
      ]
      checks += [("ELECT_GEAR", 20)]
    else:
      signals += [
        ("CF_Lvr_Gear", "LVR12", 0)
      ]
      checks += [
        ("LVR12", 100)
      ]

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 0, enforce_checks=False)

  @staticmethod
  def get_can2_parser(CP):
    signals = []
    checks = []
    if CP.mdpsBus == 1:
      signals += [
        ("CR_Mdps_StrColTq", "MDPS12", 0),
        ("CF_Mdps_Def", "MDPS12", 0),
        ("CF_Mdps_ToiActive", "MDPS12", 0),
        ("CF_Mdps_ToiUnavail", "MDPS12", 0),
        ("CF_Mdps_ToiFlt", "MDPS12", 0),
        ("CF_Mdps_MsgCount2", "MDPS12", 0),
        ("CF_Mdps_Chksum2", "MDPS12", 0),
        ("CF_Mdps_SErr", "MDPS12", 0),
        ("CR_Mdps_StrTq", "MDPS12", 0),
        ("CF_Mdps_FailStat", "MDPS12", 0),
        ("CR_Mdps_OutTq", "MDPS12", 0)
      ]
      checks += [
        ("MDPS12", 50)
      ]
    if CP.sasBus == 1:
      signals += [
        ("SAS_Angle", "SAS11", 0),
        ("SAS_Speed", "SAS11", 0),
      ]
      checks += [
        ("SAS11", 100)
      ]
    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 1, enforce_checks=False)

  @staticmethod
  def get_cam_can_parser(CP):

    signals = [
      # sig_name, sig_address, default
      ("CF_Lkas_LdwsActivemode", "LKAS11", 0),
      ("CF_Lkas_LdwsSysState", "LKAS11", 0),
      ("CF_Lkas_SysWarning", "LKAS11", 0),
      ("CF_Lkas_LdwsLHWarning", "LKAS11", 0),
      ("CF_Lkas_LdwsRHWarning", "LKAS11", 0),
      ("CF_Lkas_HbaLamp", "LKAS11", 0),
      ("CF_Lkas_FcwBasReq", "LKAS11", 0),
      ("CF_Lkas_ToiFlt", "LKAS11", 0),
      ("CF_Lkas_HbaSysState", "LKAS11", 0),
      ("CF_Lkas_FcwOpt", "LKAS11", 0),
      ("CF_Lkas_HbaOpt", "LKAS11", 0),
      ("CF_Lkas_FcwSysState", "LKAS11", 0),
      ("CF_Lkas_FcwCollisionWarning", "LKAS11", 0),
      ("CF_Lkas_MsgCount", "LKAS11", 0),
      ("CF_Lkas_FusionState", "LKAS11", 0),
      ("CF_Lkas_FcwOpt_USM", "LKAS11", 0),
      ("CF_Lkas_LdwsOpt_USM", "LKAS11", 0),
    ]

    checks = [
      ("LKAS11", 100)
    ]
    if CP.sccBus == 2:
      signals += [
        ("MainMode_ACC", "SCC11", 1),
        ("SCCInfoDisplay", "SCC11", 0),
        ("AliveCounterACC", "SCC11", 0),
        ("VSetDis", "SCC11", 30),
        ("ObjValid", "SCC11", 0),
        ("DriverAlertDisplay", "SCC11", 0),
        ("TauGapSet", "SCC11", 4),
        ("ACC_ObjStatus", "SCC11", 0),
        ("ACC_ObjLatPos", "SCC11", 0),
        ("ACC_ObjDist", "SCC11", 150.),
        ("ACC_ObjRelSpd", "SCC11", 0),
        ("Navi_SCC_Curve_Status", "SCC11", 0),
        ("Navi_SCC_Curve_Act", "SCC11", 0),
        ("Navi_SCC_Camera_Act", "SCC11", 0),
        ("Navi_SCC_Camera_Status", "SCC11", 2),

        ("ACCMode", "SCC12", 0),
        ("CF_VSM_Prefill", "SCC12", 0),
        ("CF_VSM_DecCmdAct", "SCC12", 0),
        ("CF_VSM_HBACmd", "SCC12", 0),
        ("CF_VSM_Warn", "SCC12", 0),
        ("CF_VSM_Stat", "SCC12", 0),
        ("CF_VSM_BeltCmd", "SCC12", 0),
        ("ACCFailInfo", "SCC12", 0),
        ("StopReq", "SCC12", 0),
        ("CR_VSM_DecCmd", "SCC12", 0),
        ("aReqRaw", "SCC12", 0),
        ("TakeOverReq", "SCC12", 0),
        ("PreFill", "SCC12", 0),
        ("aReqValue", "SCC12", 0),
        ("CF_VSM_ConfMode", "SCC12", 1),
        ("AEB_Failinfo", "SCC12", 0),
        ("AEB_Status", "SCC12", 2),
        ("AEB_CmdAct", "SCC12", 0),
        ("AEB_StopReq", "SCC12", 0),
        ("CR_VSM_Alive", "SCC12", 0),
        ("CR_VSM_ChkSum", "SCC12", 0),

        ("SCCDrvModeRValue", "SCC13", 2),
        ("SCC_Equip", "SCC13", 1),
        ("AebDrvSetStatus", "SCC13", 0),

        ("JerkUpperLimit", "SCC14", 0),
        ("JerkLowerLimit", "SCC14", 0),
        ("SCCMode2", "SCC14", 0),
        ("ComfortBandUpper", "SCC14", 0),
        ("ComfortBandLower", "SCC14", 0),
        ("ACCMode", "SCC14", 0),
        ("ObjGap", "SCC14", 0),
      ]
      checks += [
        ("SCC11", 50),
        ("SCC12", 50),
      ]
      if CP.fcaBus == 2:
        signals += [
          ("CF_VSM_Prefill", "FCA11", 0),
          ("CF_VSM_HBACmd", "FCA11", 0),
          ("CF_VSM_Warn", "FCA11", 0),
          ("CF_VSM_BeltCmd", "FCA11", 0),
          ("CR_VSM_DecCmd", "FCA11", 0),
          ("FCA_Status", "FCA11", 2),
          ("FCA_CmdAct", "FCA11", 0),
          ("FCA_StopReq", "FCA11", 0),
          ("FCA_DrvSetStatus", "FCA11", 1),
          ("CF_VSM_DecCmdAct", "FCA11", 0),
          ("FCA_Failinfo", "FCA11", 0),
          ("FCA_RelativeVelocity", "FCA11", 0),
          ("FCA_TimetoCollision", "FCA11", 2540.),
          ("CR_FCA_Alive", "FCA11", 0),
          ("CR_FCA_ChkSum", "FCA11", 0),
          ("Supplemental_Counter", "FCA11", 0),
          ("PAINT1_Status", "FCA11", 1),
        ]
        checks += [("FCA11", 50)]

      if CP.carFingerprint in FEATURES["send_hda_mfa"]:
        signals += [
          ("HDA_USM", "LFAHDA_MFC", 0),
          ("HDA_Active", "LFAHDA_MFC", 0),
          ("HDA_Icon_State", "LFAHDA_MFC", 0),
          ("HDA_LdwSysState", "LFAHDA_MFC", 0),
          ("HDA_Icon_Wheel", "LFAHDA_MFC", 0),
        ]
        checks += [("LFAHDA_MFC", 20)]

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 2, enforce_checks=False)
