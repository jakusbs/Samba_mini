"""Coil and lock-in between measurements.

* Measurements never configure the lock-in (only "Apply now" does) and never switch it off.
* The coil keeps running after a measurement: a field sweep returns to the value it started from,
  other measurements keep their coil command; ``cleanup.coil_after_run = "zero"`` restores 0 V.
* Releasing control leaves the coil / lock-in on only when the holder chose so; losing control
  (expired lease) always switches them off.
* Samba_mini asks before releasing with an output on, and its start message says exactly what a
  run will do.
"""

import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.scan.engine import Callbacks, RunRequest, ScanEngine  # noqa: E402
from core.storage.hdf5_writer import read_file  # noqa: E402
from tests.conftest import ADMIN, start_service  # noqa: E402

LOCKIN = dict(volt=0.0, sensi=1e-3, lockin_freq=1777.0, time_const=0.1, phase=0.0, acq_time=0.01)
TIME = dict(b=0.3, duration=0.2, interval=0.05, x=0, y=0, position_stage=False, record_hall=False, **LOCKIN)
DC_SWEEP = dict(b_min=-0.2, b_max=0.2, b_step=0.1, sweep_freq=2.0, num_sweeps=1, position_stage=False, x=0, y=0)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_until(app, cond, timeout=30.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def _login(svc, name="t"):
    c = svc.client(name)
    c.hello()
    if c.call("server.hello")["needs_bootstrap"]:
        c.bootstrap(*ADMIN)
    c.login(*ADMIN)
    return c


def _coil(c):
    return c.device("daq").read("coil_command_V")["coil_command_V"].value


def _osc(c):
    return c.device("lockin").read("oscillator_V")["oscillator_V"].value


def _run(c, tmp_path, workflow, params):
    logs = []
    req = RunRequest(workflow, params, save_dir=str(tmp_path), sample="S")
    res = ScanEngine(c, req, Callbacks(log=lambda lvl, m: logs.append(m))).run()
    return res, logs


def test_runs_keep_the_coil_and_never_touch_the_lockin(samba_home, tmp_path):
    svc = start_service(samba_home)
    try:
        c = _login(svc)
        c.acquire_lease()
        c.device("lockin").command("configure", volt=0.5, sensi=1e-3, freq=1777.0, time_const=0.1, phase=0.0)
        c.device("daq").command("set_outputs", coil_V=0.3)
        res, logs = _run(c, tmp_path, "time", dict(TIME))          # Hardware box: volt 0.0, coil 0.3 V
        assert res["status"] == "completed", res
        assert _coil(c) == pytest.approx(0.3)                       # still running
        assert _osc(c) == pytest.approx(0.5)                        # not reconfigured, not switched off
        meta = read_file(res["path"])["metadata"]
        assert json.loads(meta["lockin_settings_json"])["oscillator_V"] == pytest.approx(0.5)
        assert meta["coil_after_run_V"] == pytest.approx(0.3)
        assert any("oscillator_V 0.5 (Hardware box 0)" in m for m in logs)
        # a field sweep returns to where the coil was
        c.device("daq").command("set_outputs", coil_V=0.25)
        res, _ = _run(c, tmp_path, "b_sweep_dc", dict(DC_SWEEP))
        assert res["status"] == "completed", res
        assert _coil(c) == pytest.approx(0.25)
        assert _osc(c) == pytest.approx(0.5)                        # the sweep left the lock-in alone too
    finally:
        svc.stop()


def test_coil_after_run_zero_policy(samba_home, tmp_path):
    svc = start_service(samba_home, overrides={"cleanup": {"coil_after_run": "zero"}})
    try:
        c = _login(svc)
        c.acquire_lease()
        res, _ = _run(c, tmp_path, "time", dict(TIME))
        assert res["status"] == "completed" and _coil(c) == pytest.approx(0.0)
    finally:
        svc.stop()


def test_release_keeps_outputs_only_when_chosen(samba_home):
    svc = start_service(samba_home, lease_ttl_s=1.0)
    try:
        c = _login(svc, "holder")
        watcher = _login(svc, "watcher")
        c.acquire_lease()
        c.device("daq").command("set_outputs", coil_V=0.4)
        c.device("lockin").command("configure", volt=0.7, sensi=1e-3, freq=1777.0, time_const=0.1, phase=0.0)
        c.release_lease(keep_outputs=["coil", "lockin"])            # the holder chose to leave them on
        assert _coil(watcher) == pytest.approx(0.4) and _osc(watcher) == pytest.approx(0.7)
        c.acquire_lease()
        c.release_lease(keep_outputs=["coil"])                      # only the coil
        assert _coil(watcher) == pytest.approx(0.4) and _osc(watcher) == pytest.approx(0.0)
        c.acquire_lease()
        c.release_lease()                                           # the usual cleanup
        assert _coil(watcher) == pytest.approx(0.0)
        c.acquire_lease()
        c.device("daq").command("set_outputs", coil_V=0.4)
        c.stop_heartbeat()                                          # the controlling program is lost
        assert wait_until(QApplication.instance() or QApplication([]),
                          lambda: watcher.call("lease.status")["state"] == "FREE", 20)
        assert _coil(watcher) == pytest.approx(0.0)                 # nobody in control: switched off
    finally:
        svc.stop()


def test_samba_mini_asks_before_releasing_with_the_coil_on(qapp, samba_home, monkeypatch):
    from core.gui.bridge import ServiceBridge
    from Samba_mini.app import MainWindow
    svc = start_service(samba_home)
    try:
        _login(svc, "boot")
        bridge = ServiceBridge("Samba_mini", svc.rpc_port, svc.event_port)
        w = MainWindow(bridge, auto_login=False)
        bridge.probe()
        bridge.client.login(*ADMIN)
        w._after_login()
        c = bridge.client
        c.acquire_lease()
        c.device("daq").command("set_outputs", coil_V=0.4)
        assert wait_until(qapp, lambda: "ON" in w.sb["Coil"].text(), 10)     # visible in the status bar
        monkeypatch.delenv("SAMBA_MINI_KEEP_OUTPUTS", raising=False)
        asked = []
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Cancel)
        w._toggle_control()                                         # Release control -> Cancel
        assert c.lease_holder and "+0.4 V" in asked[-1]
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        w._toggle_control()                                         # -> Yes: released, coil left on
        assert not c.lease_holder and _coil(c) == pytest.approx(0.4)
        # the start message says what a run does -- and nothing about switching the lock-in on
        from core.scan.workflows import WORKFLOWS
        text = w.start_summary(WORKFLOWS["time"], dict(TIME))
        assert "stays at +0.3 V afterwards" in text and "switched on" not in text
        sweep = w.start_summary(WORKFLOWS["b_sweep_dc"], dict(DC_SWEEP))
        assert "returns to its present value" in sweep and "lock-in" not in sweep
        monkeypatch.setenv("SAMBA_MINI_KEEP_OUTPUTS", "no")
        w.close()
        bridge.close()
    finally:
        svc.stop()
