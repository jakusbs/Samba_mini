"""Qt-free scan engine.

Runs one measurement through the device service.  The GUI (or a test, or a
script) creates a :class:`ScanEngine` with a :class:`RunRequest` and callbacks,
then calls :meth:`start` (thread) or :meth:`run` (blocking).

State machine::

    IDLE -> PREPARING -> RUNNING <-> PAUSED -> STOPPING -> FINISHED
                                     (user or auto-pause)

Final status written to the file and reported: ``completed``, ``aborted``
(user abort), ``failed`` (error), ``interrupted`` (lease or service lost:
hardware state not verified by this client).

Pause holds after the current point with field, outputs and position
unchanged; for backend operations it is forwarded to the operation.  A
hardware error during a point auto-pauses (MiniMoke behaviour): Resume retries
the same point (only absolute moves are ever repeated), Abort ends the run.
A failed data-file write also auto-pauses; the points stay in memory and are
written on Resume.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from core.client import OperationHandle, ServiceClient
from core.protocol import ErrorCode, ServiceError, new_id
from core.scan import plans
from core.scan.channels import BOOKKEEPING, Channel, sensors_for
from core.scan.workflows import WORKFLOWS, Workflow
from core.storage.hdf5_writer import DatasetSpec, ScanWriter, StorageError, make_filename
from core.version import describe, software_revision

log = logging.getLogger(__name__)

IDLE, PREPARING, RUNNING, PAUSED, STOPPING, FINISHED = (
    "IDLE", "PREPARING", "RUNNING", "PAUSED", "STOPPING", "FINISHED")

UM_PER_MM = 1000.0
INITIAL_FIELD_WAIT_S = 1.0          # MiniMoke: "Setting magnetic field ..., waiting 1 s"
BACKEND_LOST_AFTER = 3              # consecutive RPC timeouts


class Abort(Exception):
    pass


class Interrupted(Exception):
    pass


@dataclass
class RunRequest:
    workflow: str
    params: dict
    setup: str = "longitudinal"           # longitudinal | polar
    geometry: str = "LMOKE"               # PMOKE | LMOKE (polar => PMOKE)
    sample: str = "sample"
    operator: str = ""
    notes: str = ""
    config_name: str = ""
    save_dir: str = "."
    export_csv: bool = False
    lab_notebook: str | None = None       # path of the lab-notebook CSV (None = skip)
    #: Samba's further metadata fields (device_id, incidence, mirror_shift, polarization, lam2, lam4,
    #: noDC, r_4wire_ohm, r_2wire_ohm, fm_thickness_nm, t_stack_nm) -> /metadata, same keys as Samba
    extra_metadata: dict = field(default_factory=dict)
    #: Samba's λ/2 (BD) calibration: six mV values at ticks 0, 5, 10, 15, 20, 25 -> /data/calibration
    #: (all zeros = no calibration, not written, as in Samba)
    bd_calibration: list | None = None


@dataclass
class Callbacks:
    state: Callable[[str, str], None] = lambda s, m: None
    point: Callable[[dict], None] = lambda e: None
    progress: Callable[[int, int], None] = lambda d, t: None
    log: Callable[[str, str], None] = lambda level, msg: None
    finished: Callable[[dict], None] = lambda r: None


class ScanEngine:
    def __init__(self, client: ServiceClient, request: RunRequest, callbacks: Callbacks | None = None):
        self.client = client
        self.req = request
        self.cb = callbacks or Callbacks()
        self.wf: Workflow = WORKFLOWS[request.workflow]
        self.params = self.wf.coerce(request.params)
        self.state = IDLE
        self.message = ""
        self._abort = threading.Event()
        self._pause = threading.Event()
        self._lease_lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._op: OperationHandle | None = None
        self._writer: ScanWriter | None = None
        self._pending_writes: list[tuple] = []
        self._rpc_failures = 0
        self.result: dict | None = None
        self.path: Path | None = None
        self.points_done = 0
        self.points_total = 0
        self._t_start = 0.0
        self._last_xy_um: tuple[float, float] = (math.nan, math.nan)
        self.mode = "unknown"

    # ================================================================ control
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="scan-engine", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def pause(self) -> None:
        if self.state not in (RUNNING, PREPARING):
            return
        self._pause.set()
        op = self._op
        if op is not None and not op.done:
            try:
                op.pause()
            except ServiceError as exc:
                self._log("warning", f"pause not forwarded: {exc.message}")
        self._log("info", "Pause requested - the run holds after the current point "
                          "(field, outputs and position stay where they are).")

    def resume(self) -> None:
        if self.state != PAUSED and not self._pause.is_set():
            return
        self._pause.clear()
        op = self._op
        if op is not None and not op.done:
            try:
                op.resume()
            except ServiceError as exc:
                self._log("warning", f"resume not forwarded: {exc.message}")
        self._log("info", "Resumed.")

    def abort(self) -> None:
        if self.state == FINISHED:
            return
        self._abort.set()
        self._pause.clear()
        op = self._op
        if op is not None and not op.done:
            try:
                op.abort()
            except ServiceError as exc:
                self._log("warning", f"abort not forwarded to the operation: {exc.message}")
        if self.wf.uses_stage == "required" or self.params.get("position_stage"):
            def stop_stage():   # stop a move that the engine thread may be waiting for
                try:
                    self.client.device(f"stage_{self.req.setup}").command("stop", timeout_s=15.0)
                except ServiceError:
                    pass
            threading.Thread(target=stop_stage, name="abort-stage-stop", daemon=True).start()
        self._log("warning", "Abort requested.")

    def lease_lost(self) -> None:
        """Called by the GUI when the client lost the lease (heartbeat thread)."""
        self._lease_lost.set()
        self._abort.set()

    # ================================================================ helpers
    def _set_state(self, state: str, message: str = "") -> None:
        self.state = state
        self.message = message
        try:
            self.cb.state(state, message)
        except Exception:  # noqa: BLE001
            log.exception("state callback")

    def _log(self, level: str, msg: str) -> None:
        getattr(log, level if level in ("info", "warning", "error", "debug") else "info")(msg)
        try:
            self.cb.log(level, msg)
        except Exception:  # noqa: BLE001
            log.exception("log callback")

    def _check(self) -> None:
        if self._lease_lost.is_set():
            raise Interrupted("control of the setup (lease) was lost")
        if self._abort.is_set():
            raise Abort()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while True:
            self._check()
            left = end - time.monotonic()
            if left <= 0:
                return
            self._abort.wait(min(left, 0.1))

    def _gate(self) -> None:
        """Between points: hold while paused; raise on abort."""
        self._check()
        if not self._pause.is_set():
            return
        self._set_state(PAUSED, self.message or "Paused")
        while self._pause.is_set():
            self._check()
            self._abort.wait(0.1)
        self._check()
        self._set_state(RUNNING, "")

    def _auto_pause(self, msg: str) -> None:
        self._log("error", msg)
        self._pause.set()
        self._set_state(PAUSED, msg)

    def _rpc(self, fn: Callable[[], Any], what: str) -> Any:
        """Run a service call; consecutive timeouts mean the service is gone."""
        try:
            result = fn()
        except ServiceError as exc:
            if exc.code == ErrorCode.TIMEOUT and "did not answer" in exc.message:
                self._rpc_failures += 1
                if self._rpc_failures >= BACKEND_LOST_AFTER:
                    raise Interrupted(f"the device service stopped answering ({what})") from exc
            elif exc.code in (ErrorCode.AUTH_REQUIRED,):
                raise Interrupted(f"session lost: {exc.message}") from exc
            elif exc.code in (ErrorCode.LEASE_REQUIRED, ErrorCode.LEASE_HELD, ErrorCode.LEASE_RECONCILING):
                raise Interrupted(f"lease lost: {exc.message}") from exc
            raise
        self._rpc_failures = 0
        return result

    def _dev(self, name: str):
        return self.client.device(name)

    # ================================================================ run
    def run(self) -> dict:
        status, message, cleanup_problems = "failed", "", []
        csv_path = None
        self._t_start = time.time()
        self._set_state(PREPARING, f"Preparing {self.wf.title}")
        began = False
        try:
            hello = self.client.hello()
            self.mode = hello["mode"]
            self._validate()
            if not self.client.lease_holder:
                self.client.acquire_lease()
            required = self.wf.required_devices(self.params, self.req.setup)
            self._rpc(lambda: self.client.call("run.begin", {"required_devices": required,
                                                             "workflow": self.wf.key}), "run.begin")
            began = True
            self._open_file(required)
            self._set_state(RUNNING, "")
            body = {"b_sweep_dc": self._run_b_sweep, "b_sweep_lockin": self._run_b_sweep,
                    "x_sweep": self._run_position, "y_sweep": self._run_position,
                    "xy_sweep": self._run_position, "time": self._run_time}[self.wf.key]
            body()
            status = "completed"
        except Abort:
            status, message = "aborted", "aborted by the operator"
        except Interrupted as exc:
            status, message = "interrupted", f"{exc}; hardware state not verified by this client"
        except ServiceError as exc:
            status, message = "failed", f"{exc.code}: {exc.message}"
        except StorageError as exc:
            status, message = "failed", f"data file error: {exc}"
        except Exception as exc:  # noqa: BLE001
            status, message = "failed", f"{type(exc).__name__}: {exc}"
            log.error("engine crashed:\n%s", traceback.format_exc())
        self._set_state(STOPPING, message or "Finishing")
        if began and status != "interrupted":
            try:
                if status == "completed" and self.wf.uses_stage == "required":
                    self._return_to_start()
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"could not return to the start position: {exc}")
            try:
                self.client.hello(3.0)
                res = self.client.call("run.end", {"cleanup": True}, 60.0)
                cleanup_problems = res.get("cleanup_problems", [])
            except ServiceError as exc:
                cleanup_problems = [f"cleanup not confirmed: {exc.message}"]
        elif status == "interrupted":
            cleanup_problems = ["cleanup not confirmed: the service performs it when the lease expires"]
        for p in cleanup_problems:
            self._log("error", f"cleanup: {p}")
        self._finalize_file(status, message, cleanup_problems)
        if self.path is not None and self.req.export_csv and self.points_done:
            try:
                from core.storage.csv_export import export_csv
                csv_path = export_csv(self.path)
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"CSV export failed: {exc}")
        self._lab_notebook(status)
        self.result = {"status": status, "message": message, "path": str(self.path) if self.path else None,
                       "csv_path": str(csv_path) if csv_path else None, "points": self.points_done,
                       "points_planned": self.points_total, "cleanup_problems": cleanup_problems,
                       "mode": self.mode}
        self._set_state(FINISHED, f"{status}: {message}" if message else status)
        self._log("info" if status == "completed" else "warning",
                  f"Run {status} - {self.points_done}/{self.points_total} points"
                  + (f" - {self.path}" if self.path else ""))
        try:
            self.cb.finished(self.result)
        except Exception:  # noqa: BLE001
            log.exception("finished callback")
        return self.result

    def _validate(self) -> None:
        p = self.params
        msg = None
        if self.wf.key == "b_sweep_dc":
            msg = plans.b_sweep_dc_validation(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"])
        elif self.wf.key == "b_sweep_lockin":
            msg = plans.b_sweep_lockin_validation(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"],
                                                  p["time_const"], p["acq_time"])
        if msg:
            raise ServiceError(ErrorCode.INVALID_VALUE, msg)
        if self.req.setup not in ("longitudinal", "polar"):
            raise ServiceError(ErrorCode.INVALID_VALUE, "setup must be longitudinal or polar")

    # ================================================================ file
    def _geometry(self) -> str:
        return "PMOKE" if self.req.setup == "polar" else self.req.geometry

    def _layout(self) -> tuple[tuple, list[DatasetSpec], dict]:
        """Return (main shape, dataset specs, root attrs) for this workflow."""
        p = self.params
        sens = sensors_for(self.wf.uses_lockin)
        specs: list[DatasetSpec] = []

        def add_common(group="data", shape=None):
            for ch in sens + BOOKKEEPING:
                specs.append(DatasetSpec(ch.name, ch.role, ch.label, ch.unit, group, ch.dtype, shape))
            specs.append(DatasetSpec("time", "time", "Time since start", "s", group, "f8", shape))

        key = self.wf.key
        if key in ("b_sweep_dc", "b_sweep_lockin"):
            plan = self._field_plan()
            shape = (int(p["num_sweeps"]), plan.n_total)
            specs += [DatasetSpec("actuator_coil_setpoint", "x_setpoint", "Coil command setpoint", "V"),
                      DatasetSpec("actuator_coil", "x", "Coil command", "V"),
                      DatasetSpec("cycle", "index", "Sweep", "", dtype="i8"),
                      DatasetSpec("branch", "index", "Branch (0 fwd, 1 bwd)", "", dtype="i8")]
            add_common()
            n = (plan.n_total,)
            specs += [DatasetSpec("actuator_coil_setpoint", "x_setpoint", "Coil command setpoint", "V",
                                  "data/averaged", shape=n),
                      DatasetSpec("actuator_coil", "x", "Coil command", "V", "data/averaged", shape=n),
                      DatasetSpec("branch", "index", "Branch (0 fwd, 1 bwd)", "", "data/averaged", "i8", n),
                      DatasetSpec("n_sweeps", "index", "Sweeps averaged", "", "data/averaged", "i8", n)]
            for ch in sens:
                if ch.name in ("dc_signal", "intensity", "hall_field", "lockin_x", "lockin_r"):
                    specs.append(DatasetSpec(ch.name, "sensor", f"{ch.label} (mean)", ch.unit,
                                             "data/averaged", shape=n))
            root = {"_x_key": "actuator_coil", "_is_2d": False, "_is_time": False, "_n_x": plan.n_total}
            self.points_total = shape[0] * shape[1]
            return shape, specs, root
        if key in ("x_sweep", "y_sweep"):
            ax = "x" if key == "x_sweep" else "y"
            other = "y" if ax == "x" else "x"
            pts, values = self._position_points()
            n_loops = 2 * int(p["repeat_num"])
            shape = (n_loops, len(values))
            specs += [DatasetSpec(f"actuator_{ax}_setpoint", "x_setpoint", f"{ax.upper()} target", "um"),
                      DatasetSpec(f"actuator_{ax}", "x", f"{ax.upper()} position", "um"),
                      DatasetSpec(f"position_{other}", "readback", f"{other.upper()} position", "um"),
                      DatasetSpec("coil_setpoint", "readback", "Coil command", "V"),
                      DatasetSpec("loop", "index", "Loop", "", dtype="i8"),
                      DatasetSpec("iteration", "index", "Iteration", "", dtype="i8")]
            add_common()
            root = {"_x_key": f"actuator_{ax}", "_is_2d": False, "_is_time": False, "_n_x": len(values)}
            self.points_total = len(pts)
            return shape, specs, root
        if key == "xy_sweep":
            pts, xs, ys = self._position_points()
            shape = (len(ys), len(xs))
            groups = ["data"] + [f"data/repeat_{r + 1:03d}" for r in range(1, int(p["repeat_num"]))]
            for g in groups:
                specs += [DatasetSpec("actuator_x_setpoint", "x_setpoint", "X target", "um", g),
                          DatasetSpec("actuator_x", "x", "X position", "um", g),
                          DatasetSpec("actuator_y_setpoint", "y_setpoint", "Y target", "um", g),
                          DatasetSpec("actuator_y", "y", "Y position", "um", g),
                          DatasetSpec("coil_setpoint", "readback", "Coil command", "V", g)]
                add_common(g)
            root = {"_x_key": "actuator_x", "_is_2d": True, "_is_time": False, "_n_x": len(xs)}
            self.points_total = len(pts)
            return shape, specs, root
        # time
        n = plans.time_points(p["duration"], p["interval"])
        shape = (n,)
        specs += [DatasetSpec("coil_setpoint", "readback", "Coil command", "V")]
        add_common()
        for s in specs:
            if s.name == "time" and s.group == "data":
                s.role = "x"
                s.label = "Time"
        root = {"_x_key": "time", "_is_2d": False, "_is_time": True, "_n_x": n}
        self.points_total = n
        return shape, specs, root

    def _open_file(self, required: list[str]) -> None:
        shape, specs, root = self._layout()
        simulated = self.mode == "simulation"
        self.path = make_filename(self.req.save_dir, self.wf.scan_type, self.req.sample,
                                  self.req.config_name, simulated)
        devices = {}
        for name in required:
            try:
                info = self.client.device(name).info(refresh=True)
                devices[name] = {"identity": info["identity"], "state": info["state"],
                                 "simulated": info["simulated"], "config": info.get("config")}
            except ServiceError as exc:
                devices[name] = {"error": exc.message}
        try:
            machine = self.client.call("config.get")
            machine_snapshot = machine.get("data")
        except ServiceError as exc:
            machine_snapshot = {"error": exc.message}
        user = (self.client.user or {}).get("username", "")
        rev = software_revision()
        coil_cfg = (machine_snapshot or {}).get("coil", {}) if isinstance(machine_snapshot, dict) else {}
        cal_cfg = (machine_snapshot or {}).get("field_calibration", {}) if isinstance(machine_snapshot, dict) else {}
        p = self.params
        metadata = {
            "scan_name": f"{self.wf.title} {self.req.sample}",
            "workflow": self.wf.key,
            "workflow_title": self.wf.title,
            "minimoke_procedure": self.wf.minimoke_name,
            "params_json": p,
            "n_x": root["_n_x"],
            "n_y": shape[0] if len(shape) == 2 else 1,
            "points_planned": self.points_total,
            "integration_time": p.get("acq_time", float("nan")),
            "settle_time": p.get("settle_time", 0.0),
            "operator": self.req.operator or user,
            "user": user,
            "sample_id": self.req.sample,
            "notes": self.req.notes,
            "config_name": self.req.config_name,
            "setup": self.req.setup,
            "geometry": self._geometry(),
            "stage_type": "Thorlabs Kinesis" if self.req.setup == "longitudinal" else "Trinamic TMCM-3212",
            "mode": self.mode,
            "devices_json": devices,
            "machine_config_json": machine_snapshot,
            "calibration_json": {"coil_amps_per_volt": coil_cfg.get("amps_per_volt"),
                                 "field_mT_per_V": cal_cfg.get("mT_per_V"),
                                 "note": "Coil values are DAQ output volts. Field values are Hall-probe "
                                         "readings; no estimated field is stored as a measurement."},
            "units_json": {"coil": "V (DAQ analogue output; MiniMoke labelled it 'A')",
                           "positions": "um (stage software zero)", "hall_field": "mT",
                           "lockin_xyr": "V", "lockin_theta": "deg", "diodes": "V", "time": "s"},
            "software_version": describe(),
            "samba_mini_version": rev.get("version"),
            "samba_mini_git_commit": rev.get("git_commit") or "",
            "samba_mini_git_dirty": bool(rev.get("git_dirty")),
            "hostname": socket.gethostname(),
            "service_instance": self.client.instance_id or "",
            "run_id": new_id(),
        }
        for k, v in (self.req.extra_metadata or {}).items():
            metadata.setdefault(str(k), v)            # never overrides the engine's own keys
        root = dict(root, scan_type=self.wf.scan_type, mode=self.mode)
        self._writer = ScanWriter(self.path, shape, specs, root, metadata)
        self._writer.create()
        cal = [float(v) for v in (self.req.bd_calibration or [])]
        if len(cal) == 6 and any(v != 0.0 for v in cal):
            self._writer.add_static("calibration", cal, unit="mV", role="calibration",
                                    label="λ/2 calibration (ticks 0, 5, 10, 15, 20, 25)")
        self._log("info", f"Saving to {self.path}" + (" (SIMULATION)" if simulated else ""))

    def _write(self, index: tuple, values: dict, group: str = "data", count: bool = True) -> None:
        """Write (or queue on failure, then auto-pause)."""
        self._pending_writes.append((index, values, group, count))
        self._flush_pending()

    def _flush_pending(self) -> None:
        while self._pending_writes:
            index, values, group, count = self._pending_writes[0]
            try:
                self._writer.write(index, values, group, count)
            except StorageError as exc:
                self._auto_pause(f"Cannot write the data file ({exc}). {len(self._pending_writes)} point(s) "
                                 "are kept in memory. Fix the disk problem, then Resume (or Abort).")
                op = self._op
                if op is not None and not op.done:
                    try:
                        op.pause()
                    except ServiceError:
                        pass
                return
            self._pending_writes.pop(0)

    def _finalize_file(self, status: str, message: str, cleanup_problems: list[str]) -> None:
        if self._writer is None:
            return
        try:
            self._flush_pending()
        except Exception:  # noqa: BLE001
            pass
        lost = len(self._pending_writes)
        try:
            self._writer.finalize(status, {"stop_reason": message, "cleanup_problems_json": cleanup_problems,
                                           "unsaved_points": lost})
        except Exception as exc:  # noqa: BLE001
            self._log("error", f"could not finalise {self.path}: {exc}")

    def _lab_notebook(self, status: str) -> None:
        if not self.req.lab_notebook or self.path is None:
            return
        try:
            from core.storage.lab_notebook import append_run
            append_run(self.req.lab_notebook, self, status)
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"lab notebook not updated (is it open in Excel?): {exc}")

    # ================================================================ shared steps
    def _field_plan(self):
        p = self.params
        if self.wf.key == "b_sweep_dc":
            return plans.b_sweep_dc_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"])
        return plans.b_sweep_lockin_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"],
                                         p["time_const"], p["acq_time"])

    def _position_points(self):
        p = self.params
        if self.wf.key == "x_sweep":
            return plans.x_sweep_points(p["x_min"], p["x_max"], p["x_step"], p["y"], p["b"], p["repeat_num"])
        if self.wf.key == "y_sweep":
            return plans.y_sweep_points(p["y_min"], p["y_max"], p["y_step"], p["x"], p["b"], p["repeat_num"])
        return plans.xy_points(p["x_min"], p["x_max"], p["x_step"], p["y_min"], p["y_max"], p["y_step"],
                               p["b"], p["repeat_num"])

    def _initial_field(self, coil_V: float) -> None:
        """MiniMoke: if the first field is not zero, set it and wait 1 s."""
        if coil_V != 0:
            self._log("info", f"Setting coil command to {coil_V:+.4g} V, waiting {INITIAL_FIELD_WAIT_S:g} s")
            self._rpc(lambda: self._dev("daq").command("set_outputs", coil_V=coil_V), "set coil")
            self._sleep(INITIAL_FIELD_WAIT_S)

    def _move_xy_um(self, x_um: float, y_um: float) -> tuple[float, float]:
        stage = self._dev(f"stage_{self.req.setup}")
        self._log("info", f"Moving {self.req.setup} stage to ({x_um:g} um, {y_um:g} um)")
        res = self._rpc(lambda: stage.command("move_xy", x_mm=x_um / UM_PER_MM, y_mm=y_um / UM_PER_MM),
                        "stage move")
        self._last_xy_um = (res["x_mm"] * UM_PER_MM, res["y_mm"] * UM_PER_MM)
        return self._last_xy_um

    def _configure_lockin(self) -> None:
        p = self.params
        applied = self._rpc(lambda: self._dev("lockin").command(
            "configure", volt=p["volt"], sensi=p["sensi"], freq=p["lockin_freq"], time_const=p["time_const"],
            phase=p["phase"]), "lock-in configure")
        self._log("info", "Lock-in configured (single reference): " + ", ".join(
            f"{k}={v:g}" for k, v in applied.items() if isinstance(v, (int, float))))
        if self._writer:
            self._writer.set_metadata(lockin_applied_json=applied)

    def _prepare_hall(self, integration_s: float) -> None:
        res = self._rpc(lambda: self._dev("moke").command("prepare", hall_integration_s=integration_s), "hall")
        if self._writer:
            self._writer.set_metadata(hall_integration_s=res.get("hall_integration_s", integration_s))

    def _return_to_start(self) -> None:
        pts, *_ = self._position_points()
        if pts:
            self._move_xy_um(pts[0].x_um, pts[0].y_um)

    def _emit(self, event: dict) -> None:
        try:
            self.cb.point(event)
        except Exception:  # noqa: BLE001
            log.exception("point callback")
        try:
            self.cb.progress(self.points_done, self.points_total)
        except Exception:  # noqa: BLE001
            log.exception("progress callback")

    def _sensor_values(self, rec: dict) -> dict:
        vals = {}
        for ch in sensors_for(self.wf.uses_lockin) + BOOKKEEPING:
            v = rec.get(ch.key, math.nan)
            vals[ch.name] = math.nan if v is None else v
        vals["time"] = rec.get("t_trigger", math.nan) - self._t_start if rec.get("t_trigger") else math.nan
        return vals

    # ================================================================ operations
    def _consume(self, op: OperationHandle, on_record: Callable[[dict], None]) -> dict:
        """Pull an operation's output until it finishes; mirror pause state."""
        self._op = op
        abort_sent = False
        try:
            while True:
                try:
                    res = self._rpc(lambda: op.poll(0.5), "op.wait")
                except ServiceError as exc:
                    if exc.code == ErrorCode.TIMEOUT:
                        continue          # op.wait is repeatable; _rpc escalates after repeated loss
                    raise
                for rec in res["records"]:
                    if rec.get("kind") == "error":
                        self._log("error", f"{rec.get('label')}: {rec['error'].get('message')}")
                        continue
                    on_record(rec)
                st = res["state"]
                if st == "PAUSED" and self.state != PAUSED:
                    self._set_state(PAUSED, res.get("message") or "Paused")
                    self._pause.set()
                elif st == "RUNNING" and self.state == PAUSED:
                    if self._pending_writes:
                        self._flush_pending()
                    if not self._pending_writes:
                        self._pause.clear()
                        self._set_state(RUNNING, "")
                if op.done:
                    break
                if (self._abort.is_set() or self._lease_lost.is_set()) and not abort_sent:
                    abort_sent = True
                    try:
                        op.abort()
                    except ServiceError:
                        pass
                if self.state == PAUSED and not self._pause.is_set() and st == "PAUSED":
                    try:
                        op.resume()
                    except ServiceError:
                        pass
        finally:
            self._op = None
        if res["state"] == "FAILED":
            err = res.get("error") or {}
            raise ServiceError(err.get("code", ErrorCode.INTERNAL), err.get("message", "operation failed"))
        if res["state"] == "ABORTED":
            self._check()
            # Not requested by this client: the service stopped it (shutdown,
            # lease taken away, ...).
            err = res.get("error") or {}
            raise Interrupted(f"the service aborted the operation ({err.get('message', 'no reason given')})")
        return res

    # ================================================================ workflows
    def _run_b_sweep(self) -> None:
        p = self.params
        lockin = self.wf.key == "b_sweep_lockin"
        plan = self._field_plan()
        n_sweeps = int(p["num_sweeps"])
        self._log("info", f"Points per sweep: {plan.n_total} ({len(plan.forward)} fwd + {len(plan.backward)} bwd)"
                          f" | T_point {plan.t_point_s * 1e3:.2f} ms | settle {plan.settle_s * 1e3:.2f} ms"
                          f" | DAQ window {plan.acq_time_s * 1e3:.2f} ms | Hall live: {plan.hall_live}")
        self._writer.set_metadata(t_point_s=plan.t_point_s, field_settle_s=plan.settle_s,
                                  acq_time_s=plan.acq_time_s, hall_live=plan.hall_live,
                                  actual_step_V=plan.actual_step, n_forward=len(plan.forward),
                                  n_backward=len(plan.backward))
        if plan.hall_live:
            self._prepare_hall(plan.hall_integration_s)
        self._initial_field(p["b_min"])
        x_um, y_um = math.nan, math.nan
        if p.get("position_stage"):
            x_um, y_um = self._move_xy_um(p["x"], p["y"])
        if lockin:
            self._configure_lockin()
        self._writer.set_metadata(position_x_um=x_um, position_y_um=y_um)

        n = plan.n_total
        sums = {k: np.zeros(n) for k in ("dc_signal", "intensity", "hall_field", "lockin_x", "lockin_r",
                                          "actuator_coil")}
        counts = {k: np.zeros(n) for k in sums}
        completed = 0
        for s in range(n_sweeps):
            self._gate()
            self._log("info", f"Sweep {s + 1}/{n_sweeps}")
            op = self._rpc(lambda: self._dev("moke").start_operation(
                "field_sweep", setpoints_V=[float(v) for v in plan.setpoints],
                branch=[int(b) for b in plan.branch], t_point_s=plan.t_point_s, settle_s=plan.settle_s,
                acq_time_s=plan.acq_time_s, hall=plan.hall_live, lockin=lockin), "start sweep")
            sweep_vals: dict[str, np.ndarray] = {k: np.full(n, np.nan) for k in sums}

            def on_record(rec, s=s, sweep_vals=sweep_vals):
                i = int(rec["index"])
                vals = self._sensor_values(rec)
                vals.update({"actuator_coil_setpoint": rec["setpoint_V"], "actuator_coil": rec["coil_V"],
                             "cycle": s, "branch": rec["branch"]})
                self._write((s, i), vals)
                for k in sums:
                    sweep_vals[k][i] = vals.get(k, np.nan)
                self.points_done += 1
                self._emit({"kind": "point", "workflow": self.wf.key, "sweep": s, "index": i,
                            "flat_index": s * n + i, "branch": rec["branch"], "x": rec["coil_V"],
                            "values": vals, "record": rec})

            self._consume(op, on_record)
            for k in sums:
                ok = np.isfinite(sweep_vals[k])
                sums[k][ok] += sweep_vals[k][ok]
                counts[k][ok] += 1
            completed += 1
            self._writer.checkpoint()
        # Averaged loop (forward and backward kept apart by index).
        avg = {k: np.where(counts[k] > 0, sums[k] / np.maximum(counts[k], 1), np.nan) for k in sums}
        for i in range(n):
            vals = {"actuator_coil_setpoint": float(plan.setpoints[i]), "actuator_coil": avg["actuator_coil"][i],
                    "branch": int(plan.branch[i]), "n_sweeps": int(counts["dc_signal"][i])}
            for k in ("dc_signal", "intensity", "hall_field", "lockin_x", "lockin_r"):
                vals[k] = avg[k][i]
            self._write((i,), vals, "data/averaged", count=False)
        self._emit({"kind": "average", "workflow": self.wf.key, "x": plan.setpoints.tolist(),
                    "values": {k: v.tolist() for k, v in avg.items()}, "branch": plan.branch.tolist()})
        self._writer.set_metadata(sweeps_completed=completed)
        self._writer.checkpoint()

    def _run_position(self) -> None:
        p = self.params
        if self.wf.key == "xy_sweep":
            pts, xs, ys = self._position_points()
        else:
            pts, values = self._position_points()
        self._writer.set_metadata(field_settle_s=p["field_settle_s"])
        record_hall = bool(p["record_hall"])
        if record_hall:
            self._prepare_hall(plans.hall_integration_for(p["acq_time"]))
        self._initial_field(p["b"])
        first = pts[0]
        self._move_xy_um(first.x_um, first.y_um)
        self._configure_lockin()
        stage = self._dev(f"stage_{self.req.setup}")
        last_x, last_y = first.x_um, first.y_um
        last_coil = p["b"]
        read_x, read_y = self._last_xy_um
        n_cols = len(xs) if self.wf.key == "xy_sweep" else 0
        k = 0
        while k < len(pts):
            pt = pts[k]
            self._gate()
            try:
                moved = False
                if pt.x_um != last_x:
                    r = self._rpc(lambda: stage.command("move_to", axis="x", position_mm=pt.x_um / UM_PER_MM),
                                  "move x")
                    read_x, last_x, moved = r["position_mm"] * UM_PER_MM, pt.x_um, True
                if pt.y_um != last_y:
                    r = self._rpc(lambda: stage.command("move_to", axis="y", position_mm=pt.y_um / UM_PER_MM),
                                  "move y")
                    read_y, last_y, moved = r["position_mm"] * UM_PER_MM, pt.y_um, True
                if moved and p["settle_time"] > 0:
                    self._sleep(p["settle_time"])
                settle = p["field_settle_s"] if pt.coil_V != last_coil else 0.0
                token = f"{k}"
                rec = self._rpc(lambda: self._dev("moke").command(
                    "measure_point", coil_V=pt.coil_V, acq_time_s=p["acq_time"], settle_s=settle,
                    hall=record_hall, lockin=True, lockin_after="acquisition", token=token), "measure")
                if rec.get("token") != token:
                    raise ServiceError(ErrorCode.INTERNAL, "stale point rejected (token mismatch)")
                last_coil = pt.coil_V
            except ServiceError as exc:
                if exc.code == ErrorCode.CANCELLED or self._abort.is_set():
                    self._check()
                    raise
                if exc.code in (ErrorCode.OUT_OF_RANGE, ErrorCode.INVALID_VALUE):
                    raise
                # Positions after a failed move are unknown: re-command absolute targets.
                last_x = last_y = math.nan
                self._auto_pause(f"Hardware error at point {k + 1}/{len(pts)}: {exc.message} - PAUSED. "
                                 "Fix the cause, then Resume to retry this point, or Abort.")
                continue
            vals = self._sensor_values(rec)
            vals["coil_setpoint"] = pt.coil_V
            if self.wf.key == "xy_sweep":
                group = "data" if pt.rep == 0 else f"data/repeat_{pt.rep + 1:03d}"
                vals.update({"actuator_x_setpoint": pt.x_um, "actuator_x": read_x,
                             "actuator_y_setpoint": pt.y_um, "actuator_y": read_y})
                self._write((pt.iy, pt.ix), vals, group)
                if pt.iy in (0, len(ys) - 1):
                    self._writer.checkpoint()
            else:
                ax = "x" if self.wf.key == "x_sweep" else "y"
                other = "y" if ax == "x" else "x"
                vals.update({f"actuator_{ax}_setpoint": pt.x_um if ax == "x" else pt.y_um,
                             f"actuator_{ax}": read_x if ax == "x" else read_y,
                             f"position_{other}": read_y if ax == "x" else read_x,
                             "loop": pt.loop, "iteration": pt.iteration})
                self._write((pt.loop, pt.iteration), vals)
            self.points_done += 1
            self._emit({"kind": "point", "workflow": self.wf.key, "index": k, "ix": pt.ix, "iy": pt.iy,
                        "rep": pt.rep, "loop": pt.loop, "x": read_x if self.wf.key != "y_sweep" else read_y,
                        "x_um": read_x, "y_um": read_y, "coil_V": pt.coil_V, "values": vals, "record": rec,
                        "n_cols": n_cols})
            k += 1

    def _run_time(self) -> None:
        p = self.params
        n = plans.time_points(p["duration"], p["interval"])
        record_hall = bool(p["record_hall"])
        if record_hall:
            self._prepare_hall(plans.hall_integration_for(p["acq_time"]))
        self._initial_field(p["b"])
        x_um = y_um = math.nan
        if p.get("position_stage"):
            x_um, y_um = self._move_xy_um(p["x"], p["y"])
        self._configure_lockin()
        self._writer.set_metadata(position_x_um=x_um, position_y_um=y_um)
        op = self._rpc(lambda: self._dev("moke").start_operation(
            "time_trace", coil_V=p["b"], n_points=n, interval_s=p["interval"], acq_time_s=p["acq_time"],
            hall=record_hall, lockin=True), "start time trace")

        def on_record(rec):
            k = int(rec["index"])
            vals = self._sensor_values(rec)
            vals["time"] = rec["elapsed_s"]
            vals["coil_setpoint"] = p["b"]
            self._write((k,), vals)
            self.points_done += 1
            self._emit({"kind": "point", "workflow": self.wf.key, "index": k, "x": rec["elapsed_s"],
                        "values": vals, "record": rec})
            if k % 10 == 9:
                self._writer.checkpoint()
        self._consume(op, on_record)
