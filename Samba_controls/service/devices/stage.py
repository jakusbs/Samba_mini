"""Three-axis stages -- shared logic.

User coordinates are millimetres relative to a software zero (MiniMoke's
"home": the raw position stored as an offset).  Scans use absolute moves only;
relative jogs are manual controls.  Every wait is bounded and cancellable; a
cancelled or timed-out move issues the controller's stop and reports the read
back position -- it is never retried automatically.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

from core.model import ArgInfo, AttributeInfo, CommandInfo, DevState
from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.base import CallContext, Device

log = logging.getLogger(__name__)

AXES = ("x", "y", "z")


class OffsetStore:
    """Software-zero offsets (raw controller steps) in ``state\\stage_offsets.json``."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self, stage: str) -> dict | None:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        entry = data.get(stage)
        return {a: float(entry[a]) for a in AXES if a in entry} if isinstance(entry, dict) else None

    def save(self, stage: str, offsets: dict) -> None:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data[stage] = {a: float(offsets[a]) for a in AXES}
            data["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            tmp = self.path.with_suffix(".tmp")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)


class StageBase(Device):
    KIND = "stage"
    MONITORED = ["position"]
    MONITOR_INTERVAL_S = 1.0

    def __init__(self, name, config=None, simulated=False, offsets: OffsetStore | None = None):
        config = dict(config or {})
        self.stage_key = config.get("key", name)
        self.limits = {a: tuple(config["limits_mm"][a]) for a in AXES
                       if isinstance(config.get("limits_mm"), dict) and a in config["limits_mm"]}
        self.move_timeout_s = float(config.get("move_timeout_s", 120.0))
        self.ATTRIBUTES = [
            AttributeInfo("position", "dict", "mm", "r", description="x, y, z in mm (software zero)"),
            AttributeInfo("x_mm", "float", "mm", "r", label="X"),
            AttributeInfo("y_mm", "float", "mm", "r", label="Y"),
            AttributeInfo("z_mm", "float", "mm", "r", label="Z"),
            AttributeInfo("moving", "bool", "", "r"),
            AttributeInfo("offsets_steps", "dict", "steps", "r", description="software-zero offsets"),
        ]
        axis_arg = ArgInfo("axis", "str", choices=list(AXES))
        self.COMMANDS = [
            CommandInfo("move_to", [axis_arg, ArgInfo("position_mm", "float", "mm", -1000, 1000)],
                        timeout_s=self.move_timeout_s + 10, description="Absolute move, waits for completion"),
            CommandInfo("move_xy", [ArgInfo("x_mm", "float", "mm", -1000, 1000),
                                    ArgInfo("y_mm", "float", "mm", -1000, 1000)],
                        timeout_s=2 * self.move_timeout_s + 10, description="Absolute X then Y move"),
            CommandInfo("jog", [axis_arg, ArgInfo("delta_mm", "float", "mm", -50, 50)],
                        idempotent=False, timeout_s=self.move_timeout_s + 10,
                        description="Relative move (manual control only); rejected while moving"),
            CommandInfo("stop", [], description="Stop all axes"),
            CommandInfo("set_zero", [ArgInfo("axis", "str", choices=list(AXES) + ["all"], default="all",
                                             required=False)],
                        description="Make the current position the software zero (MiniMoke 'home')"),
        ]
        super().__init__(name, config, simulated)
        self.offset_store = offsets
        self.offsets = {a: float(config.get("offsets_steps", {}).get(a, 0.0)) for a in AXES}
        self._stop_event = threading.Event()

    # ------------------------------------------------------------ primitives
    # raw positions are controller steps
    def hw_get_raw(self, axis: str) -> float: ...
    def hw_move_raw(self, axis: str, raw_target: float) -> None: ...
    def hw_move_rel_raw(self, axis: str, raw_delta: float) -> None: ...
    def hw_is_moving(self, axis: str) -> bool: ...
    def hw_stop(self, axis: str) -> None: ...
    def to_raw(self, axis: str, mm: float) -> float: ...       # absolute mm -> raw steps
    def from_raw(self, axis: str, raw: float) -> float: ...    # raw steps -> mm
    def delta_raw(self, axis: str, mm: float) -> float: ...    # relative mm -> raw steps

    # ------------------------------------------------------------ offsets
    def load_offsets(self) -> None:
        if self.offset_store is not None:
            stored = self.offset_store.load(self.stage_key)
            if stored:
                self.offsets.update(stored)

    def save_offsets(self) -> None:
        if self.offset_store is not None:
            self.offset_store.save(self.stage_key, self.offsets)

    # ------------------------------------------------------------ API
    def get_mm(self, axis: str) -> float:
        return self.from_raw(axis, self.hw_get_raw(axis))

    def positions(self) -> dict:
        pos = {a: self.get_mm(a) for a in AXES}
        t = time.time()
        for a in AXES:
            self.store(f"{a}_mm", self.make_reading(f"{a}_mm", pos[a], "device", t))
        return pos

    def check_limits(self, axis: str, mm: float) -> None:
        if not math.isfinite(mm):
            raise ServiceError(ErrorCode.INVALID_VALUE, f"{axis}: non-finite target")
        if axis in self.limits:
            lo, hi = self.limits[axis]
            if not (lo <= mm <= hi):
                raise ServiceError(ErrorCode.OUT_OF_RANGE,
                                   f"{self.name} {axis}={mm} mm outside configured limits [{lo}, {hi}] mm",
                                   {"min": lo, "max": hi})

    def move_to(self, axis: str, mm: float, ctx: CallContext | None = None) -> dict:
        self.check_limits(axis, mm)
        self._stop_event.clear()
        self.set_state(DevState.MOVING, f"moving {axis} to {mm:.4f} mm")
        try:
            self.hw_move_raw(axis, self.to_raw(axis, mm))
            self._wait(axis, ctx)
        finally:
            if self.state == DevState.MOVING:
                self.set_state(DevState.ON, "idle")
        actual = self.get_mm(axis)
        self.store(f"{axis}_mm", self.make_reading(f"{axis}_mm", actual, "device"))
        return {"axis": axis, "target_mm": mm, "position_mm": actual}

    def jog(self, axis: str, delta_mm: float, ctx: CallContext | None = None) -> dict:
        if self.hw_is_moving(axis):
            raise ServiceError(ErrorCode.BUSY, f"{self.name} {axis} is moving; jog rejected")
        start = self.get_mm(axis)
        self.check_limits(axis, start + delta_mm)
        self._stop_event.clear()
        self.set_state(DevState.MOVING, f"jogging {axis} by {delta_mm:+.5f} mm")
        try:
            self.hw_move_rel_raw(axis, self.delta_raw(axis, delta_mm))
            self._wait(axis, ctx)
        finally:
            if self.state == DevState.MOVING:
                self.set_state(DevState.ON, "idle")
        actual = self.get_mm(axis)
        return {"axis": axis, "start_mm": start, "position_mm": actual}

    def request_stop(self) -> None:
        """Thread-safe: ask a running wait loop to stop (it calls hw_stop)."""
        self._stop_event.set()

    def stop_all(self) -> None:
        for a in AXES:
            try:
                self.hw_stop(a)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s stop %s failed: %s", self.name, a, exc)
        if self.state == DevState.MOVING:
            self.set_state(DevState.ON, "stopped")

    def _wait(self, axis: str, ctx: CallContext | None) -> None:
        deadline = time.perf_counter() + self.move_timeout_s
        while self.hw_is_moving(axis):
            cancelled = (ctx is not None and ctx.cancel.cancelled) or self._stop_event.is_set()
            if cancelled or time.perf_counter() > deadline:
                try:
                    self.hw_stop(axis)
                finally:
                    pos = self.get_mm(axis)
                if cancelled:
                    raise ServiceError(ErrorCode.CANCELLED, f"{self.name} {axis} move stopped at {pos:.4f} mm",
                                       {"position_mm": pos})
                raise ServiceError(ErrorCode.TIMEOUT,
                                   f"{self.name} {axis} still moving after {self.move_timeout_s} s; "
                                   f"stopped at {pos:.4f} mm (not retried)", {"position_mm": pos})
            time.sleep(0.05)

    def cleanup(self, ctx: CallContext) -> list[str]:
        if not self.usable:
            return []
        try:
            self.stop_all()
            return []
        except Exception as exc:  # noqa: BLE001
            return [f"{self.name}: stop failed: {exc}"]

    # ------------------------------------------------------------ attributes
    def _read_attribute(self, name: str) -> Any:
        if name == "position":
            return self.positions()
        if name in ("x_mm", "y_mm", "z_mm"):
            return self.get_mm(name[0])
        if name == "moving":
            return any(self.hw_is_moving(a) for a in AXES)
        if name == "offsets_steps":
            return dict(self.offsets)
        raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, name)

    # ------------------------------------------------------------ commands
    def cmd_move_to(self, ctx, axis, position_mm):
        return self.move_to(axis, position_mm, ctx)

    def cmd_move_xy(self, ctx, x_mm, y_mm):
        self.check_limits("x", x_mm)
        self.check_limits("y", y_mm)
        rx = self.move_to("x", x_mm, ctx)
        ry = self.move_to("y", y_mm, ctx)
        return {"x_mm": rx["position_mm"], "y_mm": ry["position_mm"]}

    def cmd_jog(self, ctx, axis, delta_mm):
        return self.jog(axis, delta_mm, ctx)

    def cmd_stop(self, ctx):
        self.stop_all()
        return self.positions()

    def cmd_set_zero(self, ctx, axis="all"):
        for a in (AXES if axis == "all" else (axis,)):
            self.offsets[a] = float(self.hw_get_raw(a))
        self.save_offsets()
        return {"offsets_steps": dict(self.offsets)}

    def public_config(self) -> dict:
        return dict(self.config, offsets_steps=dict(self.offsets), limits_mm=self.limits)
