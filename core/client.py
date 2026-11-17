"""Client side of the device service (Qt-free).

``ServiceClient`` is safe to use from several threads: one I/O thread owns the
ZeroMQ DEALER socket; callers enqueue requests and wait on a future keyed by
the request id.  A reply that arrives after its caller timed out is discarded
(and counted in ``late_replies``) -- it is never handed to a later request.

``DeviceProxy`` gives the TANGO-inspired device contract (read/write
attributes, commands, state/status, operations).  A few Samba-style aliases
(``read_attribute(...).value``, ``write_attribute``, ``command_inout``,
``state()``) keep familiar call patterns; this is not PyTango.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

import zmq

from core import protocol
from core.model import Quality, Reading
from core.protocol import ErrorCode, ServiceError

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 2.0


class ServiceClient:
    def __init__(self, rpc_port: int = protocol.DEFAULT_RPC_PORT,
                 event_port: int = protocol.DEFAULT_EVENT_PORT, client_name: str = "client",
                 host: str = protocol.LOOPBACK):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the first release talks to a local service only")
        self.rpc_addr = f"tcp://{host}:{rpc_port}"
        self.event_addr = f"tcp://{host}:{event_port}"
        self.client_name = client_name
        self.session: str | None = None
        self.user: dict | None = None
        #: Incremented whenever the session changes (login, logout, expiry).  GUIs tag their
        #: background requests with it and drop replies that belong to an earlier session.
        self.session_epoch = 0
        self.features: set[str] = set()
        self.instance_id: str | None = None
        self.late_replies = 0
        self._ctx = zmq.Context()
        self._pending: dict[str, Future] = {}
        self._pending_lock = threading.Lock()
        self._outbox: "queue.Queue[bytes | None]" = queue.Queue()
        self._closed = threading.Event()
        self._io_thread = threading.Thread(target=self._io_loop, name="svc-io", daemon=True)
        self._wake_addr = f"inproc://client-wake-{id(self)}"
        self._io_ready = threading.Event()
        self._io_thread.start()
        self._io_ready.wait(5)
        self._hb_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()
        self.on_connection_change: Callable[[str, dict], None] | None = None
        self.connected = False
        self.lease_holder = False
        self._sub_thread: threading.Thread | None = None
        self._subscribers: list[tuple[str, Callable[[str, dict], None]]] = []

    # ------------------------------------------------------------- transport
    def _io_loop(self) -> None:
        sock = self._ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RECONNECT_IVL, 200)
        sock.connect(self.rpc_addr)
        wake = self._ctx.socket(zmq.PULL)
        wake.bind(self._wake_addr)
        self._wake_push_local = threading.local()
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        poller.register(wake, zmq.POLLIN)
        self._io_ready.set()
        try:
            while not self._closed.is_set():
                ev = dict(poller.poll(100))
                if wake in ev:
                    while True:
                        try:
                            wake.recv(zmq.NOBLOCK)
                        except zmq.Again:
                            break
                while True:
                    try:
                        payload = self._outbox.get_nowait()
                    except queue.Empty:
                        break
                    if payload is None:
                        return
                    try:
                        sock.send(payload, zmq.NOBLOCK)
                    except zmq.Again:
                        pass   # the caller will time out
                if sock in ev:
                    while True:
                        try:
                            frames = sock.recv_multipart(zmq.NOBLOCK)
                        except zmq.Again:
                            break
                        self._deliver(frames[-1])
        finally:
            sock.close(0)
            wake.close(0)

    def _deliver(self, payload: bytes) -> None:
        try:
            msg = protocol.loads(payload)
        except ServiceError:
            return
        rid = msg.get("id") if isinstance(msg, dict) else None
        with self._pending_lock:
            fut = self._pending.pop(rid, None)
        if fut is None:
            self.late_replies += 1
            return
        fut.set_result(msg)

    def _wake_io(self) -> None:
        tls = self._wake_push_local
        sock = getattr(tls, "s", None)
        if sock is None:
            sock = self._ctx.socket(zmq.PUSH)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(self._wake_addr)
            tls.s = sock
        try:
            sock.send(b"", zmq.NOBLOCK)
        except zmq.ZMQError:
            pass

    def call(self, method: str, params: dict | None = None, timeout_s: float = protocol.DEFAULT_REQUEST_TIMEOUT_S) -> Any:
        if self._closed.is_set():
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, "client closed")
        req = protocol.make_request(method, params, self.session, timeout_s)
        fut: Future = Future()
        with self._pending_lock:
            self._pending[req["id"]] = fut
        self._outbox.put(protocol.dumps(req))
        self._wake_io()
        try:
            msg = fut.result(timeout=timeout_s + 0.5)
        except TimeoutError:
            with self._pending_lock:
                self._pending.pop(req["id"], None)
            self._set_connected(False, {"reason": f"no reply to {method} within {timeout_s} s"})
            raise ServiceError(ErrorCode.TIMEOUT, f"service did not answer {method} within {timeout_s} s "
                               "(is the service running?)", {"method": method}) from None
        self._set_connected(True, {})
        if msg.get("ok"):
            return msg.get("result")
        err = ServiceError.from_dict(msg.get("error") or {})
        if err.code == ErrorCode.AUTH_REQUIRED and self.session and req.get("session") == self.session:
            self.session = None
            self.user = None
            self.lease_holder = False
            self.session_epoch += 1
            self._notify("session_lost", {"message": err.message})
        raise err

    def _set_connected(self, value: bool, info: dict) -> None:
        if value != self.connected:
            self.connected = value
            self._notify("connected" if value else "disconnected", info)

    def _notify(self, what: str, info: dict) -> None:
        if self.on_connection_change:
            try:
                self.on_connection_change(what, info)
            except Exception:  # noqa: BLE001
                log.exception("connection callback")

    def close(self) -> None:
        self._hb_stop.set()
        self._closed.set()
        self._outbox.put(None)
        try:
            self._wake_io()
        except Exception:  # noqa: BLE001
            pass
        self._io_thread.join(2)
        if self._sub_thread:
            self._sub_thread.join(1)
        try:
            self._ctx.destroy(linger=0)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- session
    def hello(self, timeout_s: float = 3.0) -> dict:
        info = self.call("server.hello", {}, timeout_s)
        protocol.check_protocol(info.get("protocol"))
        if self.instance_id and info["instance_id"] != self.instance_id:
            self._notify("restarted", info)
        self.instance_id = info["instance_id"]
        self.features = set(info.get("features") or ())
        return info

    def has_feature(self, name: str) -> bool:
        """True when the connected service announced *name* in ``server.hello`` (services older
        than the feature do not, and answer its methods with UNKNOWN_METHOD)."""
        return name in getattr(self, "features", set())

    def login(self, username: str, password: str) -> dict:
        res = self.call("auth.login", {"username": username, "password": password,
                                       "client": self.client_name}, 15.0)
        self.session = res["session"]
        self.user = res["user"]
        self.session_epoch += 1
        # The session belongs to the service instance that answered the login -- which need not be
        # the one of the last hello (both apps started at once: the service restarted in between,
        # 17 Nov).  An older service does not say: ask it.
        iid = res.get("instance_id")
        if not iid:
            try:
                iid = self.call("server.hello", {}, 5.0).get("instance_id")
            except ServiceError:
                iid = None
        if iid:
            self.instance_id = iid
        self.start_heartbeat()
        return res

    def bootstrap(self, username: str, password: str, full_name: str = "") -> dict:
        return self.call("auth.bootstrap", {"username": username, "password": password,
                                            "full_name": full_name}, 15.0)

    def logout(self) -> None:
        if self.session:
            try:
                self.call("auth.logout", {}, 30.0)
            finally:
                self.session = None
                self.user = None
                self.lease_holder = False
                self.session_epoch += 1

    def start_heartbeat(self) -> None:
        if self._hb_thread and self._hb_thread.is_alive():
            return
        self._hb_stop.clear()

        def loop():
            while not self._hb_stop.wait(HEARTBEAT_INTERVAL_S):
                if not self.session:
                    continue
                try:
                    res = self.call("session.heartbeat", {}, 3.0)
                except ServiceError as exc:
                    if exc.code == ErrorCode.AUTH_REQUIRED:
                        self.lease_holder = False
                    continue
                if res.get("instance_id"):
                    # The session answered, so it lives on this instance.  A service restart ends
                    # the session (reported as session_lost); an id differing here only means the
                    # recorded one was stale -- adopt it instead of reporting a restart every beat.
                    self.instance_id = res["instance_id"]
                had = self.lease_holder
                self.lease_holder = bool(res.get("lease_holder"))
                if had and not self.lease_holder:
                    self._notify("lease_lost", res.get("lease") or {})
        self._hb_thread = threading.Thread(target=loop, name="svc-heartbeat", daemon=True)
        self._hb_thread.start()

    def stop_heartbeat(self) -> None:
        """Stop heartbeats (used by tests to simulate a lost client)."""
        self._hb_stop.set()

    # ------------------------------------------------------------- lease
    def acquire_lease(self) -> dict:
        st = self.call("lease.acquire", {}, 10.0)
        self.lease_holder = True
        return st

    def release_lease(self, force: bool = False, keep_outputs: list[str] | None = None) -> dict:
        """Release control; ``keep_outputs`` ("coil", "lockin") stay running instead of being
        switched off by the cleanup."""
        params: dict = {"force": force}
        if keep_outputs:
            params["keep_outputs"] = list(keep_outputs)
        st = self.call("lease.release", params, 120.0)
        self.lease_holder = False
        return st

    def lease_status(self) -> dict:
        return self.call("lease.status")

    # ------------------------------------------------------------- devices
    def for_run(self, run_id: str) -> "RunClient":
        """A private scan capability; the shared GUI client stays manual."""
        return RunClient(self, run_id)

    def device(self, name: str) -> "DeviceProxy":
        return DeviceProxy(self, name)

    def devices(self) -> list[dict]:
        return self.call("device.list")

    def status(self) -> dict:
        return self.call("server.status")

    # ------------------------------------------------------------- events
    def subscribe(self, prefix: str, callback: Callable[[str, dict], None]) -> None:
        """Receive PUB events whose topic starts with *prefix* (callback runs in
        the subscriber thread).  Notifications only -- never data."""
        self._subscribers.append((prefix, callback))
        if self._sub_thread is None:
            self._sub_thread = threading.Thread(target=self._sub_loop, name="svc-events", daemon=True)
            self._sub_thread.start()

    def _sub_loop(self) -> None:
        sock = self._ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        sock.connect(self.event_addr)
        try:
            while not self._closed.is_set():
                if not sock.poll(200):
                    continue
                try:
                    topic, payload = sock.recv_multipart(zmq.NOBLOCK)
                    body = protocol.loads(payload)
                except (zmq.ZMQError, ValueError, ServiceError):
                    continue
                t = topic.decode("utf-8", "replace")
                for prefix, cb in list(self._subscribers):
                    if t.startswith(prefix):
                        try:
                            cb(t, body)
                        except Exception:  # noqa: BLE001
                            log.exception("event callback")
        finally:
            sock.close(0)


class ReadingValue:
    """Samba-style ``read_attribute(...)`` result (``.value``, ``.quality``)."""

    def __init__(self, reading: Reading):
        self.value = reading.value
        self.quality = reading.quality
        self.time = reading.t
        self.unit = reading.unit
        self.reading = reading


class RunClient:
    """Attach the run capability without changing the shared session/client."""

    def __init__(self, client: ServiceClient, run_id: str):
        self._client = client
        self._run_id = run_id

    def call(self, method: str, params: dict | None = None,
             timeout_s: float = protocol.DEFAULT_REQUEST_TIMEOUT_S) -> Any:
        return self._client.call(method, dict(params or {}, _run_id=self._run_id), timeout_s)

    def device(self, name: str) -> "DeviceProxy":
        return DeviceProxy(self, name)

    def __getattr__(self, name):
        return getattr(self._client, name)


class DeviceProxy:
    def __init__(self, client: ServiceClient, name: str):
        self.client = client
        self.name = name
        self._info: dict | None = None

    # metadata
    def info(self, refresh: bool = False) -> dict:
        if self._info is None or refresh:
            self._info = self.client.call("device.info", {"device": self.name})
        return self._info

    def state(self) -> str:
        return self.info(refresh=True)["state"]

    def status(self) -> str:
        return self.info(refresh=True)["status"]

    def _command_info(self, command: str) -> dict | None:
        for c in self.info()["commands"]:
            if c["name"] == command:
                return c
        return None

    # attributes
    def read(self, *attrs: str, timeout_s: float = 30.0) -> dict[str, Reading]:
        raw = self.client.call("device.read", {"device": self.name, "attrs": list(attrs)}, timeout_s)
        return {k: Reading.from_dict(v) for k, v in raw.items()}

    def read_value(self, attr: str, allow_stale: bool = False, timeout_s: float = 30.0) -> Any:
        """Return the value, or raise if there is no valid reading (never 0 instead)."""
        r = self.read(attr, timeout_s=timeout_s)[attr]
        ok = r.valid or (allow_stale and r.quality == Quality.STALE)
        if not ok or r.value is None or (isinstance(r.value, float) and math.isnan(r.value)):
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE,
                               f"{self.name}.{attr}: no valid reading ({r.quality.value})")
        return r.value

    def write(self, attr: str, value: Any, timeout_s: float = 30.0) -> Any:
        return self.client.call("device.write", {"device": self.name, "attr": attr, "value": value},
                                timeout_s)["value"]

    # commands
    def command(self, command: str, timeout_s: float | None = None, **args) -> Any:
        if timeout_s is None:
            info = self._command_info(command)
            timeout_s = (info["timeout_s"] + 5.0) if info else protocol.DEFAULT_REQUEST_TIMEOUT_S
        return self.client.call("device.command", {"device": self.name, "command": command, "args": args},
                                min(timeout_s, protocol.MAX_REQUEST_TIMEOUT_S))

    def connect(self) -> dict:
        return self.client.call("device.connect", {"device": self.name}, 90.0)

    def disconnect(self) -> dict:
        return self.client.call("device.disconnect", {"device": self.name}, 60.0)

    def reconnect(self) -> dict:
        return self.client.call("device.reconnect", {"device": self.name}, 120.0)

    def start_operation(self, name: str, **args) -> "OperationHandle":
        summary = self.client.call("op.start", {"device": self.name, "name": name, "args": args}, 15.0)
        return OperationHandle(self.client, summary["op_id"], summary)

    # Samba-style aliases (not PyTango)
    def read_attribute(self, attr: str) -> ReadingValue:
        return ReadingValue(self.read(attr)[attr])

    def write_attribute(self, attr: str, value: Any) -> Any:
        return self.write(attr, value)

    def command_inout(self, command: str, args: dict | None = None) -> Any:
        return self.command(command, **(args or {}))


class OperationHandle:
    def __init__(self, client: ServiceClient, op_id: str, summary: dict | None = None):
        self.client = client
        self.op_id = op_id
        self.summary = summary or {}
        self.last_seq = 0
        self.state = self.summary.get("state", "RUNNING")

    def poll(self, timeout_s: float = 1.0) -> dict:
        res = self.client.call("op.wait", {"op_id": self.op_id, "since_seq": self.last_seq,
                                           "timeout_s": timeout_s, "known_state": self.state},
                               timeout_s + 5.0)
        self.last_seq = res["last_seq"]
        self.state = res["state"]
        return res

    def pause(self) -> dict:
        return self.client.call("op.pause", {"op_id": self.op_id})

    def resume(self) -> dict:
        return self.client.call("op.resume", {"op_id": self.op_id})

    def abort(self) -> dict:
        return self.client.call("op.abort", {"op_id": self.op_id})

    @property
    def done(self) -> bool:
        return self.state in ("COMPLETED", "ABORTED", "FAILED")
