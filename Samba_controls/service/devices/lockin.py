"""Lock-in amplifier -- shared attribute/command definitions.

The rig uses one Ametek / Signal Recovery 7270.  Its oscillator drives the
sample current (through an external current source) and it demodulates at the
same reference; MiniMoke uses single reference mode (REFMODE 0) and one ``XY.``
transaction per point.  One session is used for both configuration and
measurement.
"""

from __future__ import annotations

import math
import time
from typing import Any

from core.model import ArgInfo, AttributeInfo, CommandInfo
from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.base import CallContext, Device

# From MiniMoke's Ametek7270 driver (derived from PyMeasure, MIT licence).
SENSITIVITIES = [
    0.0, 2.0e-9, 5.0e-9, 10.0e-9, 20.0e-9, 50.0e-9, 100.0e-9,
    200.0e-9, 500.0e-9, 1.0e-6, 2.0e-6, 5.0e-6, 10.0e-6,
    20.0e-6, 50.0e-6, 100.0e-6, 200.0e-6, 500.0e-6, 1.0e-3,
    2.0e-3, 5.0e-3, 10.0e-3, 20.0e-3, 50.0e-3, 100.0e-3,
    200.0e-3, 500.0e-3, 1.0,
]
TIME_CONSTANTS = [
    10.0e-6, 20.0e-6, 50.0e-6, 100.0e-6, 200.0e-6, 500.0e-6,
    1.0e-3, 2.0e-3, 5.0e-3, 10.0e-3, 20.0e-3, 50.0e-3, 100.0e-3,
    200.0e-3, 500.0e-3, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0,
    100.0, 200.0, 500.0, 1.0e3, 2.0e3, 5.0e3, 10.0e3,
    20.0e3, 50.0e3, 100.0e3,
]


def truncated_discrete(value: float, values: list[float]) -> float:
    """PyMeasure ``truncated_discrete_set``: the smallest allowed value >= *value*
    (the largest one if *value* is above all)."""
    for v in sorted(values):
        if value <= v:
            return v
    return max(values)


class LockinBase(Device):
    KIND = "lockin"
    MONITORED = ["xy"]
    MONITOR_INTERVAL_S = 1.0
    READ_TIMEOUT_S = 60.0           # covers MiniMoke's reconnect ladder

    def __init__(self, name: str, config: dict | None = None, simulated: bool = False):
        config = dict(config or {})
        self.max_osc = float(config.get("max_oscillator_V", 5.0))
        self.ATTRIBUTES = [
            AttributeInfo("xy", "dict", "V", "r", description="one XY. snapshot: x, y, r (V), theta (deg)"),
            AttributeInfo("x_V", "float", "V", "r", label="X"),
            AttributeInfo("y_V", "float", "V", "r", label="Y"),
            AttributeInfo("r_V", "float", "V", "r", label="R"),
            AttributeInfo("theta_deg", "float", "deg", "r", label="θ"),
            AttributeInfo("oscillator_V", "float", "V", "rw", 0.0, self.max_osc,
                          "Oscillator amplitude (OA.); drives the sample current source",
                          "Oscillator amplitude"),
            AttributeInfo("sensitivity_V", "float", "V", "rw", 2e-9, 1.0,
                          "Sensitivity (SEN, voltage mode); rounded up to the next range",
                          "Sensitivity"),
            AttributeInfo("frequency_Hz", "float", "Hz", "rw", 0.0, 2.5e5,
                          "Oscillator frequency (OF.)", "Frequency"),
            AttributeInfo("time_constant_s", "float", "s", "rw", 10e-6, 100e3,
                          "Time constant (TC); rounded up to the next value", "Time constant"),
            AttributeInfo("phase_deg", "float", "deg", "rw", -360.0, 360.0,
                          "Reference phase (REFP.)", "Phase"),
            AttributeInfo("reference_mode", "int", "", "r", description="REFMODE (0 = single)"),
            AttributeInfo("identification", "str", "", "r", description="ID/VER"),
        ]
        self.COMMANDS = [
            CommandInfo("configure", [
                ArgInfo("volt", "float", "V", 0.0, self.max_osc),
                ArgInfo("sensi", "float", "V", 1e-9, 1.0),
                ArgInfo("freq", "float", "Hz", 1.0, 2.5e5),
                ArgInfo("time_const", "float", "s", 10e-6, 100.0),
                ArgInfo("phase", "float", "deg", -360.0, 360.0),
            ], description="REFMODE 0, then OA., SEN, OF., TC, REFP. (MiniMoke order)"),
            CommandInfo("output_off", [], description="Oscillator amplitude to 0 V (OA. 0)"),
            CommandInfo("read_xyrt", [], mutating=False, timeout_s=60.0,
                        description="One XY. transaction; R and θ derived"),
            CommandInfo("reconnect", [], description="Re-open the session (device clear, framing probe)"),
        ]
        super().__init__(name, config, simulated)

    # --------------------------------------------------------- primitives
    def hw_read_xy(self) -> tuple[float, float]: ...
    def hw_set(self, what: str, value: float) -> float: ...
    def hw_get(self, what: str) -> Any: ...
    def hw_reference_mode(self, mode: int) -> None: ...
    def hw_reconnect(self) -> None: ...

    # --------------------------------------------------------- API
    def read_xy_rt(self) -> dict:
        x, y = self.hw_read_xy()
        t = time.time()
        r = math.hypot(x, y)
        theta = math.degrees(math.atan2(y, x))
        snap = {"x_V": x, "y_V": y, "r_V": r, "theta_deg": theta, "t": t}
        for k in ("x_V", "y_V", "r_V", "theta_deg"):
            self.store(k, self.make_reading(k, snap[k], "device", t))
        return snap

    def configure(self, volt, sensi, freq, time_const, phase) -> dict:
        if not (0 <= volt <= self.max_osc):
            raise ServiceError(ErrorCode.OUT_OF_RANGE, f"oscillator amplitude {volt} V outside [0, {self.max_osc}]")
        self.hw_reference_mode(0)
        applied = {
            "oscillator_V": self.hw_set("oscillator_V", volt),
            "sensitivity_V": self.hw_set("sensitivity_V", sensi),
            "frequency_Hz": self.hw_set("frequency_Hz", freq),
            "time_constant_s": self.hw_set("time_constant_s", time_const),
            "phase_deg": self.hw_set("phase_deg", phase),
            "reference_mode": 0,
        }
        for k, v in applied.items():
            self.store(k, self.make_reading(k, v, "write"))
        return applied

    def output_off(self) -> None:
        self.hw_set("oscillator_V", 0.0)
        self.store("oscillator_V", self.make_reading("oscillator_V", 0.0, "write"))

    def cleanup(self, ctx: CallContext) -> list[str]:
        if not self.usable:
            return [f"{self.name}: not connected; oscillator output state unknown"]
        try:
            self.output_off()
            return []
        except Exception as exc:  # noqa: BLE001
            self.set_fault(exc, "cleanup")
            return [f"{self.name}: could not set oscillator amplitude to 0: {exc}"]

    # --------------------------------------------------------- attributes
    def _read_attribute(self, name: str) -> Any:
        if name == "xy":
            snap = self.read_xy_rt()
            return {k: snap[k] for k in ("x_V", "y_V", "r_V", "theta_deg")}
        if name in ("x_V", "y_V", "r_V", "theta_deg"):
            return self.read_xy_rt()[name]
        return self.hw_get(name)

    def _write_attribute(self, name: str, value: Any) -> Any:
        if name in ("oscillator_V", "sensitivity_V", "frequency_Hz", "time_constant_s", "phase_deg"):
            return self.hw_set(name, float(value))
        return super()._write_attribute(name, value)

    # --------------------------------------------------------- commands
    def cmd_configure(self, ctx, volt, sensi, freq, time_const, phase):
        return self.configure(volt, sensi, freq, time_const, phase)

    def cmd_output_off(self, ctx):
        self.output_off()
        return {"oscillator_V": 0.0}

    def cmd_read_xyrt(self, ctx):
        return self.read_xy_rt()

    def cmd_reconnect(self, ctx):
        self.hw_reconnect()
        return {"state": self.state.value}
