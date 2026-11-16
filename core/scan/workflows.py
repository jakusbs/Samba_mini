"""Workflow definitions shared by the measurement UI and the engine.

Parameters, defaults and limits follow MiniMoke HEAD (``src/procedures``).
Units are the physical ones: MiniMoke's field parameters labelled "A" are the
coil command in volts on the DAQ output (see docs/implementation-plan.md).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Param:
    name: str
    label: str
    unit: str = ""
    dtype: str = "float"          # float, int, bool, choice
    default: Any = 0.0
    min: float | None = None
    max: float | None = None
    decimals: int = 4
    step: float | None = None
    help: str = ""
    group: str = "Scan"
    choices: list | None = None


def _lockin_params(volt=1.0, sensi=500e-6, freq=1777.0, tc=0.5, phase=0.0, acq=1.0,
                   freq_min=173.0, tc_min=0.1, tc_max=10.0, sensi_min=1e-6) -> list[Param]:
    return [
        Param("volt", "Lock-in oscillator amplitude", "V", default=volt, min=0.0, max=5.0, decimals=3,
              group="Lock-in", help="OA. — drives the sample current through the external source"),
        Param("sensi", "Lock-in sensitivity", "V", default=sensi, min=sensi_min, max=1.0, decimals=9,
              group="Lock-in", help="rounded up to the next 7270 range"),
        Param("lockin_freq", "Lock-in frequency", "Hz", default=freq, min=freq_min, max=102000.0, decimals=1,
              group="Lock-in"),
        Param("time_const", "Lock-in time constant", "s", default=tc, min=tc_min, max=tc_max, decimals=5,
              group="Lock-in", help="rounded up to the next 7270 value"),
        Param("phase", "Lock-in phase", "deg", default=phase, min=-180.0, max=180.0, decimals=2, group="Lock-in"),
        Param("acq_time", "Acquisition time", "s", default=acq, min=1e-5, max=600.0, decimals=5,
              group="Acquisition", help="DAQ window per point (50 kHz); Hall integration = min(t, 0.54 s)"),
    ]


HALL_PARAM = Param("record_hall", "Record Hall field", "", "bool", True, group="Acquisition",
                   help="Read the Hall probe at every point (MiniMoke always did)")
STAGE_PARAM = Param("position_stage", "Move stage to (x, y) first", "", "bool", True, group="Position",
                    help="MiniMoke moved the stage to (x, y) before field sweeps and time traces")
FIELD_SETTLE = Param("field_settle_s", "Field settle after field change", "s", default=1.0, min=0.0,
                     max=60.0, decimals=3, group="Acquisition",
                     help="hardware-timed wait after the coil command changes (MiniMoke waited 1 s "
                          "after the initial field only)")


@dataclass
class Workflow:
    key: str
    title: str
    short: str
    scan_type: str                         # Samba scan_type attribute
    params: list[Param]
    uses_lockin: bool
    uses_stage: str                        # "required", "optional", "no"
    x_key: str = ""
    minimoke_name: str = ""
    description: str = ""

    def defaults(self) -> dict:
        return {p.name: p.default for p in self.params}

    def param(self, name: str) -> Param:
        for p in self.params:
            if p.name == name:
                return p
        raise KeyError(name)

    def coerce(self, values: dict) -> dict:
        """Fill defaults, coerce types and enforce limits.  Raises ValueError."""
        out = {}
        for p in self.params:
            v = values.get(p.name, p.default)
            if p.dtype == "bool":
                v = bool(v)
            elif p.dtype == "int":
                v = int(round(float(v)))
            elif p.dtype == "choice":
                if v not in (p.choices or []):
                    raise ValueError(f"{p.label}: {v!r} not one of {p.choices}")
            else:
                v = float(v)
                if not math.isfinite(v):
                    raise ValueError(f"{p.label}: not a finite number")
            if p.dtype in ("float", "int"):
                if p.min is not None and v < p.min:
                    raise ValueError(f"{p.label}: {v:g} {p.unit} is below the minimum {p.min:g} {p.unit}")
                if p.max is not None and v > p.max:
                    raise ValueError(f"{p.label}: {v:g} {p.unit} is above the maximum {p.max:g} {p.unit}")
            out[p.name] = v
        return out

    def required_devices(self, params: dict, setup: str) -> list[str]:
        req = ["daq", "moke"]
        if params.get("record_hall", True) or self.key == "b_sweep_dc":
            req.append("hall")
        if self.uses_lockin:
            req.append("lockin")
        if self.uses_stage == "required" or (self.uses_stage == "optional" and params.get("position_stage")):
            req.append(f"stage_{setup}")
        return req


COIL = dict(unit="V", min=-6.0, max=6.0, decimals=4)

WORKFLOWS: dict[str, Workflow] = {}


def _add(wf: Workflow) -> None:
    WORKFLOWS[wf.key] = wf


_add(Workflow(
    "b_sweep_dc", "DC B-sweep", "B-DC", "FIELD",
    [
        Param("b_min", "From (coil command)", default=-0.5, group="Field", **COIL),
        Param("b_max", "To (coil command)", default=0.5, group="Field", **COIL),
        Param("b_step", "Step", "V", default=0.04, min=1e-8, max=12.0, decimals=5, group="Field"),
        Param("sweep_freq", "Sweep frequency", "Hz", default=1.0, min=0.01, max=100.0, decimals=4,
              group="Field", help="full loops per second; sets the time per point"),
        Param("num_sweeps", "Number of sweeps to average", "", "int", 5, 1, 1000, group="Field"),
        Param("x", "Position x", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("y", "Position y", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        STAGE_PARAM,
    ],
    uses_lockin=False, uses_stage="optional", x_key="actuator_coil", minimoke_name="B-Sweep",
    description="Fast repeated coil sweeps, balanced-diode DC signal, forward/backward averaged separately.",
))

_add(Workflow(
    "b_sweep_lockin", "Lock-in B-sweep", "B-LI", "FIELD",
    [
        Param("b_min", "From (coil command)", default=-0.5, group="Field", **COIL),
        Param("b_max", "To (coil command)", default=0.5, group="Field", **COIL),
        Param("b_step", "Step", "V", default=0.04, min=1e-8, max=12.0, decimals=5, group="Field"),
        Param("sweep_freq", "Max sweep frequency", "Hz", default=0.1, min=0.001, max=10.0, decimals=4,
              group="Field", help="upper bound; each point needs at least 5 τ + acquisition time"),
        Param("num_sweeps", "Number of sweeps to average", "", "int", 5, 1, 1000, group="Field"),
        *_lockin_params(volt=1.0, sensi=500e-6, freq=1777.0, tc=0.1, acq=0.1, freq_min=1.0, tc_min=10e-6,
                        tc_max=100.0, sensi_min=1e-9),
        Param("x", "Position x", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("y", "Position y", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        STAGE_PARAM,
    ],
    uses_lockin=True, uses_stage="optional", x_key="actuator_coil", minimoke_name="B-Sweep LockIn",
    description="Field sweeps with the 7270 oscillator modulating the sample current; X/Y read per point.",
))

_POS_COMMON = [
    FIELD_SETTLE,
    Param("settle_time", "Settle time after move", "s", default=0.0, min=0.0, max=60.0, decimals=3,
          group="Acquisition"),
    HALL_PARAM,
]

_add(Workflow(
    "x_sweep", "X-sweep", "X", "SPATIAL_X",
    [
        Param("x_min", "From x", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("x_max", "To x", "um", default=100.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("x_step", "Step", "um", default=10.0, min=1e-3, max=1e6, decimals=3, group="Position"),
        Param("y", "Position y", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("b", "Field (coil command)", default=0.0, group="Field", **COIL),
        Param("repeat_num", "Repeat number", "", "int", 5, 1, 1000, group="Field",
              help="each repeat measures +b then -b over all positions"),
        *_lockin_params(), *_POS_COMMON,
    ],
    uses_lockin=True, uses_stage="required", x_key="actuator_x", minimoke_name="X-Sweep",
))

_add(Workflow(
    "y_sweep", "Y-sweep", "Y", "SPATIAL_Y",
    [
        Param("y_min", "From y", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("y_max", "To y", "um", default=100.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("y_step", "Step", "um", default=10.0, min=1e-3, max=1e6, decimals=3, group="Position"),
        Param("x", "Position x", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("b", "Field (coil command)", default=0.0, group="Field", **COIL),
        Param("repeat_num", "Repeat number", "", "int", 5, 1, 1000, group="Field"),
        *_lockin_params(), *_POS_COMMON,
    ],
    uses_lockin=True, uses_stage="required", x_key="actuator_y", minimoke_name="Y-Sweep",
))

_add(Workflow(
    "xy_sweep", "XY map", "XY", "SPATIAL_XY",
    [
        Param("x_min", "From x", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("x_max", "To x", "um", default=100.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("x_step", "Step x", "um", default=10.0, min=1e-3, max=1e6, decimals=3, group="Position"),
        Param("y_min", "From y", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("y_max", "To y", "um", default=100.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("y_step", "Step y", "um", default=10.0, min=1e-3, max=1e6, decimals=3, group="Position"),
        Param("b", "Field (coil command)", default=0.0, group="Field", **COIL),
        Param("repeat_num", "Repeat number", "", "int", 1, 1, 100, group="Field",
              help="repeats the whole map"),
        *_lockin_params(), *_POS_COMMON,
    ],
    uses_lockin=True, uses_stage="required", x_key="actuator_x", minimoke_name="XY-Sweep",
))

_add(Workflow(
    "time", "Time trace", "T", "TIME",
    [
        Param("b", "Field (coil command)", default=0.0, group="Field", **COIL),
        Param("duration", "Duration", "s", default=60.0, min=1e-3, max=1e6, decimals=3, group="Time"),
        Param("interval", "Sampling interval", "s", default=0.5, min=1e-6, max=86400.0, decimals=4, group="Time"),
        *_lockin_params(volt=0.0, acq=0.5),
        Param("x", "Position x", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        Param("y", "Position y", "um", default=0.0, min=-1e6, max=1e6, decimals=3, group="Position"),
        STAGE_PARAM, HALL_PARAM,
    ],
    uses_lockin=True, uses_stage="optional", x_key="time", minimoke_name="Time",
))


#: MiniMoke procedures_config.ini section -> workflow key
MINIMOKE_SECTIONS = {wf.minimoke_name: wf.key for wf in WORKFLOWS.values()}
