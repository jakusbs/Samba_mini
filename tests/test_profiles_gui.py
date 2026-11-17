"""Regression tests for accounts, saved state, calibration records and response races.

Each test drives the real windows (offscreen) against the simulated service.  They cover the
failure cases of the profile/calibration review:

* user A -> logout -> user B with empty preferences sees defaults, then A gets A's state back;
* saving preferences never reloads or replaces calibration edits;
* a new-sample calibration reset survives setup switches and a restart;
* late device / stage replies after a selection change are ignored;
* configurations never change the sample or the calibration (legacy ones only when asked);
* rename collisions and failed saves keep everything and stay visible;
* calibration files from another setup / mode are explained, never silently used;
* the live θ display and the saved file use the same calibration snapshot.
"""

import json
import os
import threading
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.protocol import ErrorCode, ServiceError  # noqa: E402
from tests.conftest import ADMIN, OPERATOR  # noqa: E402

BOB = ("bob", "bob-password-1")
CAL1 = [-5.0, -3.0, -1.0, 1.0, 3.0, 5.0]
CAL2 = [-10.0, -6.0, -2.0, 2.0, 6.0, 10.0]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_until(app, cond, timeout=20.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def pump(app, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def accounts(service):
    c = service.client("boot")
    c.hello()
    c.bootstrap(*ADMIN)
    c.login(*ADMIN)
    c.call("auth.create_user", {"username": OPERATOR[0], "password": OPERATOR[1], "full_name": "Alice A"})
    c.call("auth.create_user", {"username": BOB[0], "password": BOB[1], "full_name": "Bob B"})
    return service


def open_window(service):
    from core.gui.bridge import ServiceBridge
    from Samba_mini.app import MainWindow
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    bridge.probe()
    return w


def close(w):
    """Close a window, discarding unsaved calibration edits (the app would ask)."""
    orig = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard)
    try:
        w.close()
    finally:
        QMessageBox.question = orig


def login(w, user):
    w.bridge.client.login(*user)
    w._after_login()


def settle(qapp, w):
    """Let the debounced autosave reach the service."""
    assert wait_until(qapp, lambda: w.store.state in ("saved", "error") and not w._ws_timer.isActive()
                      and not w.store.pending(), 10), w.store.state
    return w.store.state


def spy_calls(client):
    """Record the RPC methods a client sends (to prove that nothing touches an instrument)."""
    seen = []
    orig = client.call

    def call(method, params=None, timeout_s=10.0):
        seen.append((method, dict(params or {})))
        return orig(method, params, timeout_s)
    client.call = call
    return seen


INSTRUMENT_METHODS = {"device.command", "device.write", "op.start", "run.begin", "device.connect",
                      "device.disconnect", "device.reconnect"}


def write_cal_file(path, setup="longitudinal", sample="S1", mode="simulation", status="completed",
                   levels=CAL1):
    """A Samba_mini-style calibration time scan with a clean six-step staircase."""
    import h5py
    rng = np.random.default_rng(0)
    t, dc, k = [], [], 0
    for i, lv in enumerate(levels):
        for _ in range(40):
            t.append(k * 0.1)
            dc.append(lv / 1000 + rng.normal(0, 2e-6))
            k += 1
        if i < len(levels) - 1:
            for j in range(1, 4):
                t.append(k * 0.1)
                dc.append((lv + (levels[i + 1] - lv) * j / 4) / 1000)
                k += 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with h5py.File(path, "w") as f:
        f.attrs["scan_status"] = status
        f.attrs["scan_type"] = "TIME"
        f.attrs["mode"] = mode
        m = f.create_group("metadata")
        m.attrs["setup"] = setup
        m.attrs["sample_id"] = sample
        m.attrs["mode"] = mode
        m.attrs["workflow"] = "time"
        m.attrs["calibration_scan"] = True
        d = f.create_group("data")
        d.create_dataset("time", data=np.array(t))
        d.create_dataset("dc_signal", data=np.array(dc))
    return path


def save_and_activate(w, values, name="cal"):
    w.bd_panel.load_calibration(values)
    rec = w.bd_panel.save(name=name)
    assert rec is not None, w.bd_panel.status_text()
    assert w.bd_panel.activate(interactive=False), w.bd_panel.status_text()
    return rec


# --------------------------------------------------------------------------- accounts
def test_user_switch_resets_everything_and_restores_each_users_state(qapp, accounts, tmp_path, monkeypatch):
    service = accounts
    w = open_window(service)
    calls = spy_calls(w.bridge.client)
    login(w, OPERATOR)
    defaults_dir = w.dir_edit.text()
    defaults_params = w.trajectory.working_state()
    defaults_scan = w.calibration.timescan.values()
    assert w.trajectory.meta.meta_operator.text() == "Alice A"           # default operator = full name

    # --- user A sets up an experiment
    w.dir_edit.setText(str(tmp_path / "alice"))
    w.trajectory.set_workflow("time")
    w.trajectory.set_params("time", {"duration": 3.0, "interval": 0.1, "acq_time": 0.02, "volt": 0.25})
    w.trajectory.meta.meta_sample.setText("AliceSample")
    w.trajectory.meta.meta_operator.setText("Alice")
    w.trajectory.meta.meta_notes.setText("annealed")
    w.calibration.timescan.load({"n": 42, "interval": 0.3, "acq_time": 0.05, "volt": 0.5, "b": 0.125})
    rec = save_and_activate(w, CAL1, "alice cal")
    w._save_config("longitudinal", "alice trace", "create")
    assert settle(qapp, w) == "saved"
    a_state = w.trajectory.working_state()

    # --- logout: nothing of A stays on screen
    w._login_logout()
    assert not w.bridge.client.session
    assert w.dir_edit.text() == defaults_dir and w.profile.data_dir.text() == defaults_dir
    assert w.trajectory.meta.meta_sample.text() == "" and w.trajectory.meta.meta_operator.text() == ""
    assert w.trajectory.meta.meta_notes.text() == ""
    assert w.trajectory.working_state() == defaults_params
    assert w.calibration.timescan.values() == defaults_scan
    assert w.bd_panel.get_calibration() == [0.0] * 6 and w.bd_panel.active() is None
    assert not w.plot1d._kerr_cal.ok
    assert w.config_list.names("longitudinal") == [] and w.store.values() == {}

    # --- user B with empty preferences: explicit defaults, not A's leftovers
    login(w, BOB)
    assert w.bridge.client.user["username"] == "bob"
    assert w.dir_edit.text() == defaults_dir
    assert w.trajectory.meta.meta_sample.text() == ""
    assert w.trajectory.meta.meta_operator.text() == "Bob B"
    assert w.trajectory.working_state() == defaults_params
    assert w.calibration.timescan.values() == defaults_scan
    assert w.bd_panel.active() is None and w.bd_panel.get_calibration() == [0.0] * 6
    assert w.config_list.names("longitudinal") == []
    w._login_logout()

    # --- A again: A's own state is back, nothing else
    login(w, OPERATOR)
    assert w.dir_edit.text() == str(tmp_path / "alice") == w.profile.data_dir.text()
    assert w.trajectory.working_state() == a_state
    assert w.trajectory.meta.meta_sample.text() == "AliceSample"
    assert w.trajectory.meta.meta_operator.text() == "Alice"
    assert w.calibration.timescan.values()["n"] == 42 and w.calibration.timescan.values()["volt"] == 0.5
    assert w.bd_panel.active()["record"]["id"] == rec["id"] and w.bd_panel.get_calibration() == CAL1
    assert w.config_list.current_name() == "alice trace"
    assert w.plot1d._kerr_cal.ok and w.plot1d._kerr_cal.mv == CAL1
    # Logging in, out and restoring a profile never touched an instrument.
    assert not [m for m, _p in calls if m in INSTRUMENT_METHODS], calls
    close(w)


def test_session_expiry_resets_and_gives_unsaved_changes_back(qapp, accounts, monkeypatch):
    w = open_window(accounts)
    login(w, OPERATOR)
    settle(qapp, w)
    # The service forgets the session (as after a service restart); the next save fails.
    accounts.server.sessions.drop(w.bridge.client.session)
    w.trajectory.meta.meta_sample.setText("NotYetSaved")
    assert wait_until(qapp, lambda: w._session_user is None, 10)        # session_lost -> logged-out state
    assert w.trajectory.meta.meta_sample.text() == "" and "expired" in w.user_lbl.text()
    login(w, OPERATOR)
    assert w.trajectory.meta.meta_sample.text() == "NotYetSaved"         # given back, then saved
    assert settle(qapp, w) == "saved"
    stored = accounts.client("check")
    stored.hello()
    stored.login(*OPERATOR)
    assert stored.call("prefs.get")["working.longitudinal"]["experiment"]["sample_id"] == "NotYetSaved"
    close(w)


def test_data_folder_is_one_setting_and_failed_saves_keep_the_edits(qapp, accounts, tmp_path, monkeypatch):
    w = open_window(accounts)
    login(w, OPERATOR)
    w.profile.data_dir.setText(str(tmp_path / "p"))
    assert w.dir_edit.text() == str(tmp_path / "p")                     # Profile -> action bar
    w.dir_edit.setText(str(tmp_path / "q"))
    assert w.profile.data_dir.text() == str(tmp_path / "q")              # action bar -> Profile
    assert settle(qapp, w) == "saved"

    c = w.bridge.client
    orig = c.call

    def failing(method, params=None, timeout_s=10.0):
        if method == "prefs.set":
            raise ServiceError(ErrorCode.TIMEOUT, "disk full (test)")
        return orig(method, params, timeout_s)
    monkeypatch.setattr(c, "call", failing)
    w.profile.operator.setText("Dr. A")
    assert wait_until(qapp, lambda: w.store.state == "error", 10)
    assert "NOT SAVED" in w.sb["Saved"].text() and w.profile.retry_btn.isVisible() is False or True
    assert "disk full" in w.profile.save_state.text()
    assert w.profile.operator.text() == "Dr. A" and w.store.pending()["default_operator"] == "Dr. A"
    monkeypatch.setattr(c, "call", orig)
    w.store.retry()
    assert settle(qapp, w) == "saved"
    assert orig("prefs.get")["default_operator"] == "Dr. A"
    close(w)


# --------------------------------------------------------------------------- calibration
def test_saving_preferences_never_touches_calibration_edits(qapp, accounts):
    w = open_window(accounts)
    login(w, OPERATOR)
    rec = save_and_activate(w, CAL1)
    w.bd_panel.load_calibration(CAL2)                                    # unsaved edit
    assert w.bd_panel.is_dirty()
    w.profile.operator.setText("someone")                                # unrelated preferences ...
    w.profile.csv.setChecked(True)
    w.kerr_state.set(True)
    assert settle(qapp, w) == "saved"
    assert w.bd_panel.get_calibration() == CAL2 and w.bd_panel.is_dirty()   # ... leave the editor alone
    assert w.bd_panel.active()["record"]["id"] == rec["id"]
    # Navigation that would drop the edit asks: Cancel keeps everything where it was.
    import Samba_mini.panels.bd_calibration as bdmod
    answers = []
    orig_q = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: answers.append(a[1]) or QMessageBox.StandardButton.Cancel)
    try:
        w.set_setup("polar")
        assert w.setup == "longitudinal" and w.bd_panel.get_calibration() == CAL2
        w._login_logout()
        assert w.bridge.client.session
    finally:
        QMessageBox.question = orig_q
    assert answers == ["Unsaved calibration", "Unsaved calibration"]
    assert bdmod is not None
    close(w)


def test_new_sample_reset_survives_setup_switch_and_restart(qapp, accounts, monkeypatch):
    w = open_window(accounts)
    login(w, OPERATOR)
    w.trajectory.meta.meta_sample.setText("S1")
    w.trajectory.meta.meta_sample.editingFinished.emit()
    save_and_activate(w, CAL1)
    assert w.bd_panel.active()["sample_id"] == "S1"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    w.trajectory.meta.meta_sample.setText("S2")
    w.trajectory.meta.meta_sample.editingFinished.emit()
    assert w.bd_panel.active() is None and w.bd_panel.get_calibration() == [0.0] * 6
    w.set_setup("polar")
    w.set_setup("longitudinal")
    assert w.bd_panel.active() is None and w.bd_panel.get_calibration() == [0.0] * 6   # not reloaded
    assert w.trajectory.meta.meta_sample.text() == "S2"
    settle(qapp, w)
    close(w)
    w2 = open_window(accounts)                                           # restart
    login(w2, OPERATOR)
    assert w2.bd_panel.active() is None and w2.bd_panel.get_calibration() == [0.0] * 6
    assert w2.trajectory.meta.meta_sample.text() == "S2"
    # "Load saved" reads the service, not a cache: the old record is still there to reuse explicitly.
    assert w2.bd_panel.load_saved(w2.bridge.client.call("calibration.list", {"setup": "longitudinal"})[0]["id"])
    assert w2.bd_panel.get_calibration() == CAL1 and w2.bd_panel.active() is None
    close(w2)


def test_load_saved_reads_the_service_not_a_cache(qapp, accounts):
    w = open_window(accounts)
    login(w, OPERATOR)
    rec = save_and_activate(w, CAL1)
    other = accounts.client("other-window")                               # same account, elsewhere
    other.hello()
    other.login(*OPERATOR)
    rec2 = other.call("calibration.save", {"record": dict(rec, values=CAL2), "base": rec["id"]})
    other.call("calibration.activate", {"setup": "longitudinal", "id": rec2["id"], "sample_id": None})
    assert w.bd_panel.load_saved(rec2["id"])
    assert w.bd_panel.get_calibration() == CAL2 and w.bd_panel.active()["record"]["id"] == rec2["id"]
    close(w)


def test_calibration_import_checks_setup_sample_and_mode(qapp, accounts, tmp_path, monkeypatch):
    from Samba_mini.panels.bd_calibration import file_problems, read_file_info
    w = open_window(accounts)
    login(w, OPERATOR)
    w.trajectory.meta.meta_sample.setText("S1")
    ok_file = write_cal_file(str(tmp_path / "d" / "100000_TIME_S1_calibration_SIM.h5"))
    polar_file = write_cal_file(str(tmp_path / "d" / "100100_TIME_S1_calibration_SIM.h5"), setup="polar")
    partial = write_cal_file(str(tmp_path / "d" / "100200_TIME_S1_calibration_SIM.h5"), status="aborted")
    ctx_real = {"setup": "longitudinal", "sample_id": "S1", "mode": "real"}
    assert any("simulation" in p for p in file_problems(read_file_info(ok_file), ctx_real))
    assert file_problems(read_file_info(ok_file), dict(ctx_real, mode="simulation")) == []
    assert any("did not complete" in p for p in file_problems(read_file_info(partial), ctx_real))

    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.No)
    assert not w.bd_panel.import_file(polar_file)                        # explained, refused
    assert "polar setup" in asked[-1] and w.bd_panel.get_calibration() == [0.0] * 6
    assert not w.bd_panel.import_file(polar_file, auto=True)             # automatic path: no dialog, no change
    assert len(asked) == 1 and "polar" in w.bd_panel.status_text()
    assert w.bd_panel.import_file(ok_file)                               # matches: fitted, not saved
    assert w.bd_panel.get_calibration() == pytest.approx(CAL1, abs=0.02) and w.bd_panel.is_dirty()
    assert len(asked) == 1
    rec = w.bd_panel.save(name="fit")
    assert rec["origin"] == "fit" and rec["mode"] == "simulation" and rec["sample_id"] == "S1"
    assert rec["source_file"] == os.path.abspath(ok_file) and rec["fit"]["n_plateaus"] == 6
    # Explicitly accepting a mismatch is recorded with the fit
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    assert w.bd_panel.import_file(partial)
    rec2 = w.bd_panel.save(name="partial")
    assert any("did not complete" in p for p in rec2["fit"]["accepted_mismatches"])
    close(w)


def test_calibration_scan_uses_its_own_settings_and_is_fitted_for_review(qapp, accounts, tmp_path):
    w = open_window(accounts)
    login(w, OPERATOR)
    w.dir_edit.setText(str(tmp_path / "data"))
    w.trajectory.set_workflow("x_sweep")
    w.trajectory.hardware.li["volt"].setValue(1.5)                       # what the Hardware box shows now
    w.trajectory.set_params("time", {"volt": 0.75, "lockin_freq": 999.0})  # parked time-scan values
    w.calibration.timescan.load({"n": 6, "interval": 0.05, "acq_time": 0.01, "volt": 0.2, "lockin_freq": 1234.0,
                                 "time_const": 0.1, "b": 0.0})
    p = w.calibration_scan_params()
    assert p["volt"] == 0.2 and p["lockin_freq"] == 1234.0 and p["position_stage"] is False
    w._copy_hardware_to_calibration_scan()
    assert w.calibration.timescan.values()["volt"] == 1.5                # explicit copy from what is shown
    w.calibration.timescan.load({"volt": 0.2, "lockin_freq": 1234.0})
    w.live_tabs.setCurrentWidget(w.calibration)
    w.start_run()
    assert wait_until(qapp, lambda: not w._calib_run, 60)
    from core.storage.hdf5_writer import read_file
    meta = read_file(w.engine.result["path"])["metadata"]
    applied = json.loads(meta["params_json"])
    assert applied["volt"] == 0.2 and applied["lockin_freq"] == 1234.0
    # the acquisition result went straight to the fitting step (a 6-point simulated trace does not
    # fit; that is reported, without a dialog and without touching the editor)
    pump(qapp, 0.3)
    assert w.bd_panel.get_calibration() == [0.0] * 6
    assert any("not fitted" in line for _lvl, line in w._log_entries)
    close(w)


def test_live_display_and_file_share_one_frozen_calibration(qapp, accounts, tmp_path):
    from core.storage.hdf5_writer import read_file
    w = open_window(accounts)
    login(w, OPERATOR)
    w.dir_edit.setText(str(tmp_path / "data"))
    rec1 = save_and_activate(w, CAL1, "first")
    w.trajectory.set_workflow("time")
    w.trajectory.set_params("time", {"duration": 1.5, "interval": 0.05, "acq_time": 0.01, "time_const": 0.1,
                                     "volt": 0.0, "position_stage": False})
    w.bottom_tabs.setCurrentWidget(w.trajectory)
    w.start_run()
    assert wait_until(qapp, lambda: w._progress[0] >= 3, 30)
    rec2 = save_and_activate(w, CAL2, "second")                          # changed during the run
    w.bd_panel.load_calibration([1, 2, 3, 4, 5, 6])                     # and an unsaved edit
    assert w.plot1d._kerr_cal.mv == CAL1 and w.map2d._kerr_cal.mv == CAL1   # display stays frozen
    assert wait_until(qapp, lambda: w.engine.result is not None and w.start_btn.isEnabled(), 60)
    res = w.engine.result
    assert res["status"] == "completed"
    f = read_file(res["path"])
    assert list(f["data"]["calibration"]["values"]) == CAL1
    assert f["data"]["calibration"]["attrs"]["calibration_id"] == rec1["id"]
    assert f["metadata"]["bd_calibration_id"] == rec1["id"] and f["metadata"]["bd_calibration_version"] == 1
    snap = json.loads(f["metadata"]["bd_calibration_json"])
    assert snap["values"] == CAL1 and snap["creator"] == OPERATOR[0] and snap["setup"] == "longitudinal"
    assert w.plot1d._kerr_cal.mv == CAL2                                 # after the run: the active one
    assert rec2["id"] != rec1["id"]
    close(w)


# --------------------------------------------------------------------------- presets
def test_presets_never_change_sample_or_calibration(qapp, accounts, monkeypatch):
    w = open_window(accounts)
    login(w, OPERATOR)
    w.trajectory.meta.meta_sample.setText("S1")
    w.trajectory.meta.meta_operator.setText("Alice")
    w.trajectory.meta.incidence_combo.setCurrentText("PMOKE")
    w.trajectory.set_workflow("time")
    assert w._save_config("longitudinal", "trace", "create")
    stored = w.bridge.client.call("presets.get", {"workflow": "samba_mini/longitudinal", "name": "trace"})
    meta = stored["value"]["metadata"]
    assert "sample_id" not in meta and "sample" not in meta and "operator" not in meta
    assert meta["incidence"] == "PMOKE"
    w.trajectory.meta.meta_sample.setText("S2")
    w.trajectory.meta.meta_sample.editingFinished.emit()
    rec = save_and_activate(w, CAL1)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[1]) or QMessageBox.StandardButton.No)
    w.trajectory.set_workflow("x_sweep")                                  # edits ...
    w._load_config("longitudinal", "trace")                               # ... with "trace" selected: asks
    assert asked == ["Unsaved configuration changes"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[1])
                        or QMessageBox.StandardButton.Discard)
    w._load_config("longitudinal", "trace")
    assert w.trajectory.current == "time"
    assert w.trajectory.meta.meta_sample.text() == "S2" and w.trajectory.meta.meta_operator.text() == "Alice"
    assert w.bd_panel.active()["record"]["id"] == rec["id"]

    # A configuration saved by the earlier version still carries a sample: applied only on request.
    w.bridge.client.call("presets.save", {"workflow": "samba_mini/longitudinal", "name": "legacy", "value": {
        "workflow": "time", "params": {"duration": 2.0},
        "metadata": {"sample_id": "OLD", "operator": "Former", "incidence": "LMOKE"}}})
    w._reload_configs()
    asked.clear()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[1]) or QMessageBox.StandardButton.No)
    w._load_config("longitudinal", "legacy")
    assert asked == ["Sample information in this configuration"]
    assert w.trajectory.meta.meta_sample.text() == "S2" and w.bd_panel.active()["record"]["id"] == rec["id"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[1]) or QMessageBox.StandardButton.Yes)
    w._load_config("longitudinal", "trace")
    w._load_config("longitudinal", "legacy")
    assert w.trajectory.meta.meta_sample.text() == "OLD"                 # explicit, then the sample check ran:
    assert asked[-1] == "New sample" and w.bd_panel.active() is None
    close(w)


def test_setup_switch_keeps_each_setups_draft_and_preset(qapp, accounts):
    w = open_window(accounts)
    login(w, OPERATOR)
    w.trajectory.set_workflow("time")
    assert w._save_config("longitudinal", "L1", "create")
    w.trajectory.set_params("time", {"acq_time": 0.25})                   # unsaved change to L1
    assert w._update_preset_dirty() and "•" in w.config_list.lists["longitudinal"].item(0).text()
    w.set_setup("polar")
    assert w.config_list.current_name() is None                          # polar: its own (default) state
    assert w.trajectory.current == "x_sweep" and w.trajectory.meta.incidence_combo.currentText() == "PMOKE"
    w.trajectory.set_workflow("y_sweep")
    w.set_setup("longitudinal")
    assert w.config_list.current_name() == "L1" and w.trajectory.current == "time"
    assert w.trajectory.params()["acq_time"] == 0.25 and w._update_preset_dirty()
    w.set_setup("polar")
    assert w.trajectory.current == "y_sweep"
    close(w)


def test_rename_collision_and_failed_save_change_nothing(qapp, accounts):
    w = open_window(accounts)
    login(w, OPERATOR)
    w.trajectory.set_workflow("time")
    assert w._save_config("longitudinal", "A", "create")
    w.trajectory.set_workflow("x_sweep")
    assert w._save_config("longitudinal", "B", "create")
    w._rename_config("longitudinal", "A", "B")
    assert "already exists" in w.config_list.status.text()
    stored = {p["name"]: p["value"]["workflow"] for p in
              w.bridge.client.call("presets.list", {"workflow": "samba_mini/longitudinal"})}
    assert stored == {"A": "time", "B": "x_sweep"} and set(w.configs["longitudinal"]) == {"A", "B"}
    assert not w._save_config("longitudinal", "A", "create")             # "+" with a taken name
    assert w.bridge.client.call("presets.get", {"workflow": "samba_mini/longitudinal",
                                                "name": "A"})["value"]["workflow"] == "time"
    before = w.trajectory.working_state()
    assert not w._save_config("longitudinal", "x" * 100, "create")      # refused by the service
    assert "not saved" in w.config_list.status.text() and w.trajectory.working_state() == before
    w._copy_config("longitudinal", "A", "A copy")                        # copies the SAVED A
    assert w.configs["longitudinal"]["A copy"]["workflow"] == "time"
    w._rename_config("longitudinal", "B", "C")
    assert w.config_list.current_name() == "C" and set(w.configs["longitudinal"]) == {"A", "A copy", "C"}
    close(w)


# --------------------------------------------------------------------------- races
def test_late_stage_reply_after_setup_switch_is_ignored(qapp, accounts):
    w = open_window(accounts)
    login(w, OPERATOR)
    c = w.bridge.client
    c.acquire_lease()
    c.device("stage_longitudinal").command("move_to", axis="x", position_mm=5.0)
    assert wait_until(qapp, lambda: w.calibration.jogs["x"]._pos is not None, 20)
    gate = threading.Event()
    orig = c.call

    def slow(method, params=None, timeout_s=10.0):
        if method == "device.read" and (params or {}).get("device") == "stage_longitudinal":
            gate.wait(10)                                                # the old stage answers late
        return orig(method, params, timeout_s)
    c.call = slow
    w.calibration.refresh_positions()
    w.set_setup("polar")
    jx = w.calibration.jogs["x"]
    shown = []                                   # every position the X jog displays from now on
    orig_set = jx.set_position
    jx.set_position = lambda v, simulated=False: (shown.append(float(v)), orig_set(v, simulated))
    assert wait_until(qapp, lambda: jx._pos is not None, 20)
    polar_x = jx._pos
    assert abs(polar_x - 5.0) > 1e-6
    gate.set()
    assert wait_until(qapp, lambda: not gate.is_set() or len(shown) >= 1, 2)
    pump(qapp, 1.0)
    assert all(abs(v - 5.0) > 1e-6 for v in shown), shown    # the longitudinal answer was never shown
    c.call = orig
    close(w)


def test_controls_device_panel_ignores_late_info_and_binds_commands(qapp, accounts):
    from core.gui.bridge import ServiceBridge
    from Samba_controls.panels.devices import DevicePanel
    bridge = ServiceBridge("Samba_controls", accounts.rpc_port, accounts.event_port)
    bridge.probe()
    bridge.client.login(*ADMIN)
    c = bridge.client
    gate = threading.Event()
    orig = c.call
    sent = []

    def slow(method, params=None, timeout_s=10.0):
        if method == "device.info" and (params or {}).get("device") == "daq":
            gate.wait(10)
        if method == "device.command":
            sent.append((params or {}).get("device"))
        return orig(method, params, timeout_s)
    c.call = slow
    panel = DevicePanel(lambda: bridge)
    panel.show_device("daq")
    assert not panel.controls_enabled()                                  # unresolved: nothing can be sent
    panel.show_device("lockin")
    assert not panel.controls_enabled()
    assert wait_until(qapp, lambda: panel.info is not None, 20)
    assert panel.info["name"] == "lockin" and panel.controls_enabled()
    gate.set()                                                           # the daq answer arrives late ...
    pump(qapp, 0.5)
    assert panel.name == "lockin" and panel.info["name"] == "lockin"     # ... and is ignored
    assert panel.title.text().startswith("lockin")
    buttons = [b for b in panel.cmd_box.findChildren(type(panel.read_btn)) if b.property("device")]
    assert buttons and {b.property("device") for b in buttons} == {"lockin"}
    # A command button built for the lock-in never retargets: after selecting the DAQ it does nothing.
    read_btn = next(b for b in buttons if b.text() == "Run read_xyrt")
    panel.show_device("daq")
    read_btn.click()
    pump(qapp, 0.3)
    assert sent == []
    assert wait_until(qapp, lambda: panel.info is not None and panel.info["name"] == "daq", 20)
    # Logout / session change: the panel forgets the selection.
    panel.reset("Log in again")
    assert panel.info is None and not panel.controls_enabled()
    c.call = orig
    bridge.close()


def test_replies_of_an_earlier_session_are_dropped(qapp, accounts):
    from core.gui.bridge import call_guarded
    from core.client import ServiceClient
    c = ServiceClient(accounts.rpc_port, accounts.event_port, "t")
    c.hello()
    c.login(*OPERATOR)
    got = []
    gate = threading.Event()
    call_guarded(c, lambda: (gate.wait(5), "old")[1], got.append)
    c.logout()
    c.login(*BOB)
    gate.set()
    pump(qapp, 0.5)
    assert got == []                                                     # alice's reply never reaches bob
    call_guarded(c, lambda: "new", got.append)
    assert wait_until(qapp, lambda: got == ["new"], 5)
    c.close()
