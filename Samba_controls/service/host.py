"""Device host: creates devices from the machine configuration, owns one
worker per physical instrument, serves readbacks and runs cleanup.

Monitoring
----------
A monitor thread polls each idle physical device's ``MONITORED`` attributes.
It never queues behind real work: it skips a device whose worker is busy or has
a backlog, and it is suspended completely while a measurement run is active
(``run_active``).  During a run, reads are answered from the values the
acquisition itself stored (``source = "acquisition"``/``"cache"``), so a
monitoring client never races the acquisition driver.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from core.config.machine import MachineConfig
from core.model import DevState, Reading
from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.base import CallContext, Device
from Samba_controls.service.worker import CancelToken, DeviceWorker

log = logging.getLogger(__name__)


def build_devices(cfg: MachineConfig, state_dir) -> tuple[list[Device], dict]:
    """Instantiate devices for the configured mode.  Returns (devices, notes)."""
    from Samba_controls.service.devices.logical import Magnet, MokeSequencer
    from Samba_controls.service.devices.stage import OffsetStore

    data = cfg.data
    mode = data["backend"]["mode"]
    offsets = OffsetStore(state_dir / "stage_offsets.json")
    coil = data["coil"]
    daq_cfg = dict(data["daq"], coil_min_V=coil["min_V"], coil_max_V=coil["max_V"])
    notes = {}
    devices: list[Device] = []
    if mode == "simulation":
        from Samba_controls.service.devices.sim import SimDaq, SimHall, SimLockin, SimPhysics, SimStage
        phys = SimPhysics()
        notes["physics"] = phys
        devices += [SimDaq("daq", daq_cfg, phys), SimLockin("lockin", data["lockin"], phys),
                    SimHall("hall", data["hall"], phys)]
        for key, st in data["stages"].items():
            devices.append(SimStage(f"stage_{key}", dict(st, key=key), phys, offsets))
    else:
        from Samba_controls.service.devices.ametek7270 import Ametek7270
        from Samba_controls.service.devices.hall import MagnetPhysikHall
        from Samba_controls.service.devices.kinesis_stage import KinesisStage
        from Samba_controls.service.devices.nidaq import NiDaq
        from Samba_controls.service.devices.trinamic_stage import TrinamicStage
        devices += [NiDaq("daq", daq_cfg), Ametek7270("lockin", data["lockin"]),
                    MagnetPhysikHall("hall", data["hall"])]
        drivers = {"kinesis": KinesisStage, "trinamic": TrinamicStage}
        for key, st in data["stages"].items():
            cls = drivers.get(st.get("driver"))
            if cls is None:
                raise ServiceError(ErrorCode.INVALID_VALUE, f"unknown stage driver {st.get('driver')!r}")
            devices.append(cls(f"stage_{key}", dict(st, key=key), offsets=offsets))
    cal = data.get("field_calibration", {})
    devices.append(Magnet("magnet", {"coil_min_V": coil["min_V"], "coil_max_V": coil["max_V"],
                                     "amps_per_volt": coil.get("amps_per_volt"),
                                     "field_mT_per_V": cal.get("mT_per_V")}, simulated=mode == "simulation"))
    devices.append(MokeSequencer("moke", {}, simulated=mode == "simulation"))
    enabled = {"daq": data["daq"]["enabled"], "lockin": data["lockin"]["enabled"],
               "hall": data["hall"]["enabled"]}
    for key, st in data["stages"].items():
        enabled[f"stage_{key}"] = st.get("enabled", True)
    notes["enabled"] = enabled
    return devices, notes


class DeviceHost:
    def __init__(self, devices: list[Device], enabled: dict[str, bool] | None = None,
                 publish: Callable[[str, dict], None] | None = None):
        self._devices = {d.name: d for d in devices}
        self._workers: dict[str, DeviceWorker] = {}
        self.enabled = enabled or {}
        self.publish = publish or (lambda topic, body: None)
        self.run_active = False
        self.run_info: dict | None = None
        self._last_poll: dict[tuple[str, str], float] = {}
        self._stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        for d in devices:
            d.on_state_change = self._state_changed
            d.on_reading = self._reading_stored
            if d.PHYSICAL:
                self._workers[d.name] = DeviceWorker(d.name)
            elif hasattr(d, "bind_host"):
                d.bind_host(self)
        for name, on in self.enabled.items():
            if not on and name in self._devices:
                self._devices[name].set_state(DevState.DISABLED, "disabled in the machine configuration")

    # ------------------------------------------------------------ access
    def device(self, name: str) -> Device:
        d = self._devices.get(name)
        if d is None:
            raise ServiceError(ErrorCode.UNKNOWN_DEVICE, f"no device {name!r}")
        return d

    def devices(self) -> list[Device]:
        return list(self._devices.values())

    def submit(self, name: str, fn: Callable[[Device], Any], timeout_s: float,
               cancel: CancelToken | None = None, label: str = ""):
        d = self.device(name)
        if not d.PHYSICAL:
            raise ServiceError(ErrorCode.INTERNAL, f"{name} is logical")
        return self._workers[name].submit(lambda: fn(d), timeout_s, cancel, label)

    def wait(self, name: str, job, timeout_s: float):
        return self._workers[name].wait(job, timeout_s)

    def run(self, name: str, fn: Callable[[Device], Any], timeout_s: float,
            cancel: CancelToken | None = None, label: str = "") -> Any:
        d = self.device(name)
        if not d.PHYSICAL:
            return fn(d)
        return self._workers[name].call(lambda: fn(d), timeout_s, cancel, label)

    # ------------------------------------------------------------ lifecycle
    def connect(self, name: str, timeout_s: float = 60.0) -> dict:
        d = self.device(name)
        if not self.enabled.get(name, True):
            raise ServiceError(ErrorCode.NOT_SUPPORTED, f"{name} is disabled in the machine configuration")
        if not d.PHYSICAL:
            if hasattr(d, "refresh_state"):
                d.refresh_state()
            return d.info()
        self.run(name, lambda dev: dev.connect(), timeout_s, label="connect")
        self._refresh_logical()
        return d.info()

    def disconnect(self, name: str, timeout_s: float = 30.0) -> dict:
        d = self.device(name)
        if d.PHYSICAL:
            self.run(name, lambda dev: dev.disconnect(), timeout_s, label="disconnect")
        self._refresh_logical()
        return d.info()

    def reconnect(self, name: str) -> dict:
        d = self.device(name)
        if d.PHYSICAL and d.state not in (DevState.OFF, DevState.DISABLED):
            try:
                self.disconnect(name)
            except ServiceError as exc:
                log.warning("disconnect before reconnect failed: %s", exc)
        return self.connect(name)

    def connect_all(self, timeout_s: float = 60.0) -> dict:
        """Connect every enabled physical device in parallel (each on its own worker)."""
        jobs = {}
        for name, d in self._devices.items():
            if d.PHYSICAL and self.enabled.get(name, True):
                jobs[name] = self._workers[name].submit(d.connect, timeout_s, label="connect")
        results = {}
        for name, job in jobs.items():
            try:
                self._workers[name].wait(job, timeout_s)
                results[name] = "ON"
            except ServiceError as exc:
                results[name] = f"{exc.code}: {exc.message}"
            except Exception as exc:  # noqa: BLE001
                results[name] = str(exc)
        self._refresh_logical()
        return results

    def _refresh_logical(self) -> None:
        for d in self._devices.values():
            if not d.PHYSICAL and hasattr(d, "refresh_state"):
                try:
                    d.refresh_state()
                except Exception:  # noqa: BLE001
                    pass

    def shutdown(self) -> None:
        self._stop.set()
        for name, d in self._devices.items():
            if d.PHYSICAL and d.state not in (DevState.OFF, DevState.DISABLED):
                try:
                    self.run(name, lambda dev: dev.disconnect(), 15.0, label="disconnect")
                except Exception as exc:  # noqa: BLE001
                    log.warning("disconnect %s failed: %s", name, exc)
        for w in self._workers.values():
            w.stop()
        for w in self._workers.values():
            w.join(2.0)

    # ------------------------------------------------------------ reads
    def read(self, name: str, attrs: list[str], timeout_s: float | None = None) -> dict:
        d = self.device(name)
        out = {}
        for attr in attrs:
            if attr not in d.attributes:
                raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, f"{name} has no attribute {attr!r}")
            if d.PHYSICAL and (self.run_active or self._workers[name].busy):
                # Coordinated readback: never interleave with the acquisition.
                out[attr] = d.cached(attr).to_dict()
                continue
            if d.PHYSICAL and not d.usable:
                r = d.cached(attr).to_dict()
                r["error"] = f"{name} is {d.state.value}: {d.status}"
                out[attr] = r
                continue
            t = timeout_s or d.READ_TIMEOUT_S
            reading: Reading = self.run(name, lambda dev, a=attr: dev.read_attribute(a), t, label=f"read {attr}")
            out[attr] = reading.to_dict()
        return out

    def write(self, name: str, attr: str, value: Any, timeout_s: float = 30.0) -> Any:
        return self.run(name, lambda dev: dev.write_attribute(attr, value), timeout_s, label=f"write {attr}")

    def command(self, name: str, command: str, args: dict, ctx: CallContext) -> Any:
        d = self.device(name)
        info, bound = d.bind_args(d.commands, command, args)
        if command == "stop" and hasattr(d, "request_stop"):
            d.request_stop()              # reaches a move already running on the worker
        if not d.PHYSICAL:
            return d.execute(command, bound, ctx)
        return self.run(name, lambda dev: dev.execute(command, bound, ctx), info.timeout_s, ctx.cancel,
                        label=command)

    # ------------------------------------------------------------ cleanup
    def cleanup_all(self, cleanup_cfg: dict | None = None, timeout_s: float = 30.0) -> list[str]:
        cfg = cleanup_cfg or {}
        problems: list[str] = []
        ctx = CallContext(host=self)
        order = []
        if cfg.get("stop_stages", True):
            order += [n for n, d in self._devices.items() if d.KIND == "stage"]
        if cfg.get("zero_coil", True):
            order.append("daq")
        if cfg.get("lockin_output_off", True):
            order.append("lockin")
        for name in order:
            d = self._devices.get(name)
            if d is None or d.state == DevState.DISABLED:
                continue
            if hasattr(d, "request_stop"):
                d.request_stop()
            try:
                problems += self.run(name, lambda dev: dev.cleanup(ctx), timeout_s, label="cleanup")
            except ServiceError as exc:
                problems.append(f"{name}: cleanup did not complete ({exc.message})")
        return problems

    # ------------------------------------------------------------ monitor
    def start_monitor(self, period_s: float = 0.25) -> None:
        def loop():
            while not self._stop.wait(period_s):
                if self.run_active:
                    continue
                now = time.monotonic()
                for name, d in list(self._devices.items()):
                    if not d.PHYSICAL or not d.usable or not d.MONITORED:
                        continue
                    w = self._workers[name]
                    if w.busy or w.backlog:
                        continue
                    for attr in d.MONITORED:
                        key = (name, attr)
                        if now - self._last_poll.get(key, 0.0) < d.MONITOR_INTERVAL_S:
                            continue
                        self._last_poll[key] = now
                        w.submit(lambda dev=d, a=attr: self._monitor_read(dev, a), d.READ_TIMEOUT_S,
                                 label=f"monitor {attr}")
        self._monitor_thread = threading.Thread(target=loop, name="monitor", daemon=True)
        self._monitor_thread.start()

    def _monitor_read(self, d: Device, attr: str) -> None:
        if self.run_active:
            return
        try:
            d.read_attribute(attr)
        except ServiceError as exc:
            if exc.code not in (ErrorCode.INVALID_STATE, ErrorCode.DEVICE_UNAVAILABLE):
                log.debug("monitor read %s.%s: %s", d.name, attr, exc.message)

    # ------------------------------------------------------------ events
    def _state_changed(self, d: Device) -> None:
        self.publish(f"state.{d.name}", {"device": d.name, "state": d.state.value, "status": d.status,
                                         "last_error": d.last_error})

    def _reading_stored(self, d: Device, attr: str, r: Reading) -> None:
        self.publish(f"readback.{d.name}", {"device": d.name, "attr": attr, **r.to_dict()})

    def summary(self) -> list[dict]:
        out = []
        for d in self._devices.values():
            w = self._workers.get(d.name)
            out.append({"name": d.name, "kind": d.KIND, "physical": d.PHYSICAL, "simulated": d.simulated,
                        "state": d.state.value, "status": d.status, "last_error": d.last_error,
                        "busy": bool(w and w.busy), "busy_with": w.current_label if w else None,
                        "identity": d.identity})
        return out
