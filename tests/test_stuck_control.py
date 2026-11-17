"""Control of the setup must not get stuck (17 Nov, real setup).

A garbled 7270 reply marked the lock-in faulted; releasing control then could not confirm the
oscillator was off, so the lease stayed RECONCILING -- and a faulted device can only be
reconnected by the lease holder, so "Force release..." could not help either: only a service
restart did.  Now an administrator's forced release reconnects faulted devices first and, once
every operation has stopped, frees the setup even if a device's cleanup cannot be confirmed.

Separately, Samba_mini warned "The device service restarted" every 2 s after logging in to a
service that had restarted since its first hello: the client records the instance at login.
"""

import time

from core.client import ServiceClient
from core.model import DevState
from core.protocol import ErrorCode, ServiceError
from tests.conftest import ADMIN, start_service


def _admin(svc, name):
    c = svc.client(name)
    c.hello()
    if c.call("server.hello")["needs_bootstrap"]:
        c.bootstrap(*ADMIN)
    c.login(*ADMIN)
    return c


def _wait(cond, timeout=20.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        time.sleep(0.05)
    return cond()


def test_forced_release_reconnects_a_faulted_lockin_and_frees_the_setup(samba_home):
    svc = start_service(samba_home)
    try:
        holder, admin = _admin(svc, "holder"), _admin(svc, "admin")
        lockin = svc.server.host.device("lockin")
        holder.acquire_lease()
        lockin.set_fault("could not convert string to float: '9\\x03-9.734E-04,-1.5E-03'", "read oscillator_V")
        holder.release_lease()                                   # cleanup cannot reach the lock-in
        st = admin.call("lease.status")
        assert st["state"] == "RECONCILING"
        assert "lockin: not connected" in st["last_reconcile"]["problems"][0]
        try:
            admin.acquire_lease()
            raise AssertionError("acquired while RECONCILING")
        except ServiceError as exc:
            assert exc.code == ErrorCode.LEASE_RECONCILING
        st = admin.call("lease.force_release", {}, 120.0)
        assert st["state"] == "FREE", st
        rep = st["last_reconcile"]
        assert rep["reconnected"] == ["lockin"] and rep["problems"] == [] and "unconfirmed" not in rep
        assert lockin.state == DevState.ON
        assert lockin.read_attribute("oscillator_V").value == 0.0           # the cleanup ran
        admin.acquire_lease()                                                 # control can be taken
    finally:
        svc.stop()


def test_forced_release_frees_the_setup_even_if_a_device_stays_unreachable(samba_home, monkeypatch):
    svc = start_service(samba_home)
    try:
        holder, admin = _admin(svc, "holder"), _admin(svc, "admin")
        lockin = svc.server.host.device("lockin")

        def unreachable():
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, "7270 not reachable")
        monkeypatch.setattr(lockin, "_connect", unreachable)
        holder.acquire_lease()
        lockin.set_fault("cable pulled", "read")
        holder.release_lease()
        assert admin.call("lease.status")["state"] == "RECONCILING"
        st = admin.call("lease.force_release", {}, 120.0)
        assert st["state"] == "FREE", st                     # never blocked for good
        assert "lockin: not connected" in st["last_reconcile"]["unconfirmed"][0]   # but it is said
        admin.acquire_lease()
        # a plain release by the holder still does not hide an unconfirmed cleanup
        admin.release_lease()
        assert admin.call("lease.status")["state"] == "RECONCILING"
    finally:
        svc.stop()


def test_login_records_the_service_instance_no_false_restart_warnings(samba_home, monkeypatch):
    import core.client as client_mod
    monkeypatch.setattr(client_mod, "HEARTBEAT_INTERVAL_S", 0.1)
    svc = start_service(samba_home)
    try:
        _admin(svc, "boot")
        c = ServiceClient(svc.rpc_port, svc.event_port, "Samba_mini")
        seen = []
        c.on_connection_change = lambda what, info: seen.append(what)
        c.hello()
        c.instance_id = "instance-of-a-service-that-has-stopped"   # hello before a restart (17 Nov)
        c.login(*ADMIN)
        assert c.instance_id == svc.server.instance_id
        time.sleep(0.6)                                             # several heartbeats
        assert "restarted" not in seen, seen
        c.instance_id = "stale-again"                               # a stale id seen by a heartbeat
        assert _wait(lambda: c.instance_id == svc.server.instance_id, 5)
        assert "restarted" not in seen, seen
        c.stop_heartbeat()
        c.close()
    finally:
        svc.stop()


def test_samba_mini_stays_responsive_without_a_service(samba_home):
    """Reported 17 Nov: Samba_mini 'gets stuck' when Samba_controls is not started.  Its 2 s
    service check waited ~1.5 s for an answer on the GUI thread; it runs in the background now."""
    import os
    import socket
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from core.gui.bridge import ServiceBridge
    from Samba_mini.app import MainWindow
    qapp = QApplication.instance() or QApplication([])
    ports = []
    for _ in range(2):                                   # nothing listens there
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
    w = MainWindow(ServiceBridge("Samba_mini", *ports), auto_login=True)
    w.show()
    ticks = []
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 5.0:               # covers two or three service checks
        qapp.processEvents()
        time.sleep(0.005)
    timer.stop()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps and max(gaps) < 0.5, f"GUI blocked for {max(gaps):.2f} s"
    assert w.mode_badge.text() == "SERVICE OFFLINE"
    w._poll.stop()
    w.bridge.close()
    _KEEP_WINDOWS.append(w)                             # its last probe may still be on its way


_KEEP_WINDOWS: list = []
