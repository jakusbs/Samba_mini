"""GUI smoke tests (offscreen): windows build, log in, run through the UI."""

import json

import numpy as np
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
    w.trajectory.set_params("time", {"duration": 1.0, "interval": 0.05, "acq_time": 0.01, "time_const": 0.1,
                                     "position_stage": False})
    assert w.trajectory.estimate()[0] == 20
    # Complete the initial offscreen paint before measuring the event loop.
    qapp.processEvents()
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
    assert w.engine.result["status"] == "completed", w.engine.result
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, f"Expected multiple GUI timer ticks during the scan, got {len(ticks)}"
    assert max(gaps) < 0.5, f"GUI blocked for {max(gaps):.2f} s"
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


_KEEP: list = []


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
    w.panel._timer.stop()
    _KEEP.append(w)      # deleting it mid-session lets queued Qt callbacks hit freed widgets
    monkeypatch.setattr(w.bridge, "probe", lambda *a, **k: None)
    started = []
    monkeypatch.setattr(w.proc, "start", lambda mode=None, extra_args=None: started.append(mode))
    return w, started


def test_controls_starts_only_real_and_offers_import_without_config(qapp, samba_home, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from core.config import machine
    from Samba_controls.panels import admin as admin_panel

    src = _fake_minimoke(tmp_path)
    before = {p: p.stat().st_mtime for p in src.rglob("*")}
    answers = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: answers.pop(0))
    monkeypatch.setattr(admin_panel, "choose_minimoke_folder", lambda parent: str(src))

    w, started = _controls_without_service(monkeypatch)
    assert not hasattr(w, "mode_combo")                          # no simulation choice in the window
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
    # the window never starts a simulation, whatever the configuration file says
    data = machine.load().data
    data["backend"]["mode"] = "simulation"
    machine.save(data)
    w.start_service()
    assert started == ["real", "real", "real"]
    w.bridge.close()


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


ROUND_TRIP = {"b_min": -0.3, "b_max": 0.45, "b_step": 0.015, "sweep_freq": 0.05, "num_sweeps": 3, "x": 12.5,
              "y": -7.25, "position_stage": False, "volt": 0.75, "sensi": 2e-4, "lockin_freq": 2345.6,
              "time_const": 0.2, "phase": 12.5, "acq_time": 0.3, "x_min": -20.0, "x_max": 30.0, "x_step": 2.5,
              "y_min": 5.0, "y_max": 15.0, "y_step": 0.5, "b": 0.12, "repeat_num": 2, "field_settle_s": 0.4,
              "settle_time": 0.15, "record_hall": False, "duration": 12.0, "interval": 0.25}


def test_trajectory_layout_round_trips_every_minimoke_workflow(qapp):
    """Samba's Spatial/Field layout must hand the engine exactly MiniMoke's parameters."""
    from core.scan.workflows import WORKFLOWS
    from Samba_mini.panels.trajectory import TrajectoryPanel

    t = TrajectoryPanel()
    for key, wf in WORKFLOWS.items():
        values = wf.coerce({p.name: ROUND_TRIP[p.name] for p in wf.params})
        assert values != wf.coerce(wf.defaults())
        t.set_workflow(key)
        t.set_params(key, values)
        assert t.current == key
        got = wf.coerce(t.params())
        assert set(got) == set(values), key
        for name, v in values.items():
            if name == "repeat_num":          # no Repeats field: repeats are done with the Scanlist
                assert got[name] == 1
                continue
            assert got[name] == pytest.approx(v), (key, name, got[name], v)
    # each measurement keeps its own lock-in / timing values (MiniMoke remembered them per procedure)
    t.set_workflow("b_sweep_lockin")
    t.set_params("b_sweep_lockin", {"time_const": 0.001})
    t.set_workflow("time")
    assert t.params()["time_const"] == pytest.approx(0.2)
    t.set_workflow("b_sweep_lockin")
    assert t.params()["time_const"] == pytest.approx(0.001)
    # Samba's rule: the ticked axes choose the spatial measurement, none = time scan
    t.set_workflow("time")
    t.x_grp.scan_cb.setChecked(True)
    assert t.current == "x_sweep"
    t.y_grp.scan_cb.setChecked(True)
    assert t.current == "xy_sweep"
    t.x_grp.scan_cb.setChecked(False)
    assert t.current == "y_sweep"
    t.y_grp.scan_cb.setChecked(False)
    assert t.current == "time"
    t.type_btns[1].click()
    assert t.current in ("b_sweep_lockin", "b_sweep_dc")
    t.rb_dc.click()
    assert t.current == "b_sweep_dc" and not t.li_box.isEnabled() and t.dc_box.isEnabled()
    # polar setup measures PMOKE only
    t.set_setup("polar")
    assert t.metadata()["incidence"] == "PMOKE" and not t.meta.incidence_combo.isEnabled()


def test_right_panel_rows_plot_only(qapp):
    from Samba_mini.panels.right_panel import RightPanel

    r = RightPanel()
    r.configure_for("b_sweep_lockin")
    axes = {m["name"]: m["axis"] for m in r.sensors_meta()}
    assert axes["lockin_x"] == "Y1" and axes["dc_signal"] == "Y2" and axes["hall_field"] == "—"
    r.configure_for("b_sweep_dc")                       # no lock-in channels without the lock-in
    assert all(m["axis"] == "—" for m in r.sensors_meta() if m["name"].startswith("lockin"))
    assert {row.channel() for row in r.rows} == {"dc_signal", "intensity", "hall_field"}


def test_scanlist_runs_n_scans_with_field_flip(qapp, service, tmp_path):
    """Start on the Scanlist tab runs the Trajectory measurement N times (ABBA field flip)
    and writes Samba's scanlist .txt; each scan is its own file."""
    from core.gui.bridge import ServiceBridge
    from core.storage.hdf5_writer import read_file
    from Samba_mini.app import MainWindow

    service.client("boot").bootstrap(*ADMIN)
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    bridge.probe()
    bridge.client.login(*ADMIN)
    w._after_login()
    w.dir_edit.setText(str(tmp_path / "data"))
    w.trajectory.set_workflow("time")
    w.trajectory.set_params("time", {"duration": 0.1, "interval": 0.02, "acq_time": 0.01, "time_const": 0.1,
                                     "volt": 0.0, "b": 0.2, "position_stage": False, "record_hall": True})
    w.scanlist.n_scans.setValue(3)
    w.scanlist.field_flip.setChecked(True)
    w.scanlist.order_btn.click()                       # AB -> ABBA
    w.scanlist.name.setText("flip test")
    w.scanlist.name.textEdited.emit("flip test")
    w.bottom_tabs.setCurrentWidget(w.scanlist)
    w.start_run()
    assert w._sl is not None
    assert wait_until(qapp, lambda: w._sl is None and not w.running, 120)
    txt = tmp_path / "data" / "ScanLists_Longitudinal" / "flip test.txt"
    lines = txt.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "# polarity: relay_flip=0 field_flip=1 order=ABBA"
    rows = [ln.split("\t") for ln in lines if not ln.startswith("#")]
    assert len(rows) == 3 and all(r[1] == "+1" for r in rows)
    signs = [float(r[2]) > 0 for r in rows]            # simulated Hall follows the coil: A B B
    assert signs == [True, False, False], rows
    phases = [read_file(r[0])["metadata"]["scanlist_phase"] for r in rows]
    assert phases == ["A", "B", "B"]
    coils = [json.loads(read_file(r[0])["metadata"]["params_json"])["b"] for r in rows]
    assert coils == [0.2, -0.2, -0.2]
    w.close()


def test_bd_calibration_per_setup_reaches_runs(qapp, service, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from core.gui.bridge import ServiceBridge
    from core.storage.hdf5_writer import read_file
    from Samba_mini.app import MainWindow

    service.client("boot").bootstrap(*ADMIN)
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    bridge.probe()
    bridge.client.login(*ADMIN)
    w._after_login()
    w.dir_edit.setText(str(tmp_path / "data"))
    cal = [-4.0, -2.5, -0.5, 1.0, 2.5, 4.5]
    w.bd_panel.load_calibration(cal)
    w._bd_save(w.bd_panel.get_calibration())
    assert w.plot1d._kerr_cal.ok                        # θ display follows the calibration
    w.set_setup("polar")
    assert w.bd_panel.get_calibration() == [0.0] * 6    # the other setup has none saved
    w.set_setup("longitudinal")
    assert w.bd_panel.get_calibration() == cal
    w.trajectory.set_workflow("time")
    w.trajectory.set_params("time", {"duration": 0.04, "interval": 0.02, "acq_time": 0.01, "time_const": 0.1,
                                     "volt": 0.0, "position_stage": False})
    w.bottom_tabs.setCurrentWidget(w.trajectory)
    w.start_run()
    assert wait_until(qapp, lambda: w.engine is not None and w.engine.result is not None, 60)
    assert w.engine.result["status"] == "completed"
    assert list(read_file(w.engine.result["path"])["data"]["calibration"]["values"]) == cal
    # Samba's prompt: a new sample offers a fresh calibration
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    w.trajectory.meta.meta_sample.setText("NewSample")
    w.trajectory.meta.meta_sample.editingFinished.emit()
    assert w.bd_panel.get_calibration() == [0.0] * 6 and w.bottom_tabs.currentWidget() is w.bd_panel
    w.close()


def test_digit_jog_moves_by_the_clicked_digit(qapp, service):
    """Samba's per-digit jog: ▲/▼ of a digit jogs by that digit's unit (mm, MiniMoke's steps)."""
    from core.gui.bridge import ServiceBridge
    from Samba_mini.app import MainWindow

    service.client("boot").bootstrap(*ADMIN)
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    bridge.probe()
    bridge.client.login(*ADMIN)
    w._after_login()
    bridge.client.acquire_lease()
    jx = w.calibration.jogs["x"]
    assert wait_until(qapp, lambda: jx._pos is not None, 20)       # read after login
    start = jx._pos
    assert [d.text() for d in jx._digits] == list(f"{abs(round(start, 5)):08.5f}".replace(".", ""))
    jx._buttons[2 * 1].click()                                       # ▲ of the 1 mm digit
    assert wait_until(qapp, lambda: abs(jx._pos - (start + 1.0)) < 1e-4, 30)
    jx._buttons[2 * 4 + 1].click()                                   # ▼ of the 0.001 mm digit
    assert wait_until(qapp, lambda: abs(jx._pos - (start + 0.999)) < 1e-4, 30)
    jx.edit.setText("0.25")
    jx.edit.returnPressed.emit()                                     # typed absolute target
    assert wait_until(qapp, lambda: abs(jx._pos - 0.25) < 1e-4, 30)
    assert jx.um.text() == "= +250.00 µm"
    w.set_setup("polar")                    # the other stage: no jogging until its position is read
    assert not any(b.isEnabled() for b in w.calibration.jogs["x"]._buttons)
    assert wait_until(qapp, lambda: w.calibration.jogs["x"]._pos is not None, 20)
    w.close()


def test_data_browser_opens_a_running_xy_map_without_crashing(qapp, service, tmp_path):
    """Regression (2026-11-16): opening the Data Browser during an XY map closed Samba_mini
    (the map axes are stored per point; matplotlib got a 3-D extent and PyQt aborted)."""
    import numpy as np

    from core.gui.bridge import ServiceBridge
    from Samba_mini.app import MainWindow

    service.client("boot").bootstrap(*ADMIN)
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    bridge.probe()
    bridge.client.login(*ADMIN)
    w._after_login()
    w.dir_edit.setText(str(tmp_path / "data"))
    w.trajectory.set_workflow("xy_sweep")
    w.trajectory.set_params("xy_sweep", {"x_min": -20, "x_max": 20, "x_step": 10, "y_min": 0, "y_max": 30,
                                         "y_step": 10, "acq_time": 0.02, "time_const": 0.1, "repeat_num": 1})
    w.start_run()
    assert wait_until(qapp, lambda: w._progress[0] >= 3, 60)
    w.bottom_tabs.setCurrentWidget(w.browser)           # what the user did, mid-measurement
    w.browser.refresh()
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QTreeWidgetItemIterator
    it = QTreeWidgetItemIterator(w.browser.tree)
    running = None
    while it.value():
        fp = it.value().data(0, Qt.ItemDataRole.UserRole)
        if fp and str(fp) == str(w.engine.path):
            running = it.value()
        it += 1
    assert running is not None
    w.browser.tree.setCurrentItem(running)              # selecting the file being written
    for _ in range(5):
        qapp.processEvents()
    assert w.engine.result is None or w.engine.result["status"] == "completed"   # the run goes on
    assert wait_until(qapp, lambda: w.engine.result is not None, 120)
    assert w.engine.result["status"] == "completed"
    w.browser.refresh()
    path = w.engine.result["path"]
    sf = w.browser._get_scanfile(path)
    res = sf.read_2d("dc_signal")
    assert np.allclose(res["x_arr"], [-20, -10, 0, 10, 20]) and np.allclose(res["y_arr"], [0, 10, 20, 30])
    w.browser.plot.plot_2d(res["data"], res["x_arr"], res["y_arr"], "X", "Y", "DC")
    w.close()


def test_map_axes_from_partly_measured_setpoint_grid():
    from core.data_browser import _as_axis
    grid = np.full((4, 5), np.nan)
    grid[:, :2] = [[-20, -10]] * 4                       # only two columns measured so far
    assert np.allclose(_as_axis(grid, 5, columns=True), [-20, -10, 0, 10, 20])
    ys = np.full((4, 5), np.nan)
    ys[:2, 0] = [0, 10]
    assert np.allclose(_as_axis(ys, 4, columns=False), [0, 10, 20, 30])
    assert np.allclose(_as_axis(np.array([1.0, 2.0, 3.0]), 3, columns=True), [1, 2, 3])   # Samba's 1-D axes
    assert np.allclose(_as_axis(np.full((2, 3), np.nan), 3, columns=True), [0, 1, 2])    # nothing yet


def test_crash_guard_keeps_the_window_open(qapp, tmp_path, monkeypatch):
    """An exception escaping a Qt slot is logged and shown instead of aborting the application."""
    import sys

    from PyQt6.QtCore import QTimer

    import core.gui as gui
    monkeypatch.setenv("SAMBA_MINI_HOME", str(tmp_path / "home"))
    old_hook = sys.excepthook
    try:
        path = gui.install_crash_guard("guard_test")
        QTimer.singleShot(0, lambda: 1 / 0)
        for _ in range(20):
            qapp.processEvents()
        for h in __import__("logging").getLogger().handlers:
            h.flush()
        assert "ZeroDivisionError" in path.read_text(encoding="utf-8")
        assert gui._box_open
    finally:
        sys.excepthook = old_hook
        box = getattr(gui.handle_exception, "_box", None)
        if box is not None:
            box.close()
        root = __import__("logging").getLogger()
        for h in list(root.handlers):
            if getattr(h, "_samba_file", False):
                root.removeHandler(h)
                h.close()


def test_calibration_tab_measures_only_on_start(qapp, service, tmp_path):
    """Samba: Start while the Calibration tab is open runs the tab's own time scan; nothing is
    measured into its plot otherwise.  The file is named for the BD Calibration Fit & Import."""
    import os

    from core import bd_fit
    from core.gui.bridge import ServiceBridge
    from core.storage.hdf5_writer import read_file
    from Samba_mini.app import MainWindow

    service.client("boot").bootstrap(*ADMIN)
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    w.show()
    bridge.probe()
    bridge.client.login(*ADMIN)
    w._after_login()
    w.dir_edit.setText(str(tmp_path / "data"))
    w.live_tabs.setCurrentWidget(w.calibration)
    t0 = time.time()
    while time.time() - t0 < 2.5:                      # idle readbacks arrive about once a second
        qapp.processEvents()
        time.sleep(0.02)
    assert all(not v for v in w.calibration.plot.data.values())
    w.calibration.timescan.load({"n": 6, "interval": 0.05, "acq_time": 0.01})
    w.trajectory.set_params("time", {"time_const": 0.1, "volt": 0.0})
    w.start_run()
    assert w._calib_run
    assert wait_until(qapp, lambda: not w._calib_run, 60)       # the window has handled the whole run
    res = w.engine.result
    assert res["status"] == "completed" and res["points"] == 6
    assert bd_fit.is_time_calibration(res["path"]), os.path.basename(res["path"])
    meta = read_file(res["path"])["metadata"]
    assert json.loads(meta["params_json"])["position_stage"] is False and bool(meta["calibration_scan"])
    assert len(w.calibration.plot.data["dc_signal"]) == 6
    w.close()


def test_stage_can_be_moved_during_a_calibration_scan(qapp, service, tmp_path):
    """Focusing: during the calibration time scan the stage jog stays usable (the scan does not
    use the stage); during an X-sweep, which moves the stage, it is locked."""
    from core.gui.bridge import ServiceBridge
    from Samba_mini.app import MainWindow

    service.client("boot").bootstrap(*ADMIN)
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    w.show()
    bridge.probe()
    bridge.client.login(*ADMIN)
    w._after_login()
    w.dir_edit.setText(str(tmp_path / "data"))
    jx = w.calibration.jogs["x"]
    assert wait_until(qapp, lambda: jx._pos is not None, 20)
    start = jx._pos
    w.live_tabs.setCurrentWidget(w.calibration)
    w.calibration.timescan.load({"n": 80, "interval": 0.05, "acq_time": 0.01})
    w.trajectory.set_params("time", {"time_const": 0.1, "volt": 0.0})
    w.start_run()
    assert w._calib_run and w.calibration.stage_box.isEnabled()
    assert wait_until(qapp, lambda: w._progress[0] >= 5, 30)
    jx._buttons[2 * 1].click()                                        # ▲ 1 mm while the scan runs
    assert wait_until(qapp, lambda: abs(jx._pos - (start + 1.0)) < 1e-4, 30)
    assert w._calib_run, "the jog must not end the scan"
    assert wait_until(qapp, lambda: not w._calib_run, 60)
    assert w.engine.result["status"] == "completed"
    # an X-sweep moves the stage itself: the controls are locked while it runs
    w.live_tabs.setCurrentWidget(w.plot1d)
    w.trajectory.set_workflow("x_sweep")
    w.trajectory.set_params("x_sweep", {"x_min": 0, "x_max": 30, "x_step": 10, "acq_time": 0.01,
                                        "time_const": 0.1})
    w.start_run()
    assert not w.calibration.stage_box.isEnabled()
    w.abort_run()
    assert wait_until(qapp, lambda: w.start_btn.isEnabled(), 60)      # the window has handled the end
    assert w.calibration.stage_box.isEnabled()
    w.close()


def test_calibration_plot_time_axis_follows_the_data(qapp):
    """The calibration plot's time axis grows with the recorded data instead of spanning the
    planned duration (a 10000-sample scan squeezed its first minutes into a sliver)."""
    from Samba_mini.panels.calibration import ScanPlot

    p = ScanPlot()
    p.show()
    p.begin(500.0, "long scan")
    for t in (0.0, 0.2, 0.4, 0.6):
        p.add(t, {"balanced_V": 0.01 + t, "intensity_V": 1.0})
    p._redraw(force=True)
    lo, hi = p.x_limits()
    assert lo == 0.0 and 0.6 <= hi < 1.0
    for t in (5.0, 10.0):
        p.add(t, {"balanced_V": 0.02, "intensity_V": 1.0})
    p._redraw(force=True)
    assert 10.0 <= p.x_limits()[1] < 11.0
    p.close()
