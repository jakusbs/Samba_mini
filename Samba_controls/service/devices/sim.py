"""Simulated instruments for the explicit simulation mode.

All readings from these devices carry ``Quality.SIMULATED`` and every data
file written in simulation mode is labelled ``mode = "simulation"``.  The
physics is deliberately simple and *not* a model of the real setup:

* field B = k·V_coil approached with a first-order lag (``coil_tau_s``);
* magnetisation follows a two-branch tanh hysteresis loop;
* the sample is a disc (radius 300 µm at the stage origin) on a substrate,
  which gives the spatial scans something to show.
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np

from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.daq import DaqBase
from Samba_controls.service.devices.hall import HallBase
from Samba_controls.service.devices.lockin import (SENSITIVITIES, TIME_CONSTANTS, LockinBase,
                                                   truncated_discrete)
from Samba_controls.service.devices.stage import AXES, StageBase


class SimPhysics:
    def __init__(self, seed: int | None = None):
        self.field_per_volt_mT = 100.0
        self.coil_tau_s = 0.005
        self.hc_mT = 25.0
        self.width_mT = 6.0
        self.hall_offset_mT = 0.35
        self.rng = np.random.default_rng(seed)
        self._lock = threading.Lock()
        self._coil = 0.0
        self._coil_prev_field = 0.0
        self._coil_t = time.monotonic()
        self._branch = 1            # +1: descending branch (came from +saturation)
        self.osc_V = 0.0
        self.stage_pos_mm = {"x": 0.0, "y": 0.0}
        self.active_stage = "longitudinal"
        self.fail_next: dict[str, int] = {}

    def set_coil(self, volts: float) -> None:
        with self._lock:
            now = time.monotonic()
            self._coil_prev_field = self._field_at(now)
            self._coil = float(volts)
            self._coil_t = now

    def _field_at(self, t: float) -> float:
        target = self._coil * self.field_per_volt_mT
        dt = max(0.0, t - self._coil_t)
        return target + (self._coil_prev_field - target) * math.exp(-dt / self.coil_tau_s)

    def field_mT(self, t: float | None = None) -> float:
        with self._lock:
            return self._field_at(time.monotonic() if t is None else t)

    def magnetisation(self, field_mT: float) -> float:
        sat = self.hc_mT + 3 * self.width_mT
        if field_mT > sat:
            self._branch = 1
        elif field_mT < -sat:
            self._branch = -1
        return math.tanh((field_mT + self._branch * self.hc_mT) / self.width_mT)

    def spatial(self) -> tuple[float, float]:
        """(magnetic contrast, reflectivity) at the current stage position."""
        x_um = self.stage_pos_mm["x"] * 1000.0
        y_um = self.stage_pos_mm["y"] * 1000.0
        r = math.hypot(x_um, y_um)
        inside = 1.0 / (1.0 + math.exp((r - 300.0) / 15.0))
        return inside, 0.8 + 0.2 * inside

    def should_fail(self, key: str) -> bool:
        n = self.fail_next.get(key, 0)
        if n > 0:
            self.fail_next[key] = n - 1
            return True
        return False


class SimDaq(DaqBase):
    def __init__(self, name, config=None, physics: SimPhysics | None = None):
        super().__init__(name, config, simulated=True)
        self.physics = physics or SimPhysics()
        self.identity.update({"model": "Simulated DAQ", "driver": "sim"})
        self._started: dict | None = None

    def _hw_open(self):
        if self.physics.should_fail("daq.connect"):
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, "simulated: DAQ not found")

    def _hw_close(self):
        pass

    def _hw_timing(self, rate, n):
        pass

    def _hw_start(self, buffer):
        if self.physics.should_fail("daq.start"):
            raise ServiceError(ErrorCode.DEVICE_FAULT, "simulated DAQ start failure")
        coil = float(buffer[2, 0])
        self.physics.set_coil(coil)
        self._started = {"t0": time.monotonic(), "n": buffer.shape[1]}

    def _hw_wait_read(self, n, timeout_s):
        st = self._started
        self._started = None
        if st is None:
            raise ServiceError(ErrorCode.DEVICE_FAULT, "simulated DAQ: no task running")
        duration = n / self.rate
        hang = self.physics.should_fail("daq.hang")
        remaining = (st["t0"] + duration) - time.monotonic()
        if hang or remaining > timeout_s:
            time.sleep(min(timeout_s, max(remaining, 0)))
            raise ServiceError(ErrorCode.TIMEOUT, "simulated DAQ wait_until_done timed out")
        if remaining > 0:
            time.sleep(remaining)
        p = self.physics
        # Field at each sample (sub-sampled for speed), magnetisation from the loop.
        t = st["t0"] + np.arange(n) / self.rate
        idx = np.linspace(0, n - 1, min(n, 64)).astype(int)
        fields = np.array([p.field_mT(float(tt)) for tt in t[idx]])
        mags = np.array([p.magnetisation(float(b)) for b in fields])
        mag = np.interp(np.arange(n), idx, mags)
        contrast, refl = p.spatial()
        intensity = refl * 1.2 + p.rng.normal(0, 2e-4, n)
        balanced = 0.004 + 0.02 * contrast * mag * refl + p.rng.normal(0, 3e-4, n)
        return balanced, intensity

    def _hw_static(self, values, timeout_s):
        self.physics.set_coil(float(values[2]))

    def _hw_stop(self):
        self._started = None


class SimHall(HallBase):
    def __init__(self, name, config=None, physics: SimPhysics | None = None):
        super().__init__(name, config, simulated=True)
        self.physics = physics or SimPhysics()
        self.identity.update({"model": "Simulated Hall probe", "driver": "sim"})

    def _connect(self):
        if self.physics.should_fail("hall.connect"):
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, "simulated: Hall probe not found")
        self.set_integration(self.integration_s)

    def hw_set_filter(self, points):
        pass

    def hw_read_tesla(self):
        if self.physics.should_fail("hall.read"):
            raise ServiceError(ErrorCode.DEVICE_FAULT, "simulated Hall read failure")
        b = self.physics.field_mT() + self.physics.hall_offset_mT + self.physics.rng.normal(0, 0.02)
        return b / 1000.0

    def hw_zero(self, timeout_s, ctx):
        self.sleep(0.2, ctx)
        self.physics.hall_offset_mT = -self.physics.field_mT()


class SimLockin(LockinBase):
    def __init__(self, name, config=None, physics: SimPhysics | None = None):
        super().__init__(name, config, simulated=True)
        self.physics = physics or SimPhysics()
        self.identity.update({"model": "Simulated 7270", "driver": "sim"})
        self.settings = {"oscillator_V": 0.0, "sensitivity_V": 1e-3, "frequency_Hz": 1777.0,
                         "time_constant_s": 0.1, "phase_deg": 0.0, "reference_mode": 0}

    def _connect(self):
        if self.physics.should_fail("lockin.connect"):
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, "simulated: lock-in not reachable")

    def hw_read_xy(self):
        p = self.physics
        if p.should_fail("lockin.read"):
            raise ServiceError(ErrorCode.DEVICE_FAULT, "simulated lock-in read failure")
        time.sleep(0.002)
        contrast, refl = p.spatial()
        m = p.magnetisation(p.field_mT())
        amp = 2e-4 * self.settings["oscillator_V"] * refl
        ph = math.radians(self.settings["phase_deg"])
        signal = amp * (0.3 + contrast * m)
        x = signal * math.cos(ph) + p.rng.normal(0, 2e-7)
        y = -signal * math.sin(ph) + p.rng.normal(0, 2e-7)
        return x, y

    def hw_set(self, what, value):
        if what == "sensitivity_V":
            value = truncated_discrete(value, SENSITIVITIES)
        elif what == "time_constant_s":
            value = truncated_discrete(value, TIME_CONSTANTS)
        elif what == "phase_deg":
            value = value % 360.0
        elif what == "oscillator_V":
            value = min(max(value, 0.0), 5.0)
            self.physics.osc_V = value
        self.settings[what] = float(value)
        return float(value)

    def hw_get(self, what):
        if what == "identification":
            return "7270 (simulated)/sim"
        return self.settings[what]

    def hw_reference_mode(self, mode):
        self.settings["reference_mode"] = mode

    def hw_reconnect(self):
        pass


class SimStage(StageBase):
    """Simulated 3-axis stage moving at ``speed_mm_s``."""

    def __init__(self, name, config=None, physics: SimPhysics | None = None, offsets=None):
        super().__init__(name, config, simulated=True, offsets=offsets)
        self.physics = physics or SimPhysics()
        self.speed_mm_s = float(self.config.get("sim_speed_mm_s", 5.0))
        self.mm2steps = float(self.config.get("mm2steps", 10000.0))
        self.identity.update({"model": "Simulated stage", "driver": "sim"})
        self._raw = {a: 0.0 for a in AXES}
        self._motion: dict[str, tuple[float, float, float, float]] = {}

    def _connect(self):
        if self.physics.should_fail(f"{self.name}.connect"):
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, "simulated: stage not found")
        self.load_offsets()

    def _update(self, axis):
        m = self._motion.get(axis)
        if m is None:
            return
        start, target, t0, dur = m
        frac = 1.0 if dur <= 0 else min(1.0, (time.monotonic() - t0) / dur)
        self._raw[axis] = start + (target - start) * frac
        if frac >= 1.0:
            del self._motion[axis]
        if axis in ("x", "y"):
            self.physics.stage_pos_mm[axis] = self.from_raw(axis, self._raw[axis])

    def to_raw(self, axis, mm):
        return mm * self.mm2steps + self.offsets[axis]

    def from_raw(self, axis, raw):
        return (raw - self.offsets[axis]) / self.mm2steps

    def delta_raw(self, axis, mm):
        return mm * self.mm2steps

    def hw_get_raw(self, axis):
        self._update(axis)
        return self._raw[axis]

    def hw_move_raw(self, axis, raw_target):
        if self.physics.should_fail(f"{self.name}.move"):
            raise ServiceError(ErrorCode.DEVICE_FAULT, "simulated stage move failure")
        self._update(axis)
        start = self._raw[axis]
        dist_mm = abs(raw_target - start) / self.mm2steps
        dur = dist_mm / self.speed_mm_s
        if self.physics.should_fail(f"{self.name}.stuck"):
            dur = 1e9
        self._motion[axis] = (start, float(raw_target), time.monotonic(), dur)

    def hw_move_rel_raw(self, axis, raw_delta):
        self._update(axis)
        self.hw_move_raw(axis, self._raw[axis] + raw_delta)

    def hw_is_moving(self, axis):
        self._update(axis)
        return axis in self._motion

    def hw_stop(self, axis):
        self._update(axis)
        self._motion.pop(axis, None)
