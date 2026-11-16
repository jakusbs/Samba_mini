"""TANGO-inspired device model shared by the service and its clients.

This is *not* TANGO: there is no database, no PyTango and no Jive.  The model
borrows the vocabulary (device, attribute, command, state, status) so that the
backend exposes every instrument -- real, simulated or logical -- through the
same small contract.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DevState(str, Enum):
    ON = "ON"              # connected and idle
    OFF = "OFF"            # not connected (deliberately)
    MOVING = "MOVING"      # motion in progress
    RUNNING = "RUNNING"    # acquisition / operation in progress
    FAULT = "FAULT"        # connection or driver error; see status / last_error
    DISABLED = "DISABLED"  # not configured / driver package or runtime missing
    UNKNOWN = "UNKNOWN"    # state could not be determined (e.g. after a crash)
    INIT = "INIT"          # connecting


#: States in which a device may be used by a measurement.
USABLE_STATES = {DevState.ON, DevState.MOVING, DevState.RUNNING}


class Quality(str, Enum):
    VALID = "VALID"          # fresh reading from the instrument
    INVALID = "INVALID"      # no reading available (value is NaN / None)
    STALE = "STALE"          # cached value; see age_s
    SIMULATED = "SIMULATED"  # produced by a simulated device


@dataclass
class AttributeInfo:
    name: str
    dtype: str                    # "float", "int", "bool", "str", "array"
    unit: str = ""
    access: str = "r"             # "r" or "rw"
    min: float | None = None
    max: float | None = None
    description: str = ""
    label: str = ""
    choices: list | None = None   # allowed discrete values (e.g. sensitivities)

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self, value: Any) -> Any:
        """Validate and coerce a value for writing.  Raises ValueError."""
        if self.access != "rw":
            raise PermissionError(f"attribute {self.name} is read-only")
        return coerce(self.dtype, value, self.min, self.max, self.choices, self.name)


@dataclass
class ArgInfo:
    name: str
    dtype: str
    unit: str = ""
    min: float | None = None
    max: float | None = None
    default: Any = None
    required: bool = True
    description: str = ""
    choices: list | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CommandInfo:
    name: str
    args: list[ArgInfo] = field(default_factory=list)
    returns: str = "none"
    mutating: bool = True         # needs the setup lease
    idempotent: bool = True       # safe to repeat after an unknown outcome
    timeout_s: float = 10.0
    description: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["args"] = [a.to_dict() for a in self.args]
        return d

    def bind(self, raw: dict | None) -> dict:
        """Validate a call's arguments against the declaration."""
        raw = dict(raw or {})
        bound = {}
        for arg in self.args:
            if arg.name in raw:
                bound[arg.name] = coerce(arg.dtype, raw.pop(arg.name), arg.min, arg.max,
                                         arg.choices, f"{self.name}.{arg.name}")
            elif arg.required and arg.default is None:
                raise ValueError(f"missing argument {arg.name!r} for {self.name}")
            else:
                bound[arg.name] = arg.default
        if raw:
            raise ValueError(f"unexpected argument(s) for {self.name}: {sorted(raw)}")
        return bound


def coerce(dtype: str, value: Any, vmin=None, vmax=None, choices=None, what: str = "value") -> Any:
    if dtype == "float":
        try:
            v = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{what}: expected a number, got {value!r}") from exc
        if math.isnan(v) or math.isinf(v):
            raise ValueError(f"{what}: non-finite value {value!r}")
    elif dtype == "int":
        if isinstance(value, bool):
            raise ValueError(f"{what}: expected an integer")
        try:
            v = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{what}: expected an integer, got {value!r}") from exc
        if isinstance(value, float) and value != v:
            raise ValueError(f"{what}: expected an integer, got {value!r}")
    elif dtype == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{what}: expected true/false, got {value!r}")
        v = value
    elif dtype == "str":
        if not isinstance(value, str):
            raise ValueError(f"{what}: expected text, got {value!r}")
        v = value
    elif dtype in ("array", "list", "dict", "any"):
        v = value
    else:
        raise ValueError(f"{what}: unknown dtype {dtype}")
    if choices is not None and v not in choices:
        raise ValueError(f"{what}: {v!r} not one of {choices}")
    if dtype in ("float", "int"):
        if vmin is not None and v < vmin:
            raise OutOfRange(what, v, vmin, vmax)
        if vmax is not None and v > vmax:
            raise OutOfRange(what, v, vmin, vmax)
    return v


class OutOfRange(ValueError):
    def __init__(self, what, value, vmin, vmax):
        super().__init__(f"{what}: {value!r} outside the allowed range [{vmin}, {vmax}]")
        self.what, self.value, self.vmin, self.vmax = what, value, vmin, vmax


@dataclass
class Reading:
    """One attribute value as served to clients."""

    value: Any
    quality: Quality
    t: float = field(default_factory=time.time)
    unit: str = ""
    age_s: float = 0.0
    source: str = ""              # "device", "operation", "cache"

    def to_dict(self) -> dict:
        return {"value": self.value, "quality": self.quality.value, "t": self.t,
                "unit": self.unit, "age_s": self.age_s, "source": self.source}

    @classmethod
    def from_dict(cls, d: dict) -> "Reading":
        return cls(value=d.get("value"), quality=Quality(d.get("quality", "INVALID")),
                   t=d.get("t", 0.0), unit=d.get("unit", ""), age_s=d.get("age_s", 0.0),
                   source=d.get("source", ""))

    @property
    def valid(self) -> bool:
        return self.quality in (Quality.VALID, Quality.SIMULATED)
