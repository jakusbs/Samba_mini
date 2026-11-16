"""GUI smoke tests (offscreen): windows build, log in, run through the UI."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from tests.conftest import ADMIN  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def wait_until(app, cond, timeout=30.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def test_samba_mini_runs_measurement(qapp, service, tmp_path, monkeypatch):
    from core.gui.bridge import ServiceBridge
    from Samba_mini.app import MainWindow

    c = service.client("boot")
    c.bootstrap(*ADMIN)
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    w.show()
    bridge.probe()
    bridge.client.login(*ADMIN)
    w._after_login()
    w.dir_edit.setText(str(tmp_path / "data"))
    w.trajectory.set_workflow("time")
    w.trajectory.set_params("time", {"duration": 0.3, "interval": 0.05, "acq_time": 0.01, "time_const": 0.1,
                                     "position_stage": False})
    assert w.trajectory.estimate()[0] == 6
    # Responsiveness: the GUI event loop must keep ticking while a run is active.
    from PyQt6.QtCore import QTimer
    ticks = []
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    w.start_run()
    assert wait_until(qapp, lambda: w.engine is not None and w.engine.result is not None, 60)
    timer.stop()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps and max(gaps) < 0.5, f"GUI blocked for {max(gaps):.2f} s"
    assert wait_until(qapp, lambda: w.start_btn.isEnabled(), 10)
    assert w.engine.result["status"] == "completed", w.engine.result
    assert os.path.exists(w.engine.result["path"])
    # named configuration round trip through the service
    w._save_config("longitudinal", "fast trace")
    w._reload_configs()
    assert "fast trace" in w.configs["longitudinal"]
    w.close()


def _fake_minimoke(root):
    src = root / "MiniMoke"
    (src / "configs").mkdir(parents=True)
    (src / "configs" / "instruments_config.local.ini").write_text(
        "[LockIn]\nresource = TCPIP0::192.168.77.2::50000::SOCKET\n")
    (src / "configs" / "procedures_config.ini").write_text(
        "[B-Sweep]\nb_max = 0.7\nb_min = -0.7\nb_step = 0.005\nnum_sweeps = 2\nsweep_freq = 0.05\n")
    return src


def _controls_without_service(monkeypatch):
    """Samba_controls window that sees no running service and records service starts."""
    import socket

    from core.gui.bridge import ServiceBridge
    from Samba_controls.app import ControlsWindow
    ports = []
    for _ in range(2):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
    w = ControlsWindow(ServiceBridge("test", *ports), start_service=False)
    w._timer.stop()
    monkeypatch.setattr(w.bridge, "probe", lambda *a, **k: None)
    started = []
    monkeypatch.setattr(w.proc, "start", lambda mode=None, extra_args=None: started.append(mode))
    return w, started


def test_controls_offers_minimoke_import_for_real_without_config(qapp, samba_home, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from core.config import machine
    from Samba_controls.panels import admin as admin_panel

    src = _fake_minimoke(tmp_path)
    before = {p: p.stat().st_mtime for p in src.rglob("*")}
    answers = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: answers.pop(0))
    monkeypatch.setattr(admin_panel, "choose_minimoke_folder", lambda parent: str(src))

    w, started = _controls_without_service(monkeypatch)
    w.mode_combo.setCurrentText("real")
    # declined: nothing written, service not started
    answers[:] = [QMessageBox.StandardButton.Cancel]
    w.start_service()
    assert started == [] and machine.load().status == "defaults"
    # accepted (offer, then the summary): configuration written in real mode, service started
    answers[:] = [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Yes]
    w.start_service()
    cfg = machine.load()
    assert started == ["real"] and cfg.status == "loaded" and cfg.mode == "real"
    assert cfg.data["lockin"]["resource"] == "TCPIP0::192.168.77.2::50000::SOCKET"
    assert "b_sweep_dc" in w._pending_presets                    # saved after the first login
    assert {p: p.stat().st_mtime for p in src.rglob("*")} == before     # MiniMoke untouched
    # once a configuration exists there is no offer
    answers[:] = []
    w.start_service()
    assert started == ["real", "real"]
    # simulation / mode from configuration never ask
    w2, started2 = _controls_without_service(monkeypatch)
    from core import paths
    paths.machine_config_path().unlink()
    w2.start_service()
    assert started2 == [None]
    for win in (w, w2):
        win.bridge.close()
        win.deleteLater()


def test_samba_mini_xy_map_ui(qapp, service, tmp_path):
    from core.gui.bridge import ServiceBridge
    from Samba_mini.app import MainWindow

    c = service.client("boot")
    c.bootstrap(*ADMIN)
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    w.show()
    bridge.probe()
    bridge.client.login(*ADMIN)
    w._after_login()
    w.dir_edit.setText(str(tmp_path / "data"))
    w.trajectory.set_workflow("xy_sweep")
    w.trajectory.set_params("xy_sweep", {"x_min": 0, "x_max": 20, "x_step": 10, "y_min": 0, "y_max": 20,
                                         "y_step": 10, "acq_time": 0.005, "time_const": 0.1})
    w.start_run()
    assert wait_until(qapp, lambda: w.engine.result is not None, 60)
    assert w.engine.result["status"] == "completed"
    import numpy as np
    assert np.isfinite(w._map_data["dc_signal"]).all()
    w.close()
