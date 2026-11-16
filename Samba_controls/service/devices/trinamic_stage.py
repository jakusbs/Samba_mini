"""Polar stage: Trinamic TMCM-3212 three-axis controller through pytrinamic.

From MiniMoke ``polar_stage_class.py`` (all values taken from there):

* axes X = 2 (PP110-30), Y = 1 (GD401 lift), Z = 0 (PP110-20)
* run currents X/Y/Z = 60/102/60, standby 25, max velocity 51200, max
  acceleration 25600, 256 microsteps (AP140 = 8), StealthChop on (AP167 = 1),
  blank time 3 (AP162), PWM amplitude 128 (AP165), gradient 4 (AP166),
  PWM frequency 1 (AP191)
* position = AP1 (signed 32 bit), target reached = AP8 (1 = reached)
* moves with MVP (opcode 4; type 0 absolute, 1 relative)
* Y and Z are sign-inverted: raw = -mm*k + offset; X: raw = mm*k + offset

``hw_stop`` uses TMCL MST through ``pytrinamic``'s ``TmclInterface.stop``;
MiniMoke never stops a move, so this call is listed for hardware verification.
"""

from __future__ import annotations

import logging

from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.stage import AXES, StageBase

log = logging.getLogger(__name__)

AXIS_INDEX = {"x": 2, "y": 1, "z": 0}
SIGN = {"x": 1, "y": -1, "z": -1}
RUN_CURRENT = {"x": 60, "y": 102, "z": 60}
STANDBY_CURRENT = 25
PWM_AMPLITUDE = 128
PWM_GRADIENT = 4
BLANK_TIME = 3
PWM_FREQUENCY = 1
MICROSTEPS = 8
MAX_VELOCITY = 51200
MAX_ACCEL = 25600

AP_ACTUAL_POSITION = 1
AP_MAX_VELOCITY = 4
AP_MAX_ACCELERATION = 5
AP_MAX_CURRENT = 6
AP_STANDBY_CURRENT = 7
AP_POSITION_REACHED = 8
AP_MICROSTEP_RES = 140
AP_BLANK_TIME = 162
AP_PWM_AMPL = 165
AP_PWM_GRAD = 166
AP_STEALTH_CHOP = 167
AP_PWM_FREQ = 191
CMD_SAP, CMD_GAP, CMD_MVP = 5, 6, 4
MVP_ABS, MVP_REL = 0, 1


def to_signed32(value: int) -> int:
    value = int(value) & 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


class TrinamicStage(StageBase):
    def __init__(self, name, config=None, simulated=False, offsets=None):
        super().__init__(name, config, simulated=False, offsets=offsets)
        c = self.config
        self.port = str(c.get("port", "auto")).strip()
        self.module_id = int(c.get("module_id", 1))
        self.mm2steps = {a: float(c.get(f"mm2steps_{a}", 51200)) for a in AXES}
        self.identity.update({"model": "Trinamic TMCM-3212", "resource": self.port, "driver": "pytrinamic"})
        self._iface = None

    def _connect(self) -> None:
        try:
            from pytrinamic.connections import ConnectionManager
        except ImportError as exc:
            raise ServiceError(ErrorCode.NOT_SUPPORTED, f"pytrinamic not installed: {exc}") from exc
        args = "--interface usb_tmcl" + ("" if self.port.lower() == "auto" else f" --port {self.port}")
        try:
            self._iface = ConnectionManager(args).connect()
        except Exception as exc:
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, f"TMCM-3212 not found: {exc}") from exc
        try:
            for a in AXES:
                ax = AXIS_INDEX[a]
                for ap, val in ((AP_MAX_CURRENT, RUN_CURRENT[a]), (AP_STANDBY_CURRENT, STANDBY_CURRENT),
                                (AP_MAX_VELOCITY, MAX_VELOCITY), (AP_MAX_ACCELERATION, MAX_ACCEL),
                                (AP_MICROSTEP_RES, MICROSTEPS), (AP_STEALTH_CHOP, 1),
                                (AP_BLANK_TIME, BLANK_TIME), (AP_PWM_AMPL, PWM_AMPLITUDE),
                                (AP_PWM_GRAD, PWM_GRADIENT), (AP_PWM_FREQ, PWM_FREQUENCY)):
                    self._iface.send(CMD_SAP, ap, ax, val, self.module_id)
        except Exception:
            self._disconnect()
            raise
        self.load_offsets()

    def _disconnect(self) -> None:
        if self._iface is not None:
            try:
                self._iface.close()
            except Exception:  # noqa: BLE001
                pass
        self._iface = None

    def _gap(self, axis: str, ap: int) -> int:
        return self._iface.send(CMD_GAP, ap, AXIS_INDEX[axis], 0, self.module_id).value

    def to_raw(self, axis, mm):
        return int(round(SIGN[axis] * mm * self.mm2steps[axis] + self.offsets[axis]))

    def from_raw(self, axis, raw):
        return SIGN[axis] * (raw - self.offsets[axis]) / self.mm2steps[axis]

    def delta_raw(self, axis, mm):
        return int(round(SIGN[axis] * mm * self.mm2steps[axis]))

    def hw_get_raw(self, axis):
        return float(to_signed32(self._gap(axis, AP_ACTUAL_POSITION)))

    def hw_move_raw(self, axis, raw_target):
        self._iface.send(CMD_MVP, MVP_ABS, AXIS_INDEX[axis], int(raw_target), self.module_id)

    def hw_move_rel_raw(self, axis, raw_delta):
        self._iface.send(CMD_MVP, MVP_REL, AXIS_INDEX[axis], int(raw_delta), self.module_id)

    def hw_is_moving(self, axis):
        return self._gap(axis, AP_POSITION_REACHED) == 0

    def hw_stop(self, axis):
        self._iface.stop(AXIS_INDEX[axis], self.module_id)

    def cmd_set_zero(self, ctx, axis="all"):
        result = super().cmd_set_zero(ctx, axis)
        self.offsets = {a: float(to_signed32(int(v))) for a, v in self.offsets.items()}
        return result
