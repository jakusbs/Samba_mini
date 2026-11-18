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
from core.scan.channels import (AVERAGED, BOOKKEEPING, RESERVED, RESERVED_PREFIXES, Channel, dataset_key,
                                sensors_for)
from core.scan.workflows import RESUMABLE_AFTER_SERVICE_STOP, WORKFLOWS, Workflow
from core.storage.hdf5_writer import DatasetSpec, ScanWriter, StorageError, make_filename
from core.version import describe, software_revision

log = logging.getLogger(__name__)

IDLE, PREPARING, RUNNING, PAUSED, STOPPING, FINISHED = (
    "IDLE", "PREPARING", "RUNNING", "PAUSED", "STOPPING", "FINISHED")

UM_PER_MM = 1000.0
INITIAL_FIELD_WAIT_S = 1.0          # MiniMoke: "Setting magnetic field ..., waiting 1 s"
BACKEND_LOST_AFTER = 3              # consecutive RPC timeouts
BEGIN_WAIT_S = 30.0                 # wait this long for a manual command (e.g. a jog) to finish before starting
#: Logical endpoints and the instrument behind their failures when an older service does not
#: name the physical device: the acquisition (and the coil) is the DAQ.
_INSTRUMENT_OF = {"moke": "daq", "magnet": "daq"}


def _service_silent(exc: ServiceError) -> bool:
    """The service itself did not answer (the client's timeout), as opposed to an instrument."""
    return exc.code == ErrorCode.TIMEOUT and "did not answer" in exc.message


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
    #: The calibration record these values come from, frozen at the start of the run (id,
    #: version, setup, sample, creator, mode, source, ...) -> /metadata bd_calibration_*; the
    #: live display uses the same snapshot.  None = no calibration active.
    bd_calibration_record: dict | None = None
    #: The sensors to measure and save, as Samba's sensor list: ``[{"id": "dc_signal", "name": "BD",
    #: "y_axis": "Y1", ...}]``.  Each is saved under its name (``/data/<name>``) with its id as an
    #: attribute; the Hall probe and the lock-in are read only when one of their channels is in it.
    #: None (scripts, tests): every sensor of the workflow under its id (the earlier layout).
    channels: list | None = None


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
        self._began = threading.Event()       # run.begin accepted: from here on the run may move the stage
        self._thread: threading.Thread | None = None
        self._op: OperationHandle | None = None
        self._writer: ScanWriter | None = None
        self._pending_writes: list[tuple] = []
        self._rpc_failures = 0
        self._reconnect: str | None = None      # instrument to reconnect when the run resumes
        self._service_lost = False              # paused because the device service went away
        self._instance: str | None = None       # the service instance the run began on
        self._required: list[str] = []          # devices declared at run.begin
        self.service_restarts = 0
        self.result: dict | None = None
        self.path: Path | None = None
        self.points_done = 0
        self.points_total = 0
        self._t_start = 0.0
        self._last_xy_um: tuple[float, float] = (math.nan, math.nan)
        self.mode = "unknown"
        self._lockin_settings: dict | None = None     # the lock-in as it is set (read, never changed)
        self._coil_before = math.nan                 # coil command before the run (field sweeps return to it)
        self._coil_after_run = "keep"
        self._resolve_channels()

    # ================================================================ channels
    def _resolve_channels(self) -> None:
        """The sensors this run records: ``self.sel`` = [{ch, ds (dataset name), label, unit, ...}],
        plus whether the Hall probe and the lock-in are read at all."""
        avail = {c.name: c for c in sensors_for(self.wf.uses_lockin)}
        self.sel: list[dict] = []
        self.skipped_channels: list[str] = []
        if self.req.channels is None:
            for c in avail.values():
                self.sel.append({"ch": c, "ds": c.name, "label": c.label, "unit": c.unit, "spec": {}})
            self.read_hall = bool(self.params.get("record_hall", True))
            self.read_lockin = self.wf.uses_lockin
            return
        used: set[str] = set()
        for spec in self.req.channels:
            spec = dict(spec or {})
            c = avail.get(spec.get("id"))
            if c is None:
                self.skipped_channels.append(str(spec.get("name") or spec.get("id")))
                continue
            if any(x["ch"].name == c.name for x in self.sel):
                continue
            label = str(spec.get("name") or c.label)
            base = ds = dataset_key(label)
            n = 2
            while ds.lower() in used or ds.lower() in RESERVED or ds.lower().startswith(RESERVED_PREFIXES):
                ds = f"{base}_{n}"
                n += 1
            used.add(ds.lower())
            self.sel.append({"ch": c, "ds": ds, "label": label, "unit": c.unit, "spec": spec})
        self.read_hall = any(x["ch"].device == "hall" for x in self.sel)
        self.read_lockin = self.wf.uses_lockin and any(x["ch"].lockin for x in self.sel)

    def _channel_attrs(self, x: dict) -> dict:
        spec = x["spec"]
        return {"channel": x["ch"].name, "device": x["ch"].device, "y_axis": spec.get("y_axis", ""),
                "line": spec.get("line", "")}

    def channels_record(self) -> list[dict]:
        return [{"id": x["ch"].name, "name": x["label"], "dataset": x["ds"], "unit": x["unit"],
                 "device": x["ch"].device, "line": x["spec"].get("line", ""),
                 "y_axis": x["spec"].get("y_axis", "")} for x in self.sel]

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
        # Before run.begin the run has not moved the stage, and a stop sent then would only make
        # run.begin wait for it; the engine thread checks the abort flag before its first move.
        if self._began.is_set() and (self.wf.uses_stage == "required" or self.params.get("position_stage")):
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
        self.message = message                  # first: whoever sees the new state sees its message
        self.state = state
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
        """Between points: hold while paused; raise on abort.  After a hardware error, Resume
        first reconnects the instrument that failed (see ``_hardware_pause``)."""
        self._check()
        if not self._pause.is_set():
            return
        while True:
            self._set_state(PAUSED, self.message or "Paused")
            while self._pause.is_set():
                self._check()
                self._abort.wait(0.1)
            self._check()
            if self._service_lost and not self._reattach():
                continue
            if self._reconnect is None or self._reconnect_now():
                break
        self._set_state(RUNNING, "")

    def _auto_pause(self, msg: str) -> None:
        self._log("error", msg)
        self._pause.set()
        self._set_state(PAUSED, msg)

    def _hardware_pause(self, where: str, exc: ServiceError, called: str) -> None:
        """Pause on a hardware error and remember the instrument to reconnect on Resume: the one
        the service names (``details["device"]``), else the device that was called.  Retrying
        on the same connection does not help a stuck instrument (e.g. a DAQ task that never
        finishes, -200560), and a reconnect writes no outputs: coil, lock-in settings and stage
        stay as they are."""
        name = exc.details.get("device") or called
        name = _INSTRUMENT_OF.get(name, name)
        self._reconnect = name if exc.code != ErrorCode.BUSY else None
        then = (f"Resume to reconnect {name} and retry this point" if self._reconnect
                else "Resume to retry this point")
        self._auto_pause(f"Hardware error at {where}: {exc.message} - PAUSED. Fix the cause, then {then}, or Abort.")

    def _service_gone_or_restarted(self) -> bool:
        """After losing the service, the session or control: True if the service is gone or is a
        new instance (stopped and started again).  Then the scan can wait and go on: a service
        that stops during a map scan leaves coil and lock-in output on.  False if the same
        service took control away (lease expired, forced release): its cleanup switched the
        outputs off, so the scan must not simply go on."""
        base = getattr(self.client, "_client", self.client)
        try:
            info = base.hello(2.0)
        except ServiceError:
            return True
        return bool(self._instance) and info.get("instance_id") != self._instance

    def _may_wait_for_service(self) -> bool:
        """A lost service may be waited for: a map scan, not stopped by the operator or a forced
        release, and the service is gone or was restarted (see ``_service_gone_or_restarted``)."""
        return (not self._abort.is_set() and not self._lease_lost.is_set()
                and self.wf.key in RESUMABLE_AFTER_SERVICE_STOP and self._service_gone_or_restarted())

    def _service_pause(self, where: str, reason: str) -> None:
        self._service_lost = True
        self._reconnect = None              # a restarted service connects every instrument anew
        self._auto_pause(f"Lost the device service at {where} ({reason}) - PAUSED. Coil and lock-in output "
                         "stay as they are. Start the service again, then Resume to continue the scan "
                         "from this point, or Abort.")

    def _reattach(self) -> bool:
        """Resume after losing the service: on the same service (it was only slow) go on; on a
        restarted one take control again, begin a new run with the same devices (reconnecting
        any the service could not connect at its start) and go on.  False (and paused again) if
        that is not possible yet."""
        base = getattr(self.client, "_client", self.client)
        self._set_state(RUNNING, "Connecting to the device service")
        try:
            info = base.hello(3.0)
            if info.get("instance_id") == self._instance:
                self._service_lost = False
                self._rpc_failures = 0
                self._log("info", "The device service answers again; the scan continues.")
                return True
            if not base.session:
                raise ServiceError(ErrorCode.AUTH_REQUIRED, "not logged in any more: Abort, log in and "
                                   "measure the rest")
            base.acquire_lease()
            self.client = base
            try:
                run = self._begin_run(self._required)
            except ServiceError as exc:
                missing = [str(m).split(":")[0] for m in exc.details.get("missing", [])]
                if exc.code != ErrorCode.DEVICE_UNAVAILABLE or not missing:
                    raise
                for name in missing:
                    self._log("info", f"Reconnecting {name} (not available on the restarted service).")
                    self._rpc(lambda n=name: base.device(n).reconnect(), f"reconnect {name}")
                run = self._begin_run(self._required)
            self.client = base.for_run(run["run_id"])
        except (ServiceError, Interrupted) as exc:
            msg = exc.message if isinstance(exc, ServiceError) else str(exc)
            self._auto_pause(f"Device service not ready ({msg}) - PAUSED. Start it, then Resume, or Abort.")
            return False
        self._instance = info.get("instance_id")
        self._service_lost = False
        self._rpc_failures = 0
        self.service_restarts += 1
        if self._writer:
            self._writer.set_metadata(service_restarts=self.service_restarts)
        if self.read_hall:
            try:
                self._prepare_hall(plans.hall_integration_for(self.params["acq_time"]))
            except (ServiceError, Interrupted) as exc:
                self._log("warning", f"Hall integration not set again after the restart ({exc}); the Hall "
                                     "probe averages with its default from now on.")
        self._log("info", "Back on the restarted device service with control of the setup; the scan "
                          "continues.")
        return True

    def _reconnect_now(self) -> bool:
        """Reconnect the instrument a hardware error came from; False (and paused again) if
        that failed."""
        name = self._reconnect
        self._set_state(RUNNING, f"Reconnecting {name}")
        self._log("info", f"Reconnecting {name} before retrying the point.")
        try:
            self._rpc(lambda: self._dev(name).reconnect(), f"reconnect {name}")
        except Interrupted as exc:
            if not self._may_wait_for_service():
                raise
            self._service_pause(f"reconnecting {name}", str(exc))
            return False
        except ServiceError as exc:
            if exc.code == ErrorCode.CANCELLED or self._abort.is_set():
                self._check()
                raise
            if _service_silent(exc) and self._may_wait_for_service():
                self._service_pause(f"reconnecting {name}", exc.message)
                return False
            self._auto_pause(f"Reconnecting {name} failed: {exc.message} - PAUSED. Fix the cause, then "
                             "Resume to try again, or Abort.")
            return False
        self._reconnect = None
        self._log("info", f"{name} reconnected.")
        return True

    def _rpc(self, fn: Callable[[], Any], what: str) -> Any:
        """Run a service call; consecutive timeouts mean the service is gone."""
        try:
            result = fn()
        except ServiceError as exc:
            if exc.details.get("service_stopping"):
                raise Interrupted("the device service stopped") from exc
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

    def _begin_run(self, required: list[str]) -> dict:
        """run.begin, waiting (up to BEGIN_WAIT_S, abortable) while the service is still finishing
        a device command — e.g. a jog the operator clicked just before Start."""
        deadline = time.monotonic() + BEGIN_WAIT_S
        told = False
        while True:
            self._check()
            try:
                return self._rpc(lambda: self.client.call("run.begin", {"required_devices": required,
                                                                     "workflow": self.wf.key}), "run.begin")
            except ServiceError as exc:
                if (exc.code != ErrorCode.BUSY or not exc.details.get("inflight")
                        or time.monotonic() >= deadline):
                    raise
                if not told:
                    self._log("info", f"Waiting for a device command to finish before starting ({exc.message}).")
                    told = True
            self._abort.wait(0.1)

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
            self._instance = hello.get("instance_id")
            self._validate()
            if not self.client.lease_holder:
                self.client.acquire_lease()
            required = self.wf.required_devices(self.params, self.req.setup)
            if not self.read_hall and "hall" in required:
                required.remove("hall")                # no Hall channel ticked: the probe is not used
            if not self.sel:
                raise ServiceError(ErrorCode.INVALID_VALUE, "no channel to measure: tick at least one")
            if self.skipped_channels:
                self._log("warning", "not measured by this measurement: " + ", ".join(self.skipped_channels))
            if not self.read_lockin and "lockin" in required:
                required.remove("lockin")              # no lock-in channel ticked: the lock-in is not used
            self._read_before_run()
            self._required = list(required)
            run = self._begin_run(required)
            self.client = self.client.for_run(run["run_id"])
            began = True
            self._began.set()
            self._check()                       # aborted while run.begin was on its way
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
            if self._abort.is_set() and not self._lease_lost.is_set():
                # The operator's abort raced a service call (e.g. the stage stop it sent was still
                # in flight): the run was aborted, not failed.  Keep the error in the log.
                status, message = "aborted", "aborted by the operator"
                self._log("warning", f"after the abort: {exc.code}: {exc.message}")
            else:
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
            except (Abort, Interrupted):
                self._log("info", "Not returning to the start position: the run was stopped.")
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"could not return to the start position: {exc}")
            coil_problems = self._coil_after()
            try:
                self.client.hello(3.0)
                res = self.client.call("run.end", {"cleanup": True}, 60.0)
                cleanup_problems = coil_problems + res.get("cleanup_problems", [])
            except ServiceError as exc:
                cleanup_problems = coil_problems + [f"cleanup not confirmed: {exc.message}"]
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
        specs: list[DatasetSpec] = []

        def add_common(group="data", shape=None):
            for x in self.sel:
                specs.append(DatasetSpec(x["ds"], "sensor", x["label"], x["unit"], group, "f8", shape,
                                         attrs=self._channel_attrs(x)))
            for ch in BOOKKEEPING:
                if ch.name == "hall_integration" and not self.read_hall:
                    continue
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
            for x in self.sel:
                if x["ch"].name in AVERAGED:
                    specs.append(DatasetSpec(x["ds"], "sensor", f"{x['label']} (mean)", x["unit"],
                                             "data/averaged", shape=n, attrs=self._channel_attrs(x)))
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
            "channels_json": self.channels_record(),
            "lockin_settings_json": (dict(self._lockin_settings, note="read at run start; Samba_mini "
                                          "measurements do not change the lock-in")
                                     if self._lockin_settings is not None else {"used": False}),
            "coil_before_run_V": self._coil_before,
        }
        rec = self.req.bd_calibration_record
        cal_vals = [float(v) for v in (self.req.bd_calibration or [])]
        used = len(cal_vals) == 6 and any(v != 0.0 for v in cal_vals)
        metadata["bd_calibration_id"] = (rec or {}).get("id") or "" if used else ""
        metadata["bd_calibration_version"] = int((rec or {}).get("version") or 0) if used else 0
        metadata["bd_calibration_json"] = (dict(rec, values=cal_vals) if (rec and used) else
                                           {"active": False} if not used else
                                           {"values": cal_vals, "id": None,
                                            "note": "values without a calibration record"})
        for k, v in (self.req.extra_metadata or {}).items():
            metadata.setdefault(str(k), v)            # never overrides the engine's own keys
        root = dict(root, scan_type=self.wf.scan_type, mode=self.mode)
        self._writer = ScanWriter(self.path, shape, specs, root, metadata)
        self._writer.create()
        cal = [float(v) for v in (self.req.bd_calibration or [])]
        if len(cal) == 6 and any(v != 0.0 for v in cal):
            rec = self.req.bd_calibration_record or {}
            self._writer.add_static("calibration", cal, unit="mV", role="calibration",
                                    label="λ/2 calibration (ticks 0, 5, 10, 15, 20, 25)",
                                    attrs={"calibration_id": rec.get("id") or "",
                                           "calibration_version": int(rec.get("version") or 0)})
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
    def _field_plan(self, applied_time_constant_s: float | None = None):
        p = self.params
        if self.wf.key == "b_sweep_dc":
            return plans.b_sweep_dc_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"])
        return plans.b_sweep_lockin_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"],
                                         (p["time_const"] if applied_time_constant_s is None
                                          else applied_time_constant_s), p["acq_time"])

    def _position_points(self):
        p = self.params
        if self.wf.key == "x_sweep":
            return plans.x_sweep_points(p["x_min"], p["x_max"], p["x_step"], p["y"], p["b"], p["repeat_num"])
        if self.wf.key == "y_sweep":
            return plans.y_sweep_points(p["y_min"], p["y_max"], p["y_step"], p["x"], p["b"], p["repeat_num"])
        return plans.xy_points(p["x_min"], p["x_max"], p["x_step"], p["y_min"], p["y_max"], p["y_step"],
                               p["b"], p["repeat_num"])

    def _initial_field(self, coil_V: float, wait: bool = True) -> None:
        """Set the coil command a run starts with.  Field sweeps wait 1 s afterwards (MiniMoke);
        spatial and time scans wait their Settle instead (``wait=False``).  The coil keeps running
        between measurements, so it is set whenever it differs from the value before the run --
        also when the run starts at 0 V."""
        before = self._coil_before
        if coil_V == 0 and (before == 0 or not math.isfinite(before)):
            return                                   # already off (or unknown: MiniMoke left it)
        if coil_V == before:
            return
        if wait:
            self._log("info", f"Setting coil command to {coil_V:+.4g} V, waiting {INITIAL_FIELD_WAIT_S:g} s")
        else:
            self._log("info", f"Setting coil command to {coil_V:+.4g} V")
        self._rpc(lambda: self._dev("daq").command("set_outputs", coil_V=coil_V), "set coil")
        if wait:
            self._sleep(INITIAL_FIELD_WAIT_S)

    def _move_xy_um(self, x_um: float, y_um: float) -> tuple[float, float]:
        self._check()                           # never start a move after the operator aborted
        stage = self._dev(f"stage_{self.req.setup}")
        self._log("info", f"Moving {self.req.setup} stage to ({x_um:g} um, {y_um:g} um)")
        res = self._rpc(lambda: stage.command("move_xy", x_mm=x_um / UM_PER_MM, y_mm=y_um / UM_PER_MM),
                        "stage move")
        self._last_xy_um = (res["x_mm"] * UM_PER_MM, res["y_mm"] * UM_PER_MM)
        return self._last_xy_um

    #: lock-in settings read at run start, and the workflow parameter each corresponds to
    LOCKIN_SETTINGS = {"oscillator_V": "volt", "sensitivity_V": "sensi", "frequency_Hz": "lockin_freq",
                       "time_constant_s": "time_const", "phase_deg": "phase", "reference_mode": None}

    def _read_before_run(self) -> None:
        """Before the run claims its devices (afterwards their reads come from the run's cache):
        how the lock-in is set -- measurements never change it, only "Apply now" does -- and the
        coil command a field sweep returns to."""
        self._check()                              # aborted before the run began: touch nothing
        if self.client.has_feature("coil-keep-1"):
            try:
                snap = self.client.call("config.get", {}, 10.0).get("data") or {}
                self._coil_after_run = str((snap.get("cleanup") or {}).get("coil_after_run", "keep"))
            except ServiceError:
                self._coil_after_run = "keep"
        else:
            self._coil_after_run = "zero"              # an older service zeroes the coil at run end
        try:
            r = self.client.device("daq").read("coil_command_V", timeout_s=10.0)["coil_command_V"]
            self._coil_before = float(r.value) if r.valid and r.value is not None else math.nan
        except (ServiceError, TypeError, ValueError):
            self._coil_before = math.nan
        if not self.read_lockin:
            self._lockin_settings = None
            return
        try:
            got = self.client.device("lockin").read(*self.LOCKIN_SETTINGS, timeout_s=15.0)
        except ServiceError as exc:
            raise ServiceError(exc.code, f"lock-in settings could not be read: {exc.message}") from None
        self._lockin_settings = {k: (r.value if r.valid else None) for k, r in got.items()}
        p = self.params
        differ = []
        for key, param in self.LOCKIN_SETTINGS.items():
            v = self._lockin_settings.get(key)
            if param and param in p and isinstance(v, (int, float)) and not math.isclose(
                    float(v), float(p[param]), rel_tol=0.02, abs_tol=1e-12):
                differ.append(f"{key} {v:g} (Hardware box {p[param]:g})")
        if differ:
            self._log("warning", "The lock-in is measured as it is set now (runs do not change it): "
                      + ", ".join(differ) + ". Use Apply now in the Hardware box to change it.")

    def _lockin_time_constant(self) -> float | None:
        tc = (self._lockin_settings or {}).get("time_constant_s")
        return float(tc) if isinstance(tc, (int, float)) and tc > 0 else None

    def _coil_after(self) -> list[str]:
        """The coil keeps running after a measurement ("keep", the default): a field sweep returns to
        the value it started from, other measurements leave their coil command (the Hardware box
        value).  With "zero" the service sets 0 V in run.end."""
        if self._coil_after_run != "keep":
            return []
        if self.wf.key in ("b_sweep_dc", "b_sweep_lockin"):
            target = self._coil_before if math.isfinite(self._coil_before) else 0.0
        else:
            target = float(self.params.get("b", 0.0))
        try:
            self._rpc(lambda: self._dev("daq").command("set_outputs", coil_V=target), "coil after run")
        except (ServiceError, Abort, Interrupted) as exc:
            return [f"coil not set to {target:+.4g} V after the run: {getattr(exc, 'message', exc)}"]
        self._log("info", f"Coil command left at {target:+.4g} V (it keeps running; Zero outputs switches "
                  "it off)")
        if self._writer:
            try:
                self._writer.set_metadata(coil_after_run_V=target)
            except StorageError:
                pass
        return []

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

    def _sensor_values(self, rec: dict) -> tuple[dict, dict]:
        """(values by dataset name for the file, values by channel id for the live display)."""
        by_id = {}
        for x in self.sel:
            v = rec.get(x["ch"].key, math.nan)
            by_id[x["ch"].name] = math.nan if v is None else v
        vals = {x["ds"]: by_id[x["ch"].name] for x in self.sel}
        for ch in BOOKKEEPING:
            if ch.name == "hall_integration" and not self.read_hall:
                continue
            v = rec.get(ch.key, math.nan)
            vals[ch.name] = math.nan if v is None else v
        t = rec.get("t_trigger", math.nan) - self._t_start if rec.get("t_trigger") else math.nan
        vals["time"] = by_id["time"] = t
        return vals, by_id

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
                elif st == "RUNNING" and self.state == PAUSED and not self._pause.is_set():
                    # Only once the pause has been lifted (resume): right after the engine paused
                    # itself (e.g. the disk), the service may still report RUNNING for one poll --
                    # that is not a resume (it lost the reason and paused again as "Paused").
                    if self._pending_writes:
                        self._flush_pending()
                    if not self._pending_writes:
                        self._pause.clear()
                        self._set_state(RUNNING, "")
                if op.done and res["last_seq"] >= res["total_records"]:
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
        tc = self._lockin_time_constant() if lockin else None
        if tc is not None:
            msg = plans.b_sweep_lockin_validation(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"], tc,
                                                  p["acq_time"])
            if msg:
                raise ServiceError(ErrorCode.INVALID_VALUE, f"with the lock-in's time constant of {tc:g} s: {msg}")
        plan = self._field_plan(tc)
        n_sweeps = int(p["num_sweeps"])
        self._log("info", f"Points per sweep: {plan.n_total} ({len(plan.forward)} fwd + {len(plan.backward)} bwd)"
                          f" | T_point {plan.t_point_s * 1e3:.2f} ms | settle {plan.settle_s * 1e3:.2f} ms"
                          f" | DAQ window {plan.acq_time_s * 1e3:.2f} ms | Hall live: {plan.hall_live}")
        self._writer.set_metadata(t_point_s=plan.t_point_s, field_settle_s=plan.settle_s,
                                  acq_time_s=plan.acq_time_s, hall_live=plan.hall_live,
                                  actual_step_V=plan.actual_step, n_forward=len(plan.forward),
                                  n_backward=len(plan.backward))
        hall = plan.hall_live and self.read_hall
        if hall:
            self._prepare_hall(plan.hall_integration_s)
        self._initial_field(p["b_min"])
        x_um, y_um = math.nan, math.nan
        if p.get("position_stage"):
            x_um, y_um = self._move_xy_um(p["x"], p["y"])
        self._writer.set_metadata(position_x_um=x_um, position_y_um=y_um)

        n = plan.n_total
        avg_ids = [x["ch"].name for x in self.sel if x["ch"].name in AVERAGED]
        ds_of = {x["ch"].name: x["ds"] for x in self.sel}
        sums = {k: np.zeros(n) for k in avg_ids + ["actuator_coil"]}
        counts = {k: np.zeros(n) for k in sums}
        completed = 0
        for s in range(n_sweeps):
            self._gate()
            self._log("info", f"Sweep {s + 1}/{n_sweeps}")
            op = self._rpc(lambda: self._dev("moke").start_operation(
                "field_sweep", setpoints_V=[float(v) for v in plan.setpoints],
                branch=[int(b) for b in plan.branch], t_point_s=plan.t_point_s, settle_s=plan.settle_s,
                acq_time_s=plan.acq_time_s, hall=hall, lockin=lockin and self.read_lockin), "start sweep")
            sweep_vals: dict[str, np.ndarray] = {k: np.full(n, np.nan) for k in sums}

            def on_record(rec, s=s, sweep_vals=sweep_vals):
                i = int(rec["index"])
                vals, by_id = self._sensor_values(rec)
                vals.update({"actuator_coil_setpoint": rec["setpoint_V"], "actuator_coil": rec["coil_V"],
                             "cycle": s, "branch": rec["branch"]})
                by_id["actuator_coil"] = rec["coil_V"]
                self._write((s, i), vals)
                for k in sums:
                    sweep_vals[k][i] = by_id.get(k, np.nan)
                self.points_done += 1
                self._emit({"kind": "point", "workflow": self.wf.key, "sweep": s, "index": i,
                            "flat_index": s * n + i, "branch": rec["branch"], "x": rec["coil_V"],
                            "values": by_id, "record": rec})

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
                    "branch": int(plan.branch[i]),
                    "n_sweeps": int(counts[avg_ids[0] if avg_ids else "actuator_coil"][i])}
            for k in avg_ids:
                vals[ds_of[k]] = avg[k][i]
            self._write((i,), vals, "data/averaged", count=False)
        self._emit({"kind": "average", "workflow": self.wf.key, "x": plan.setpoints.tolist(),
                    "values": {k: v.tolist() for k, v in avg.items()}, "branch": plan.branch.tolist()})
        self._writer.set_metadata(sweeps_completed=completed)
        self._writer.checkpoint()

    def _run_position(self) -> None:
        p = self.params
        if self.wf.key == "xy_sweep":
            pts, xs, ys = self._position_points()
            # Point order matters for drift: maps before this ran Y serpentine (up, down, up, ...).
            self._writer.set_metadata(scan_order="columns, Y fast from negative to positive (no zigzag), "
                                                 "X stepped from x_min to x_max")
        else:
            pts, values = self._position_points()
        record_hall = self.read_hall
        if record_hall:
            self._prepare_hall(plans.hall_integration_for(p["acq_time"]))
        self._initial_field(p["b"], wait=False)
        first = pts[0]
        self._move_xy_um(first.x_um, first.y_um)
        stage = self._dev(f"stage_{self.req.setup}")
        last_x, last_y = first.x_um, first.y_um
        last_coil = p["b"]
        pending_settle = True                    # the move to the first point (and the coil just set)
        read_x, read_y = self._last_xy_um
        n_cols = len(xs) if self.wf.key == "xy_sweep" else 0
        k = 0
        while k < len(pts):
            pt = pts[k]
            self._gate()
            if stage.client is not self.client:      # a new run after a service restart
                stage = self._dev(f"stage_{self.req.setup}")
            called = stage.name
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
                # One Settle after anything changed: a stage move or the coil command (hardware
                # timed on the DAQ, between setting the coil and acquiring).
                changed = pending_settle or moved or pt.coil_V != last_coil
                settle = p["settle_time"] if changed else 0.0
                token = f"{k}"
                called = "moke"
                rec = self._rpc(lambda: self._dev("moke").command(
                    "measure_point", coil_V=pt.coil_V, acq_time_s=p["acq_time"], settle_s=settle,
                    hall=record_hall, lockin=self.read_lockin, lockin_after="acquisition", token=token),
                    "measure")
                if rec.get("token") != token:
                    raise ServiceError(ErrorCode.INTERNAL, "stale point rejected (token mismatch)")
                last_coil = pt.coil_V
                pending_settle = False
            except Interrupted as exc:
                if not self._may_wait_for_service():
                    raise
                last_x = last_y = math.nan          # re-command absolute targets after the restart
                pending_settle = True
                self._service_pause(f"point {k + 1}/{len(pts)}", str(exc))
                continue
            except ServiceError as exc:
                if exc.code == ErrorCode.CANCELLED or self._abort.is_set():
                    self._check()
                    raise
                if exc.code in (ErrorCode.OUT_OF_RANGE, ErrorCode.INVALID_VALUE):
                    raise
                # Positions after a failed move are unknown: re-command absolute targets.
                last_x = last_y = math.nan
                pending_settle = True
                if _service_silent(exc) and self._may_wait_for_service():
                    self._service_pause(f"point {k + 1}/{len(pts)}", exc.message)
                else:
                    self._hardware_pause(f"point {k + 1}/{len(pts)}", exc, called)
                continue
            vals, by_id = self._sensor_values(rec)
            vals["coil_setpoint"] = pt.coil_V
            if self.wf.key == "xy_sweep":
                group = "data" if pt.rep == 0 else f"data/repeat_{pt.rep + 1:03d}"
                vals.update({"actuator_x_setpoint": pt.x_um, "actuator_x": read_x,
                             "actuator_y_setpoint": pt.y_um, "actuator_y": read_y})
                self._write((pt.iy, pt.ix), vals, group)
                if pt.iy == len(ys) - 1:                 # a column is complete
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
                        "x_um": read_x, "y_um": read_y, "coil_V": pt.coil_V, "values": by_id, "record": rec,
                        "n_cols": n_cols})
            k += 1

    def _run_time(self) -> None:
        p = self.params
        n = plans.time_points(p["duration"], p["interval"])
        record_hall = self.read_hall
        if record_hall:
            self._prepare_hall(plans.hall_integration_for(p["acq_time"]))
        self._initial_field(p["b"], wait=False)
        x_um = y_um = math.nan
        if p.get("position_stage"):
            x_um, y_um = self._move_xy_um(p["x"], p["y"])
        self._writer.set_metadata(position_x_um=x_um, position_y_um=y_um)
        if p.get("settle_time", 0.0) > 0:
            self._log("info", f"Settling {p['settle_time']:g} s before the first sample")
            self._sleep(p["settle_time"])
        op = self._rpc(lambda: self._dev("moke").start_operation(
            "time_trace", coil_V=p["b"], n_points=n, interval_s=p["interval"], acq_time_s=p["acq_time"],
            hall=record_hall, lockin=self.read_lockin), "start time trace")

        def on_record(rec):
            k = int(rec["index"])
            vals, by_id = self._sensor_values(rec)
            vals["time"] = by_id["time"] = rec["elapsed_s"]
            vals["coil_setpoint"] = p["b"]
            self._write((k,), vals)
            self.points_done += 1
            self._emit({"kind": "point", "workflow": self.wf.key, "index": k, "x": rec["elapsed_s"],
                        "values": by_id, "record": rec})
            if k % 10 == 9:
                self._writer.checkpoint()
        self._consume(op, on_record)
