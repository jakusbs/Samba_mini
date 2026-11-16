"""Device base class used by real, simulated and logical devices."""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.model import AttributeInfo, CommandInfo, DevState, Quality, Reading
from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.worker import NEVER_CANCELLED, CancelToken

log = logging.getLogger(__name__)


@dataclass
class CallContext:
    """Passed to commands and operations."""

    cancel: CancelToken = NEVER_CANCELLED
    host: Any = None                      # DeviceHost (for logical devices)
    operation: Any = None                 # Operation when running as one
    session_user: str | None = None
    extras: dict = field(default_factory=dict)


class Device:
    """Base class.

    Subclasses declare ``ATTRIBUTES``/``COMMANDS``/``OPERATIONS`` and implement
    ``_connect``, ``_disconnect``, ``_read_attribute`` and command methods named
    ``cmd_<name>`` / operations ``op_<name>``.  Attribute writes go to
    ``_write_attribute``.

    Physical devices are always invoked on their own worker thread by the host;
    logical devices run in the caller's thread and reach instruments only
    through ``ctx.host``.
    """

    KIND = "device"
    PHYSICAL = True
    ATTRIBUTES: list[AttributeInfo] = []
    COMMANDS: list[CommandInfo] = []
    OPERATIONS: list[CommandInfo] = []
    #: attributes polled by the monitor while the device is idle
    MONITORED: list[str] = []
    MONITOR_INTERVAL_S = 2.0
    #: attribute read timeout (s) used by the host
    READ_TIMEOUT_S = 10.0

    def __init__(self, name: str, config: dict | None = None, simulated: bool = False):
        self.name = name
        self.config = dict(config or {})
        self.simulated = simulated
        self.state = DevState.OFF
        self.status = "not connected"
        self.last_error: dict | None = None
        self.identity: dict = {"model": self.KIND, "serial": None, "resource": None,
                               "driver": type(self).__name__}
        self.attributes = {a.name: a for a in self.ATTRIBUTES}
        self.commands = {c.name: c for c in self.COMMANDS}
        self.operations = {c.name: c for c in self.OPERATIONS}
        self.cache: dict[str, Reading] = {}
        self._cache_lock = threading.Lock()
        self.on_state_change: Callable[["Device"], None] | None = None
        self.on_reading: Callable[["Device", str, Reading], None] | None = None

    # ---------------------------------------------------------------- state --
    def set_state(self, state: DevState, status: str | None = None) -> None:
        changed = state != self.state or (status is not None and status != self.status)
        self.state = state
        if status is not None:
            self.status = status
        if changed and self.on_state_change:
            try:
                self.on_state_change(self)
            except Exception:  # noqa: BLE001
                log.exception("state callback failed")

    def set_fault(self, exc: BaseException | str, where: str = "") -> None:
        msg = str(exc)
        self.last_error = {"t": time.time(), "where": where, "message": msg,
                           "type": type(exc).__name__ if not isinstance(exc, str) else "Error"}
        self.set_state(DevState.FAULT, f"{where}: {msg}" if where else msg)

    @property
    def usable(self) -> bool:
        return self.state in (DevState.ON, DevState.MOVING, DevState.RUNNING)

    def require_usable(self) -> None:
        if not self.usable:
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE,
                               f"{self.name} is {self.state.value}: {self.status}",
                               {"device": self.name, "state": self.state.value})

    def info(self) -> dict:
        return {
            "name": self.name,
            "kind": self.KIND,
            "physical": self.PHYSICAL,
            "simulated": self.simulated,
            "identity": self.identity,
            "state": self.state.value,
            "status": self.status,
            "last_error": self.last_error,
            "capabilities": sorted({f"read:{a.name}" for a in self.ATTRIBUTES}
                                   | {f"write:{a.name}" for a in self.ATTRIBUTES if a.access == "rw"}
                                   | {f"command:{c.name}" for c in self.COMMANDS}
                                   | {f"operation:{o.name}" for o in self.OPERATIONS}),
            "attributes": [a.to_dict() for a in self.ATTRIBUTES],
            "commands": [c.to_dict() for c in self.COMMANDS],
            "operations": [c.to_dict() for c in self.OPERATIONS],
            "config": self.public_config(),
        }

    def public_config(self) -> dict:
        return self.config

    # ------------------------------------------------------------ lifecycle --
    def connect(self) -> None:
        self.set_state(DevState.INIT, "connecting")
        try:
            self._connect()
        except ServiceError as exc:
            if exc.code == ErrorCode.NOT_SUPPORTED:
                self.set_state(DevState.DISABLED, exc.message)
            else:
                self.set_fault(exc.message, "connect")
            raise
        except Exception as exc:  # noqa: BLE001 - any driver error
            self.set_fault(exc, "connect")
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, f"{self.name}: {exc}",
                               {"device": self.name}) from exc
        self.last_error = None
        if self.state in (DevState.INIT, DevState.OFF, DevState.FAULT, DevState.UNKNOWN):
            self.set_state(DevState.ON, "connected (simulated)" if self.simulated else "connected")

    def disconnect(self) -> None:
        try:
            self._disconnect()
        finally:
            with self._cache_lock:
                self.cache.clear()
            self.set_state(DevState.OFF, "disconnected")

    def _connect(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _disconnect(self) -> None:
        pass

    def cleanup(self, ctx: CallContext) -> list[str]:
        """Bring outputs to the configured safe state.  Return a list of problems."""
        return []

    # ----------------------------------------------------------- attributes --
    def read_attribute(self, name: str) -> Reading:
        info = self.attributes.get(name)
        if info is None:
            raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, f"{self.name} has no attribute {name!r}")
        self.require_usable()
        try:
            value = self._read_attribute(name)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.set_fault(exc, f"read {name}")
            raise ServiceError(ErrorCode.DEVICE_FAULT, f"{self.name}.{name}: {exc}",
                               {"device": self.name}) from exc
        reading = self.make_reading(name, value, "device")
        self.store(name, reading)
        return reading

    def make_reading(self, name: str, value: Any, source: str, t: float | None = None) -> Reading:
        info = self.attributes.get(name)
        unit = info.unit if info else ""
        if value is None or (isinstance(value, float) and math.isnan(value)):
            quality = Quality.INVALID
        else:
            quality = Quality.SIMULATED if self.simulated else Quality.VALID
        return Reading(value=value, quality=quality, t=t or time.time(), unit=unit, source=source)

    def store(self, name: str, reading: Reading) -> None:
        with self._cache_lock:
            self.cache[name] = reading
        if self.on_reading:
            try:
                self.on_reading(self, name, reading)
            except Exception:  # noqa: BLE001
                log.exception("reading callback failed")

    def cached(self, name: str) -> Reading:
        with self._cache_lock:
            r = self.cache.get(name)
        if r is None:
            unit = self.attributes[name].unit if name in self.attributes else ""
            return Reading(value=None, quality=Quality.INVALID, t=0.0, unit=unit, source="cache")
        age = max(0.0, time.time() - r.t)
        return Reading(value=r.value, quality=Quality.STALE if r.quality == Quality.VALID else r.quality,
                       t=r.t, unit=r.unit, age_s=age, source=f"cache:{r.source}")

    def write_attribute(self, name: str, value: Any) -> Any:
        info = self.attributes.get(name)
        if info is None:
            raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, f"{self.name} has no attribute {name!r}")
        if info.access != "rw":
            raise ServiceError(ErrorCode.READ_ONLY, f"{self.name}.{name} is read-only")
        value = self.validate_write(name, value)
        self.require_usable()
        try:
            applied = self._write_attribute(name, value)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.set_fault(exc, f"write {name}")
            raise ServiceError(ErrorCode.DEVICE_FAULT, f"{self.name}.{name}: {exc}") from exc
        applied = value if applied is None else applied
        self.store(name, self.make_reading(name, applied, "write"))
        return applied

    def validate_write(self, name: str, value: Any) -> Any:
        from core.model import OutOfRange
        info = self.attributes[name]
        try:
            return info.validate(value)
        except OutOfRange as exc:
            raise ServiceError(ErrorCode.OUT_OF_RANGE, str(exc),
                               {"min": exc.vmin, "max": exc.vmax}) from exc
        except (ValueError, PermissionError) as exc:
            raise ServiceError(ErrorCode.INVALID_VALUE, str(exc)) from exc

    def _read_attribute(self, name: str) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def _write_attribute(self, name: str, value: Any) -> Any:
        raise ServiceError(ErrorCode.NOT_SUPPORTED, f"{self.name}.{name} cannot be written")

    # ------------------------------------------------------------- commands --
    def bind_args(self, table: dict, name: str, args: dict | None) -> tuple[CommandInfo, dict]:
        from core.model import OutOfRange
        info = table.get(name)
        if info is None:
            raise ServiceError(ErrorCode.UNKNOWN_COMMAND, f"{self.name} has no command {name!r}")
        try:
            return info, info.bind(args)
        except OutOfRange as exc:
            raise ServiceError(ErrorCode.OUT_OF_RANGE, str(exc),
                               {"min": exc.vmin, "max": exc.vmax}) from exc
        except ValueError as exc:
            raise ServiceError(ErrorCode.INVALID_VALUE, str(exc)) from exc

    def execute(self, name: str, args: dict, ctx: CallContext) -> Any:
        fn = getattr(self, f"cmd_{name}", None)
        if fn is None:
            raise ServiceError(ErrorCode.UNKNOWN_COMMAND, f"{self.name} has no command {name!r}")
        if name not in ("reconnect",):
            self.require_usable()
        try:
            return fn(ctx, **args)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.set_fault(exc, name)
            raise ServiceError(ErrorCode.DEVICE_FAULT, f"{self.name}.{name}: {exc}",
                               {"device": self.name}) from exc

    def run_operation(self, name: str, args: dict, ctx: CallContext) -> Any:
        fn = getattr(self, f"op_{name}", None)
        if fn is None:
            raise ServiceError(ErrorCode.UNKNOWN_COMMAND, f"{self.name} has no operation {name!r}")
        return fn(ctx, **args)

    # helper for subclasses: bounded, cancellable sleep
    @staticmethod
    def sleep(seconds: float, ctx: CallContext | None = None) -> None:
        """High-resolution, cancellable sleep.

        ``time.sleep`` is high resolution on Windows since Python 3.11, while
        ``Event.wait`` timeouts can be ~15 ms coarse, so sleep in short slices
        and check the cancel token between them.
        """
        if seconds <= 0:
            return
        end = time.perf_counter() + seconds
        cancel = ctx.cancel if ctx is not None else NEVER_CANCELLED
        while True:
            cancel.raise_if_cancelled()
            left = end - time.perf_counter()
            if left <= 0:
                return
            time.sleep(min(left, 0.02))
