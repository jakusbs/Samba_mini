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
