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
