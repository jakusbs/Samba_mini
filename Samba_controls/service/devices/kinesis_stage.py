"""Longitudinal stage: three Thorlabs Kinesis motors through pylablib.

From MiniMoke ``longitudinal_stage_class.py``: ``Thorlabs.KinesisMotor(serial)``
per axis, ``mm2steps`` steps per mm (2184533 on the rig), controller backlash
``backlash_mm`` programmed with ``setup_gen_move(backlash_distance=..)``,
user position = (raw - offset) / mm2steps for all three axes.  The Thorlabs
Kinesis GUI must be closed (it holds the devices).
"""

from __future__ import annotations

import logging

from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.stage import AXES, StageBase

log = logging.getLogger(__name__)


class KinesisStage(StageBase):
    def __init__(self, name, config=None, simulated=False, offsets=None):
        super().__init__(name, config, simulated=False, offsets=offsets)
        c = self.config
        self.serials = {a: str(c.get(f"serial_{a}", "")) for a in AXES}
        self.mm2steps = float(c.get("mm2steps", 2184533))
        self.backlash_mm = float(c.get("backlash_mm", 0.02))
        self.identity.update({"model": "Thorlabs Kinesis (pylablib)",
                              "serial": ",".join(self.serials.values()), "driver": "pylablib"})
        self._motors: dict = {}

    def _connect(self) -> None:
        if not all(self.serials.values()):
            raise ServiceError(ErrorCode.NOT_SUPPORTED, "Kinesis serial numbers not configured")
        try:
            from pylablib.devices import Thorlabs
        except ImportError as exc:
            raise ServiceError(ErrorCode.NOT_SUPPORTED, f"pylablib not installed: {exc}") from exc
        motors = {}
        try:
            for a in AXES:
                motors[a] = Thorlabs.KinesisMotor(self.serials[a])
        except Exception as exc:
            for m in motors.values():
                try:
                    m.close()
                except Exception:  # noqa: BLE001
                    pass
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE,
                               f"Kinesis motor not found ({exc}); is the Kinesis GUI closed?") from exc
        self._motors = motors
        steps = int(round(self.backlash_mm * self.mm2steps))
        for a, m in motors.items():
            try:
                m.setup_gen_move(backlash_distance=steps, scale=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not set backlash on %s: %s", a, exc)
        self.load_offsets()

    def _disconnect(self) -> None:
        for m in self._motors.values():
            try:
                m.close()
            except Exception:  # noqa: BLE001
                pass
        self._motors = {}

    def to_raw(self, axis, mm):
        return mm * self.mm2steps + self.offsets[axis]

    def from_raw(self, axis, raw):
        return (raw - self.offsets[axis]) / self.mm2steps

    def delta_raw(self, axis, mm):
        return mm * self.mm2steps

    def hw_get_raw(self, axis):
        return float(self._motors[axis].get_position(scale=False))

    def hw_move_raw(self, axis, raw_target):
        self._motors[axis].move_to(raw_target, scale=False)

    def hw_move_rel_raw(self, axis, raw_delta):
        self._motors[axis].move_by(raw_delta, scale=False)

    def hw_is_moving(self, axis):
        return bool(self._motors[axis].is_moving())

    def hw_stop(self, axis):
        self._motors[axis].stop(immediate=False, sync=True, timeout=10.0)
