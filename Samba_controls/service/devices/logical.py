"""Logical devices.  They own no hardware; they coordinate the physical owners
through ``ctx.host.run(device, fn, ...)``, which executes on that instrument's
worker thread.

* ``magnet``  -- coil command (through the DAQ) and field readback (Hall probe).
* ``moke``    -- the acquisition sequencer: per-point timed sequences
  (DAQ window + Hall + lock-in) and the timed loops that must not depend on
  GUI round trips (field sweep, time trace).
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from core.model import ArgInfo, AttributeInfo, CommandInfo, DevState
from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.base import CallContext, Device

log = logging.getLogger(__name__)

HALL_MIN_ACQ_S = 0.002   # MiniMoke: fastest valid Hall reading per point


class Magnet(Device):
    DEFAULT_LABEL = "Magnet (coil command + Hall probe, logical)"
    KIND = "magnet"
    PHYSICAL = False

    def __init__(self, name, config=None, simulated=False):
        config = dict(config or {})
        self.amps_per_volt = config.get("amps_per_volt")
        self.mT_per_V = config.get("field_mT_per_V")
        self.ATTRIBUTES = [
            AttributeInfo("coil_command_V", "float", "V", "rw", config.get("coil_min_V", -6.0),
                          config.get("coil_max_V", 6.0), "Coil amplifier command (DAQ output)",
                          "Coil command"),
            AttributeInfo("field_mT", "float", "mT", "r", description="Hall probe reading", label="Field"),
            AttributeInfo("coil_current_A", "float", "A", "r",
                          description="Derived from the configured amplifier transfer (A/V); "
                                      "invalid when not configured", label="Coil current (derived)"),
            AttributeInfo("field_estimate_mT", "float", "mT", "r",
                          description="ESTIMATE from the configured calibration; not a measurement",
                          label="Field estimate"),
        ]
        self.COMMANDS = [
            CommandInfo("set_coil", [ArgInfo("coil_V", "float", "V", config.get("coil_min_V", -6.0),
                                             config.get("coil_max_V", 6.0))],
                        description="Write the coil command now"),
            CommandInfo("zero", [], description="Coil command to 0 V"),
        ]
        super().__init__(name, config, simulated)
        self.daq_name = config.get("daq", "daq")
        self.hall_name = config.get("hall", "hall")
        self.host = None

    def _connect(self) -> None:
        pass

    def bind_host(self, host) -> None:
        self.host = host

    def refresh_state(self) -> None:
        daq = self.host.device(self.daq_name)
        if daq.usable:
            self.set_state(DevState.ON, "coil available" + ("" if self.host.device(self.hall_name).usable
                                                              else "; Hall probe unavailable"))
        else:
            self.set_state(DevState.FAULT, f"DAQ {daq.state.value}")

    def _coil(self) -> float:
        r = self.host.read(self.daq_name, ["coil_command_V"])["coil_command_V"]
        return r["value"] if r["quality"] in ("VALID", "SIMULATED", "STALE") else math.nan

    def _read_attribute(self, name: str) -> Any:
        if name == "coil_command_V":
            return self._coil()
        if name == "field_mT":
            r = self.host.read(self.hall_name, ["field_mT"])["field_mT"]
            return r["value"]
        if name == "coil_current_A":
            if self.amps_per_volt is None:
                return math.nan
            return self._coil() * float(self.amps_per_volt)
        if name == "field_estimate_mT":
            if self.mT_per_V is None:
                return math.nan
            return self._coil() * float(self.mT_per_V)
        raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, name)

    def _write_attribute(self, name, value):
        if name == "coil_command_V":
            return self.cmd_set_coil(CallContext(host=self.host), value)["coil_V"]
        return super()._write_attribute(name, value)

    def cmd_set_coil(self, ctx, coil_V):
        self.host.run(self.daq_name, lambda d: d.static_outputs(coil_V, *d.ao_V), 10.0,
                      ctx.cancel, "set coil")
        return {"coil_V": coil_V}

    def cmd_zero(self, ctx):
        return self.cmd_set_coil(ctx, 0.0)

    def require_usable(self) -> None:
        self.refresh_state()
        super().require_usable()


class MokeSequencer(Device):
    DEFAULT_LABEL = "MOKE sequencer (logical)"
    KIND = "moke"
    PHYSICAL = False

    COMMANDS = [
        CommandInfo("measure_point", [
            ArgInfo("coil_V", "float", "V"),
            ArgInfo("acq_time_s", "float", "s", 1e-5, 600.0),
            ArgInfo("settle_s", "float", "s", 0.0, 600.0, default=0.0, required=False),
            ArgInfo("hall", "bool", default=True, required=False),
            ArgInfo("lockin", "bool", default=True, required=False),
            ArgInfo("lockin_after", "str", choices=["settle", "acquisition"], default="acquisition",
                    required=False),
            ArgInfo("token", "str", default="", required=False),
        ], timeout_s=120.0, description="One timed point: DAQ window (+settle prefix), Hall, lock-in"),
        CommandInfo("prepare", [
            ArgInfo("hall_integration_s", "float", "s", 0.0, 0.6, default=None, required=False),
        ], description="Set the Hall integration time"),
    ]
    OPERATIONS = [
        CommandInfo("field_sweep", [
            ArgInfo("setpoints_V", "list"),
            ArgInfo("branch", "list"),
            ArgInfo("t_point_s", "float", "s", 0.0, 3600.0),
            ArgInfo("settle_s", "float", "s", 0.0, 600.0),
            ArgInfo("acq_time_s", "float", "s", 1e-5, 600.0),
            ArgInfo("hall", "bool"),
            ArgInfo("lockin", "bool"),
            ArgInfo("auto_pause_on_error", "bool", default=True, required=False),
        ], description="One full sweep with per-point deadline pacing"),
        CommandInfo("time_trace", [
            ArgInfo("coil_V", "float", "V"),
            ArgInfo("n_points", "int", "", 1, 10_000_000),
            ArgInfo("interval_s", "float", "s", 1e-6, 86400.0),
            ArgInfo("acq_time_s", "float", "s", 1e-5, 600.0),
            ArgInfo("hall", "bool"),
            ArgInfo("lockin", "bool"),
        ], description="Points at a fixed field, paced by interval"),
    ]

    def __init__(self, name, config=None, simulated=False):
        super().__init__(name, config, simulated)
        c = self.config
        self.daq_name = c.get("daq", "daq")
        self.hall_name = c.get("hall", "hall")
        self.lockin_name = c.get("lockin", "lockin")
        self.host = None

    def bind_host(self, host) -> None:
        self.host = host

    def _connect(self) -> None:
        pass

    def _read_attribute(self, name):
        raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, name)

    def refresh_state(self) -> None:
        daq = self.host.device(self.daq_name)
        if daq.usable:
            self.set_state(DevState.ON, "ready")
        else:
            self.set_state(DevState.FAULT, f"DAQ {daq.state.value}")

    def require_usable(self) -> None:
        self.refresh_state()
        super().require_usable()

    # ------------------------------------------------------------------ point
    def _require(self, hall: bool, lockin: bool) -> None:
        names = [self.daq_name] + ([self.hall_name] if hall else []) + ([self.lockin_name] if lockin else [])
        for n in names:
            self.host.device(n).require_usable()

    def point(self, coil_V: float, acq_time_s: float, settle_s: float, hall: bool, lockin: bool,
              lockin_after: str, ctx: CallContext) -> dict:
        host = self.host
        self._require(hall, lockin)
        ctx.cancel.raise_if_cancelled()

        def start(d):
            d.configure_acquisition(acq_time_s, settle_s)
            return d.start_acquisition(coil_V)

        started = host.run(self.daq_name, start, 10.0, ctx.cancel, "start acquisition")
        acq_id = started["acq_id"]
        t_trig = started["t_trigger"]
        window_end = t_trig + started["settle_s"] + started["acq_time_s"]
        hall_job = lockin_job = None
        rec: dict[str, Any] = {"acq_id": acq_id, "coil_V": started["coil_V"], "t_trigger": t_trig}
        # Wait for the settle prefix (hardware timed on the DAQ) to pass.  If
        # anything below fails, the DAQ's pending acquisition is discarded by
        # the next start (tasks are stopped before every start).
        self.sleep(t_trig + started["settle_s"] - time.time(), ctx)
        if lockin and lockin_after == "settle":
            lockin_job = host.submit(self.lockin_name, lambda d: d.read_xy_rt(), 60.0, ctx.cancel, "read XY")
        if hall:
            # Integrates inside the DAQ window (same field as the optical data).
            hall_job = host.submit(self.hall_name, lambda d: d.read_mT(), 10.0 + acq_time_s,
                                   ctx.cancel, "read field")
        daq = host.run(self.daq_name, lambda d: d.read_acquisition(acq_id), acq_time_s + settle_s + 15.0,
                       ctx.cancel, "read acquisition")
        if daq["acq_id"] != acq_id:
            raise ServiceError(ErrorCode.INTERNAL, "stale acquisition result rejected")
        rec.update(daq)
        if lockin and lockin_after == "acquisition":
            lockin_job = host.submit(self.lockin_name, lambda d: d.read_xy_rt(), 60.0, ctx.cancel, "read XY")
        if hall_job is not None:
            h = host.wait(self.hall_name, hall_job, 10.0 + acq_time_s)
            rec.update({"hall_field_mT": h["field_mT"], "hall_t_start": h["t_start"],
                        "hall_t_end": h["t_end"], "hall_integration_s": h["integration_s"]})
        else:
            rec.update({"hall_field_mT": math.nan, "hall_t_start": math.nan, "hall_t_end": math.nan,
                        "hall_integration_s": math.nan})
        if lockin_job is not None:
            lk = host.wait(self.lockin_name, lockin_job, 60.0)
            rec.update({"lockin_x_V": lk["x_V"], "lockin_y_V": lk["y_V"], "lockin_r_V": lk["r_V"],
                        "lockin_theta_deg": lk["theta_deg"], "lockin_t": lk["t"]})
        else:
            rec.update({"lockin_x_V": math.nan, "lockin_y_V": math.nan, "lockin_r_V": math.nan,
                        "lockin_theta_deg": math.nan, "lockin_t": math.nan})
        rec["hall_measured"] = bool(hall)
        rec["lockin_measured"] = bool(lockin)
        rec["t_window_end"] = window_end
        return rec

    def cmd_measure_point(self, ctx, coil_V, acq_time_s, settle_s=0.0, hall=True, lockin=True,
                          lockin_after="acquisition", token=""):
        rec = self.point(coil_V, acq_time_s, settle_s, hall, lockin, lockin_after, ctx)
        rec["token"] = token
        return rec

    def cmd_prepare(self, ctx, hall_integration_s=None):
        out = {}
        if hall_integration_s is not None:
            out["hall_integration_s"] = self.host.run(
                self.hall_name, lambda d: d.set_integration(hall_integration_s), 10.0, ctx.cancel,
                "set integration")
        return out

    # ------------------------------------------------------------- operations
    def _gated_point(self, op, fn, label: str, auto_pause: bool):
        """Pause gate + MiniMoke-style auto-pause on hardware error (retry the
        same point after Continue).  Returns (held_seconds, record)."""
        held = 0.0
        while True:
            held += op.checkpoint()
            try:
                return held, fn()
            except ServiceError as exc:
                if exc.code == ErrorCode.CANCELLED or op.cancel.cancelled or not auto_pause:
                    raise
                msg = f"Hardware error at {label}: {exc.message} - PAUSED; fix the cause, then Continue"
                log.error(msg)
                op.emit({"kind": "error", "label": label, "error": exc.to_dict()})
                op.auto_pause(msg)

    def op_field_sweep(self, ctx, setpoints_V, branch, t_point_s, settle_s, acq_time_s, hall, lockin,
                       auto_pause_on_error=True):
        op = ctx.operation
        setpoints = [float(v) for v in setpoints_V]
        if len(branch) != len(setpoints):
            raise ServiceError(ErrorCode.INVALID_VALUE, "branch list length mismatch")
        daq = self.host.device(self.daq_name)
        for v in setpoints:
            daq.check_coil(v)
        if t_point_s < settle_s + acq_time_s:
            t_point_s = settle_s + acq_time_s
        n = len(setpoints)
        t_begin = start = time.perf_counter()
        lockin_after = "settle" if lockin else "acquisition"
        for i, v in enumerate(setpoints):
            due = start + i * t_point_s
            wait = due - time.perf_counter()
            if wait > 0:
                self.sleep(wait, ctx)
            held, rec = self._gated_point(
                op, lambda v=v: self.point(v, acq_time_s, settle_s, hall, lockin, lockin_after, ctx),
                f"point {i + 1}/{n} ({v:+.4g} V)", auto_pause_on_error)
            start += held
            rec.update({"kind": "point", "index": i, "branch": int(branch[i]), "setpoint_V": v,
                        "late_s": max(0.0, time.perf_counter() - (start + (i + 1) * t_point_s))})
            op.emit(rec)
            op.set_progress((i + 1) / n)
        return {"points": n, "elapsed_s": time.perf_counter() - t_begin, "paused_s": op.paused_total_s}

    def op_time_trace(self, ctx, coil_V, n_points, interval_s, acq_time_s, hall, lockin):
        op = ctx.operation
        self.host.device(self.daq_name).check_coil(coil_V)
        t0 = time.perf_counter()
        t0_wall = time.time()
        pace = t0
        for k in range(n_points):
            held, rec = self._gated_point(
                op, lambda: (time.perf_counter() - t0,
                             self.point(coil_V, acq_time_s, 0.0, hall, lockin, "acquisition", ctx)),
                f"point {k + 1}/{n_points}", True)
            pace += held
            elapsed, rec = rec
            rec.update({"kind": "point", "index": k, "elapsed_s": elapsed, "t0_wall": t0_wall})
            op.emit(rec)
            op.set_progress((k + 1) / n_points)
            due = pace + (k + 1) * interval_s
            wait = due - time.perf_counter()
            if wait > 0 and k + 1 < n_points:
                self.sleep(wait, ctx)
        return {"points": n_points, "paused_s": op.paused_total_s}

