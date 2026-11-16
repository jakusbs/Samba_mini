"""The device service: ZeroMQ RPC + events on 127.0.0.1.

See ``core/protocol.py`` for the wire format and ``docs/architecture.md`` for
the design.  Every method is listed in ``self._methods`` with its access level:

* ``public``  -- no session needed (hello, login, first-user bootstrap)
* ``auth``    -- a valid session
* ``lease``   -- a valid session that holds the setup lease
* ``admin``   -- a valid session with the admin role
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import zmq

from core import protocol
from core.config.machine import MachineConfig
from core.protocol import ErrorCode, ServiceError
from core.version import PROTOCOL, describe, software_revision
from Samba_controls.service.auth import AccountStore, SessionManager
from Samba_controls.service.devices.base import CallContext
from Samba_controls.service.host import DeviceHost, build_devices
from Samba_controls.service.lease import LeaseManager
from Samba_controls.service.operations import FINAL_STATES, Operation, OperationManager
from Samba_controls.service.worker import CancelToken

log = logging.getLogger(__name__)


class ServiceServer:
    def __init__(self, cfg: MachineConfig, accounts_path: Path, state_dir: Path,
                 control_token: str | None = None, connect: bool = True,
                 rpc_port: int | None = None, event_port: int | None = None):
        self.cfg = cfg
        self.backend_cfg = cfg.data["backend"]
        self.read_only = bool(self.backend_cfg.get("read_only"))
        self.rpc_port = int(rpc_port or self.backend_cfg["rpc_port"])
        self.event_port = int(event_port or self.backend_cfg["event_port"])
        self.instance_id = protocol.new_id()
        self.started = time.time()
        self.control_token = control_token
        self.accounts = AccountStore(accounts_path)
        self.sessions = SessionManager(float(self.backend_cfg["session_idle_timeout_s"]))
        self._out: "queue.Queue[tuple]" = queue.Queue()
        self._ctx = zmq.Context()
        self._tls = threading.local()
        self._control_lock = threading.RLock()
        self._inflight: dict[str, set[str]] = {}
        self._stop = threading.Event()
        self._shutdown_reason = ""
        self.devices_list, notes = build_devices(cfg, state_dir)
        self.sim_physics = notes.get("physics")
        self.host = DeviceHost(self.devices_list, notes.get("enabled"), self.publish)
        self.ops = OperationManager(on_update=self._op_updated)
        self.lease = LeaseManager(float(self.backend_cfg["lease_ttl_s"]), self._reconcile,
                                  lambda st: self.publish("lease", st))
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="rpc")
        self._methods: dict[str, tuple[str, Callable]] = {}
        self._register()
        self._connect_on_start = connect

    # ================================================================ events
    def publish(self, topic: str, body: dict) -> None:
        self._out.put(("event", topic, body))
        self._wake()

    def _wake(self) -> None:
        """Wake the socket-owning loop.  ZeroMQ sockets are not thread-safe, so
        each thread keeps its own PUSH socket to the loop's inproc PULL."""
        addr = getattr(self, "_wake_addr", None)
        if addr is None or self._stop.is_set():
            return
        sock = getattr(self._tls, "push", None)
        try:
            if sock is None:
                sock = self._ctx.socket(zmq.PUSH)
                sock.setsockopt(zmq.LINGER, 0)
                sock.setsockopt(zmq.SNDHWM, 10)
                sock.connect(addr)
                self._tls.push = sock
            sock.send(b"", zmq.NOBLOCK)
        except zmq.ZMQError:
            pass

    def _op_updated(self, op: Operation) -> None:
        self.publish(f"op.{op.id}", op.summary())

    # ================================================================ run loop
    def bind(self) -> None:
        self._router = self._ctx.socket(zmq.ROUTER)
        self._router.setsockopt(zmq.LINGER, 0)
        self._router.bind(f"tcp://{protocol.LOOPBACK}:{self.rpc_port}")
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.LINGER, 0)
        self._pub.bind(f"tcp://{protocol.LOOPBACK}:{self.event_port}")
        self._wake_addr = f"inproc://wake-{self.instance_id}"
        self._wake_pull = self._ctx.socket(zmq.PULL)
        self._wake_pull.bind(self._wake_addr)

    def start_background(self) -> None:
        """Connect devices and start helper threads (call after bind)."""
        threading.Thread(target=self._watchdog, name="lease-watchdog", daemon=True).start()
        if self._connect_on_start:
            def connect():
                res = self.host.connect_all()
                log.info("device connection results: %s", res)
                self.publish("server", self.status_public())
            threading.Thread(target=connect, name="connect-all", daemon=True).start()
        self.host.start_monitor()

    def serve_forever(self) -> None:
        poller = zmq.Poller()
        poller.register(self._router, zmq.POLLIN)
        poller.register(self._wake_pull, zmq.POLLIN)
        log.info("service %s listening on %s:%d (events :%d), mode=%s", describe(), protocol.LOOPBACK,
                 self.rpc_port, self.event_port, self.cfg.mode)
        try:
            while not self._stop.is_set():
                events = dict(poller.poll(200))
                if self._router in events:
                    while True:
                        try:
                            frames = self._router.recv_multipart(zmq.NOBLOCK)
                        except zmq.Again:
                            break
                        self._accept(frames)
                if self._wake_pull in events:
                    while True:
                        try:
                            self._wake_pull.recv(zmq.NOBLOCK)
                        except zmq.Again:
                            break
                self._flush_out()
        finally:
            self._flush_out()
            self._teardown()

    def _flush_out(self) -> None:
        while True:
            try:
                item = self._out.get_nowait()
            except queue.Empty:
                return
            try:
                if item[0] == "reply":
                    _, ident, msg = item
                    self._router.send_multipart([*ident, protocol.dumps(msg)])
                else:
                    _, topic, body = item
                    body = dict(body, _t=time.time(), _instance=self.instance_id)
                    self._pub.send_multipart([topic.encode("utf-8"), protocol.dumps(body)])
            except (zmq.ZMQError, TypeError, ValueError) as exc:
                log.warning("send failed: %s", exc)

    def _accept(self, frames: list[bytes]) -> None:
        if len(frames) < 2:
            return
        ident, payload = frames[:-1], frames[-1]
        if len(payload) > 16 * 1024 * 1024:
            self._out.put(("reply", ident, protocol.make_error(
                None, ServiceError(ErrorCode.BAD_REQUEST, "message too large"))))
            return
        self._pool.submit(self._handle, ident, payload, time.monotonic())

    def _handle(self, ident, payload: bytes, received_at: float | None = None) -> None:
        rid = None
        try:
            msg = protocol.loads(payload)
            rid = msg.get("id") if isinstance(msg, dict) else None
            req = protocol.validate_request(msg)
            rid = req["id"]
            req["_deadline"] = (received_at if received_at is not None else time.monotonic()) + req["timeout_s"]
            result = self.dispatch(req)
            reply = protocol.make_reply(rid, result)
        except ServiceError as exc:
            reply = protocol.make_error(rid, exc)
        except Exception as exc:  # noqa: BLE001
            log.error("internal error handling request:\n%s", traceback.format_exc())
            reply = protocol.make_error(rid, ServiceError(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}"))
        try:
            protocol.dumps(reply)
        except (TypeError, ValueError) as exc:
            reply = protocol.make_error(rid, ServiceError(ErrorCode.INTERNAL, f"unserialisable result: {exc}"))
        self._out.put(("reply", ident, reply))
        self._wake()

    def stop(self, reason: str = "") -> None:
        self._shutdown_reason = reason
        self._stop.set()

    def _teardown(self) -> None:
        log.info("service stopping (%s)", self._shutdown_reason or "stop requested")
        if self.ops.active() is not None:
            self.ops.abort_all("service shutting down", float(self.backend_cfg["reconcile_wait_s"]))
        if self.read_only:
            log.info("read-only mode: shutdown cleanup skipped (nothing was written)")
            problems = []
        else:
            problems = self.host.cleanup_all(self.cfg.data.get("cleanup"))
        if problems:
            log.error("cleanup problems at shutdown: %s", problems)
        self.host.shutdown()
        self._pool.shutdown(wait=False, cancel_futures=True)
        for s in (self._router, self._pub, self._wake_pull):
            try:
                s.close(0)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._ctx.destroy(linger=0)
        except Exception:  # noqa: BLE001
            pass
        self.accounts.close()

    def _watchdog(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self.lease.check_expiry()
            except Exception:  # noqa: BLE001
                log.exception("lease watchdog")

    # ================================================================ reconcile
    def _reconcile(self, reason: str) -> dict:
        if self.host.run_cancel is not None:
            self.host.run_cancel.cancel(f"lease ended: {reason}")
        wait_s = float(self.backend_cfg["reconcile_wait_s"])
        stopped = self.ops.abort_all(f"lease ended: {reason}", wait_s)
        problems = []
        if not stopped:
            problems.append(f"an operation did not stop within {wait_s} s")
        problems += self.host.cleanup_all(self.cfg.data.get("cleanup"))
        self.host.run_active = False
        self.host.run_info = None
        self.host.run_devices = None
        report = {"ok": stopped and not problems, "stopped": stopped, "problems": problems}
        if problems:
            log.error("reconcile (%s): %s", reason, problems)
        else:
            log.info("reconcile (%s): operations stopped, cleanup done", reason)
        return report

    # ================================================================ dispatch
    def dispatch(self, req: dict) -> Any:
        req.setdefault("_deadline", time.monotonic() + req["timeout_s"])
        CancelToken(deadline=req["_deadline"]).raise_if_cancelled()
        method = req["method"]
        entry = self._methods.get(method)
        if entry is None:
            raise ServiceError(ErrorCode.UNKNOWN_METHOD, f"unknown method {method!r}")
        level, fn = entry
        if self.read_only and (level == "lease" or method in _RO_FORBIDDEN):
            raise ServiceError(ErrorCode.FORBIDDEN, "the service runs in read-only diagnostics mode: "
                               "control and writes are disabled")
        session = None
        if level != "public":
            session = self.sessions.get(req["session"])
            if level == "admin" and not session.is_admin:
                raise ServiceError(ErrorCode.FORBIDDEN, "administrator role required")
            if level == "lease":
                req["_lease_cancel"] = self.lease.request_token(req["session"])
        params = req["params"]
        mutating = method in {"device.write", "device.connect", "device.disconnect",
                              "device.reconnect", "op.start"}
        command = params.get("command") if method == "device.command" else params.get("name")
        if method == "device.command":
            dev = self.host.device(_str(params, "device"))
            info = dev.commands.get(command)
            mutating = bool(info and info.mutating)
        if mutating:
            if self.read_only:
                raise ServiceError(ErrorCode.FORBIDDEN, "read-only diagnostics mode")
            with self._control_lock:
                req["_lease_cancel"] = self.lease.request_token(req["session"])
                device = _str(params, "device")
                self._refuse_during_run(params, device, command, params.get("args"),
                                       allow_stop=(command == "stop"))
                req["_mutation_cancel"] = self._request_cancel(
                    session, params, req, deadline=(method != "op.start"))
                key = protocol.new_id()
                self._inflight[key] = self._resources(device, command, params.get("args"))
            try:
                return fn(session, params, req)
            finally:
                with self._control_lock:
                    self._inflight.pop(key, None)
        if method == "run.begin":
            with self._control_lock:
                if self._inflight:
                    raise ServiceError(ErrorCode.BUSY, "a device command is still finishing")
                return fn(session, params, req)
        return fn(session, params, req)

    def _register(self) -> None:
        m = self._methods
        m["server.hello"] = ("public", self._hello)
        m["server.status"] = ("auth", lambda s, p, r: self.status_public())
        m["server.shutdown"] = ("public", self._shutdown)
        m["server.sim_fault"] = ("lease", self._sim_fault)
        m["auth.login"] = ("public", self._login)
        m["auth.bootstrap"] = ("public", self._bootstrap)
        m["auth.logout"] = ("auth", self._logout)
        m["auth.whoami"] = ("auth", lambda s, p, r: {"user": s.user, "session": s.public()})
        m["auth.change_password"] = ("auth", self._change_password)
        m["auth.list_users"] = ("admin", lambda s, p, r: self.accounts.list_users())
        m["auth.create_user"] = ("admin", self._create_user)
        m["auth.update_user"] = ("admin", self._update_user)
        m["auth.sessions"] = ("admin", lambda s, p, r: self.sessions.list())
        m["session.heartbeat"] = ("auth", self._heartbeat)
        m["lease.acquire"] = ("auth", lambda s, p, r: self.lease.acquire(s.token, s.public()))
        m["lease.release"] = ("auth", self._lease_release)
        m["lease.status"] = ("auth", lambda s, p, r: self.lease.status())
        m["lease.force_release"] = ("admin", lambda s, p, r: self.lease.force(
            f"forced by administrator {s.user['username']}"))
        m["device.list"] = ("auth", lambda s, p, r: self.host.summary())
        m["device.info"] = ("auth", lambda s, p, r: self.host.device(_str(p, "device")).info())
        m["device.read"] = ("auth", self._read)
        m["device.write"] = ("lease", self._write)
        m["device.command"] = ("auth", self._command)
        m["device.connect"] = ("lease", lambda s, p, r: self._device_action(s, p, r, "connect", self.host.connect))
        m["device.disconnect"] = ("lease", self._disconnect)
        m["device.reconnect"] = ("lease", lambda s, p, r: self._device_action(s, p, r, "reconnect",
                                                                             self.host.reconnect))
        m["op.start"] = ("lease", self._op_start)
        m["op.pause"] = ("lease", lambda s, p, r: self._op_ctl(p, "pause"))
        m["op.resume"] = ("lease", lambda s, p, r: self._op_ctl(p, "resume"))
        m["op.abort"] = ("lease", lambda s, p, r: self._op_ctl(p, "abort"))
        m["op.wait"] = ("auth", self._op_wait)
        m["op.status"] = ("auth", lambda s, p, r: self.ops.get(_str(p, "op_id")).summary())
        m["op.list"] = ("auth", lambda s, p, r: self.ops.list())
        m["run.begin"] = ("lease", self._run_begin)
        m["run.end"] = ("lease", self._run_end)
        m["prefs.get"] = ("auth", lambda s, p, r: self.accounts.get_prefs(s.user_id))
        m["prefs.set"] = ("auth", lambda s, p, r: self.accounts.set_prefs(s.user_id, p.get("values")))
        m["presets.list"] = ("auth", lambda s, p, r: self.accounts.list_presets(s.user_id, p.get("workflow")))
        m["presets.save"] = ("auth", lambda s, p, r: self.accounts.save_preset(
            s.user_id, _str(p, "workflow"), _str(p, "name"), p.get("value")))
        m["presets.delete"] = ("auth", lambda s, p, r: self.accounts.delete_preset(
            s.user_id, _str(p, "workflow"), _str(p, "name")))
        m["config.get"] = ("auth", self._config_get)

    # ================================================================ handlers
    def _hello(self, s, p, r) -> dict:
        return {
            "protocol": PROTOCOL,
            "software": software_revision(),
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "mode": self.cfg.mode,
            "read_only": self.read_only,
            "config_status": self.cfg.status,
            "config_problems": self.cfg.problems,
            "started": self.started,
            "needs_bootstrap": self.accounts.user_count() == 0,
            "rpc_port": self.rpc_port,
            "event_port": self.event_port,
        }

    def status_public(self) -> dict:
        active = self.ops.active()
        return {"mode": self.cfg.mode, "instance_id": self.instance_id, "lease": self.lease.status(),
                "devices": self.host.summary(), "run_active": self.host.run_active,
                "run_info": self.host.run_info, "active_operation": active.summary() if active else None,
                "config_status": self.cfg.status, "config_problems": self.cfg.problems}

    def _shutdown(self, s, p, r) -> dict:
        token = p.get("control_token")
        allowed = bool(self.control_token) and isinstance(token, str) and secrets.compare_digest(
            token, self.control_token)
        if not allowed:
            session = self.sessions.get(r["session"])
            if not session.is_admin:
                raise ServiceError(ErrorCode.FORBIDDEN, "only the managing Samba_controls or an "
                                   "administrator can stop the service")
        if (self.ops.active() is not None or self.host.run_active) and not p.get("force"):
            raise ServiceError(ErrorCode.BUSY, "a measurement is running; stop it first (or force)")
        threading.Timer(0.2, self.stop, args=("shutdown requested",)).start()
        return {"stopping": True}

    def _sim_fault(self, s, p, r) -> dict:
        """Inject a simulated fault (simulation mode only; used by tests and demos)."""
        if self.sim_physics is None:
            raise ServiceError(ErrorCode.NOT_SUPPORTED, "fault injection exists only in simulation mode")
        key = _str(p, "key")
        self.sim_physics.fail_next[key] = int(p.get("count", 1))
        return {"key": key, "count": self.sim_physics.fail_next[key]}

    def _login(self, s, p, r) -> dict:
        user = self.accounts.verify(p.get("username"), p.get("password"))
        session = self.sessions.create(user, str(p.get("client", "client")))
        log.info("login: %s (%s)", user["username"], session.client)
        return {"session": session.token, "user": user, "prefs": self.accounts.get_prefs(user["id"])}

    def _bootstrap(self, s, p, r) -> dict:
        if self.accounts.user_count() != 0:
            raise ServiceError(ErrorCode.FORBIDDEN, "accounts already exist; ask an administrator")
        user = self.accounts.create_user(p.get("username"), p.get("password"), "admin", p.get("full_name", ""))
        log.info("first administrator account created: %s", user["username"])
        return user

    def _logout(self, s, p, r) -> dict:
        if self.lease.is_holder(s.token):
            if self.ops.active() is not None or self.host.run_active:
                raise ServiceError(ErrorCode.BUSY, "stop the running measurement before logging out")
            self.lease.release(s.token, "logout", wait=True)
        self.sessions.drop(s.token)
        return {"logged_out": True}

    def _change_password(self, s, p, r) -> dict:
        self.accounts.change_password(s.user_id, p.get("old_password"), p.get("new_password"))
        return {"changed": True}

    def _create_user(self, s, p, r) -> dict:
        return self.accounts.create_user(p.get("username"), p.get("password"), p.get("role", "operator"),
                                         p.get("full_name", ""))

    def _update_user(self, s, p, r) -> dict:
        uid = int(p.get("user_id"))
        if "role" in p:
            self.accounts.set_role(uid, p["role"])
        if "disabled" in p:
            self.accounts.set_disabled(uid, bool(p["disabled"]))
            if p["disabled"]:
                self.sessions.drop_user(uid)
        if "password" in p:
            self.accounts.set_password(uid, p["password"])
        user = self.accounts.get_user(uid)
        self.sessions.update_user(user)
        return user

    def _heartbeat(self, s, p, r) -> dict:
        holder = self.lease.heartbeat(s.token)
        return {"lease_holder": holder, "lease": self.lease.status(), "instance_id": self.instance_id}

    def _lease_release(self, s, p, r) -> dict:
        if self.host.run_active and not p.get("force"):
            raise ServiceError(ErrorCode.BUSY, "end the measurement run first")
        return self.lease.release(s.token, "released by holder", wait=True)

    def _read(self, s, p, r) -> dict:
        attrs = p.get("attrs")
        if isinstance(attrs, str):
            attrs = [attrs]
        if not isinstance(attrs, list) or not attrs:
            raise ServiceError(ErrorCode.BAD_REQUEST, "attrs must be a non-empty list")
        return self.host.read(_str(p, "device"), attrs, min(r["timeout_s"], 60.0))

    def _write(self, s, p, r) -> Any:
        self._refuse_during_run(p, p.get("device"))
        return {"value": self.host.write(_str(p, "device"), _str(p, "attr"), p.get("value"),
                                        r["timeout_s"], self._request_cancel(s, p, r))}

    def _command(self, s, p, r) -> Any:
        dev = self.host.device(_str(p, "device"))
        name = _str(p, "command")
        info = dev.commands.get(name)
        if info is None:
            raise ServiceError(ErrorCode.UNKNOWN_COMMAND, f"{dev.name} has no command {name!r}")
        if info.mutating:
            if self.read_only:
                raise ServiceError(ErrorCode.FORBIDDEN, "read-only diagnostics mode: commands that change "
                                   "the instruments are disabled")
            r["_lease_cancel"] = self.lease.request_token(s.token)
            self._refuse_during_run(p, dev.name, name, p.get("args"), allow_stop=(name == "stop"))
        cancel = (self._request_cancel(s, p, r) if info.mutating
                  else CancelToken(deadline=r["_deadline"]))
        ctx = CallContext(cancel=cancel, host=self.host, session_user=s.user["username"])
        return self.host.command(dev.name, name, p.get("args") or {}, ctx)

    def _resources(self, device: str, command: str | None = None, args: dict | None = None) -> set[str]:
        """Resolve logical endpoints to the instruments their mutations touch."""
        dev = self.host.device(device)
        if dev.PHYSICAL:
            return {device}
        if dev.KIND == "magnet":
            return {dev.daq_name}
        if dev.KIND == "moke":
            args = args or {}
            if command == "prepare":
                return {dev.hall_name}
            return ({dev.daq_name}
                    | ({dev.hall_name} if args.get("hall", True) else set())
                    | ({dev.lockin_name} if args.get("lockin", True) else set()))
        return {device}

    def _run_scope(self, p: dict) -> bool:
        run_id = p.get("_run_id")
        if run_id is None:
            return False
        if not self.host.run_active or run_id != self.host.run_id:
            raise ServiceError(ErrorCode.INVALID_STATE, "this run has ended or was replaced")
        return True

    def _request_cancel(self, s, p, r, deadline: bool = True) -> CancelToken:
        if "_mutation_cancel" in r:
            token = r["_mutation_cancel"]
            token.raise_if_cancelled()
            return token
        parent = r.get("_lease_cancel") or self.lease.request_token(s.token)
        if self._run_scope(p):
            parent = self.host.run_cancel
        token = CancelToken(parent=parent, deadline=r["_deadline"] if deadline else None)
        token.raise_if_cancelled()
        return token

    def _refuse_during_run(self, p: dict, device: str | None, command: str | None = None,
                           args: dict | None = None, allow_stop: bool = False) -> None:
        own_run = self._run_scope(p)
        if allow_stop:
            return
        h, active = self.host, self.ops.active()
        resources = self._resources(device, command, args) if device else set()
        if own_run and active is None:
            if h.run_devices is not None and not resources.issubset(h.run_devices):
                raise ServiceError(ErrorCode.BUSY, "the run did not reserve these instruments")
            return
        if h.run_active:
            if h.run_devices is not None and resources and resources.isdisjoint(h.run_devices):
                return
        elif active is None:
            return
        raise ServiceError(ErrorCode.BUSY, f"a measurement is running and uses {device}")

    def _disconnect(self, s, p, r):
        if self.host.run_active or self.ops.active() is not None:
            raise ServiceError(ErrorCode.BUSY, "a measurement is running")
        return self._device_action(s, p, r, "disconnect", self.host.disconnect)

    def _device_action(self, s, p, r, action: str, fn) -> dict:
        """Run a manual connect / disconnect / reconnect and log who asked and what came of it
        (the automatic connection at start is logged as one summary line)."""
        name = _str(p, "device")
        self._refuse_during_run(p, name)
        cancel = self._request_cancel(s, p, r)
        who = f"{s.user['username']} ({s.client})"
        try:
            info = fn(name, timeout_s=r["timeout_s"], cancel=cancel)
        except ServiceError as exc:
            log.warning("%s %s by %s failed: %s: %s", action, name, who, exc.code, exc.message)
            raise
        except Exception as exc:
            log.warning("%s %s by %s failed: %s", action, name, who, exc)
            raise
        ident = info.get("identity") or {}
        detail = ", ".join(f"{k} {ident[k]}" for k in ("model", "serial", "resource", "firmware") if ident.get(k))
        log.info("%s %s by %s: %s%s", action, name, who, info.get("state"), f" ({detail})" if detail else "")
        return info

    def _op_start(self, s, p, r) -> dict:
        dev = self.host.device(_str(p, "device"))
        info, bound = dev.bind_args(dev.operations, _str(p, "name"), p.get("args"))
        self._refuse_during_run(p, dev.name, info.name, bound)
        op = Operation(dev.name, info.name, bound, s.token, s.user["username"])
        op.cancel = self._request_cancel(s, p, r, deadline=False)
        ctx = CallContext(cancel=op.cancel, host=self.host, operation=op, session_user=s.user["username"])
        dev.require_usable()
        self.ops.start(op, lambda o: dev.run_operation(info.name, bound, ctx))
        return op.summary()

    def _op_ctl(self, p, action: str) -> dict:
        if self.host.run_active and not self._run_scope(p):
            raise ServiceError(ErrorCode.BUSY, "control this operation through its run")
        op = self.ops.get(_str(p, "op_id"))
        getattr(op, action)()
        return op.summary()

    def _op_wait(self, s, p, r) -> dict:
        op = self.ops.get(_str(p, "op_id"))
        timeout = min(float(p.get("timeout_s", 1.0)), max(0.0, r["timeout_s"] - 0.5), 30.0)
        result = op.wait(int(p.get("since_seq", 0)), timeout, p.get("known_state"))
        # Waiting on the lease holder's operation also counts as a heartbeat.
        self.lease.heartbeat(s.token)
        return result

    def _run_begin(self, s, p, r) -> dict:
        if self.host.run_active or self.ops.active() is not None:
            raise ServiceError(ErrorCode.BUSY, "a run or operation is already active")
        required = p.get("required_devices") or []
        missing = []
        for name in required:
            d = self.host.device(name)
            if hasattr(d, "refresh_state"):
                d.refresh_state()
            if not d.usable:
                missing.append(f"{name}: {d.state.value} ({d.status})")
        if missing:
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE,
                               "required devices are not available: " + "; ".join(missing),
                               {"missing": missing})
        self.host.run_devices = {str(n) for n in required} if required else None
        self.host.run_id = protocol.new_id()
        self.host.run_cancel = self._request_cancel(s, {}, r, deadline=False)
        self.host.run_active = True
        self.host.run_info = {"user": s.user["username"], "workflow": p.get("workflow"),
                              "started": time.time(), "file": p.get("file"),
                              "devices": sorted(self.host.run_devices) if self.host.run_devices else None}
        self.publish("server", self.status_public())
        return {"run_active": True, "mode": self.cfg.mode, "run_id": self.host.run_id}

    def _run_end(self, s, p, r) -> dict:
        if not self._run_scope(p):
            raise ServiceError(ErrorCode.BUSY, "end the run through its owning client")
        if self.ops.active() is not None:
            raise ServiceError(ErrorCode.BUSY, "an operation is still running")
        self.host.run_cancel.cancel("run ended")
        problems = []
        if p.get("cleanup", True):
            problems = self.host.cleanup_all(self.cfg.data.get("cleanup"))
        if problems:
            self.lease.release(s.token, "run cleanup failed", wait=False)
            return {"run_active": True, "cleanup_problems": problems}
        self.host.run_active = False
        self.host.run_info = None
        self.host.run_devices = None
        self.publish("server", self.status_public())
        return {"run_active": False, "cleanup_problems": problems}

    def _config_get(self, s, p, r) -> dict:
        return {"path": str(self.cfg.path), "status": self.cfg.status, "problems": self.cfg.problems,
                "data": self.cfg.snapshot(), "mode": self.cfg.mode}


#: auth-level methods that change state and are refused in read-only mode
_RO_FORBIDDEN = {"lease.acquire", "lease.force_release"}


def _str(params: dict, key: str) -> str:
    v = params.get(key)
    if not isinstance(v, str) or not v:
        raise ServiceError(ErrorCode.BAD_REQUEST, f"parameter {key!r} must be a non-empty string")
    return v
