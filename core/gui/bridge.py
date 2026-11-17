"""Qt bridges: deliver client/engine callbacks (background threads) as Qt
signals on the GUI thread, and run blocking service calls off the GUI thread."""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

from core.client import ServiceClient
from core.config import machine
from core.protocol import ServiceError
from core.scan.engine import Callbacks

log = logging.getLogger(__name__)


def service_ports() -> tuple[int, int]:
    """Ports from the machine configuration (the same file the service reads)."""
    try:
        cfg = machine.load()
        b = cfg.data["backend"]
        return int(b["rpc_port"]), int(b["event_port"])
    except Exception:  # noqa: BLE001
        return machine.DEFAULTS["backend"]["rpc_port"], machine.DEFAULTS["backend"]["event_port"]


class ServiceBridge(QObject):
    connection = pyqtSignal(str, dict)     # connected / disconnected / restarted / session_lost / lease_lost
    event = pyqtSignal(str, dict)          # PUB notifications

    def __init__(self, client_name: str, rpc_port: int | None = None, event_port: int | None = None,
                 parent=None):
        super().__init__(parent)
        if rpc_port is None or event_port is None:
            rpc_port, event_port = service_ports()
        self.client = ServiceClient(rpc_port, event_port, client_name)
        self.client.on_connection_change = lambda what, info: self.connection.emit(what, dict(info or {}))
        self.client.subscribe("", lambda topic, body: self.event.emit(topic, dict(body)))
        self.hello_info: dict | None = None

    def probe(self, timeout_s: float = 1.5) -> dict | None:
        try:
            self.hello_info = self.client.hello(timeout_s)
            return self.hello_info
        except ServiceError:
            self.hello_info = None
            return None

    def close(self) -> None:
        self.client.close()


class _Signals(QObject):
    ok = pyqtSignal(object)
    err = pyqtSignal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    @pyqtSlot()
    def run(self):
        try:
            res = self.fn()
        except ServiceError as exc:
            self.signals.err.emit(exc)
        except Exception as exc:  # noqa: BLE001
            log.error("background call failed:\n%s", traceback.format_exc())
            self.signals.err.emit(exc)
        else:
            self.signals.ok.emit(res)


_keep: set = set()


def call_async(fn: Callable[[], Any], ok: Callable[[Any], None] | None = None,
               err: Callable[[Exception], None] | None = None) -> None:
    """Run *fn* in the Qt thread pool; deliver the result/exception on the GUI thread."""
    task = _Task(fn)
    holder = task.signals
    _keep.add(holder)

    def done_ok(res):
        _keep.discard(holder)
        if ok:
            ok(res)

    def done_err(exc):
        _keep.discard(holder)
        if err:
            err(exc)
        else:
            log.warning("background call failed: %s", exc)

    holder.ok.connect(done_ok)
    holder.err.connect(done_err)
    QThreadPool.globalInstance().start(task)


def call_guarded(client: ServiceClient, fn: Callable[[], Any], ok: Callable[[Any], None] | None = None,
                 err: Callable[[Exception], None] | None = None,
                 still_valid: Callable[[], bool] | None = None) -> None:
    """``call_async`` whose reply is delivered only while it is still relevant: the client's
    session is the one the request was made in (no login, logout or expiry since) and
    ``still_valid()`` -- e.g. "the same device / setup is still selected" -- holds.  An obsolete
    reply is dropped (logged at debug level), so it can never update what another user or
    another device now shows."""
    epoch = client.session_epoch

    def current() -> bool:
        return client.session_epoch == epoch and (still_valid is None or bool(still_valid()))

    def done_ok(res):
        if current():
            if ok:
                ok(res)
        else:
            log.debug("dropped an obsolete reply")

    def done_err(exc):
        if current():
            if err:
                err(exc)
            else:
                log.warning("background call failed: %s", exc)
        else:
            log.debug("dropped an obsolete error: %s", exc)

    call_async(fn, done_ok, done_err)


class _Relay(QObject):
    done = pyqtSignal(object, object, object)       # callback, result, exception


class SerialCaller(QObject):
    """Runs background calls one after another on a single worker thread, so that requests
    of one kind (e.g. preference saves) reach the service in the order they were made; results
    are delivered on the GUI thread.  ``call_sync`` waits for everything queued before it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from concurrent.futures import ThreadPoolExecutor
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="serial-call")
        self._relay = _Relay()
        self._relay.done.connect(self._deliver)

    def _deliver(self, cb, res, exc) -> None:
        if cb is not None:
            cb(res, exc)

    def submit(self, fn: Callable[[], Any], done: Callable[[Any, Exception | None], None]) -> None:
        relay = self._relay

        def run():
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001 - delivered to the caller
                relay.done.emit(done, None, exc)
            else:
                relay.done.emit(done, res, None)
        self._exec.submit(run)

    def call_sync(self, fn: Callable[[], Any], timeout_s: float = 10.0) -> Any:
        """Run *fn* after the queued calls and return its result (raises its exception)."""
        return self._exec.submit(fn).result(timeout=timeout_s)

    def shutdown(self) -> None:
        self._exec.shutdown(wait=False, cancel_futures=True)


class EngineBridge(QObject):
    state = pyqtSignal(str, str)
    point = pyqtSignal(object)
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str, str)
    finished = pyqtSignal(object)

    def callbacks(self) -> Callbacks:
        return Callbacks(state=lambda s, m: self.state.emit(s, m),
                         point=lambda e: self.point.emit(e),
                         progress=lambda d, t: self.progress.emit(d, t),
                         log=lambda lvl, m: self.log.emit(lvl, m),
                         finished=lambda r: self.finished.emit(r))


def error_text(exc: Exception) -> str:
    if isinstance(exc, ServiceError):
        return f"{exc.message}  [{exc.code}]"
    return str(exc)
