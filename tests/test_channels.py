"""Channels as in Samba: named in Samba_controls, ticked = measured and saved, files under the names.

* Channel names (machine.toml ``[<device>.channels]``) are validated: unique, not used by the
  files themselves; datasets are named exactly as Samba names them (``_hdf5_key``).
* A run records only the ticked channels, each as ``/data/<name>`` with its id, device, unit,
  physical line and plot axis as attributes; the Hall probe / lock-in are not queried when none
  of their channels is ticked; no std values anywhere.
* The CSV export and the BD fit find channels by id, whatever they are called.
"""

import copy
import json
import os
import time

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.config import machine  # noqa: E402
from core.protocol import ServiceError  # noqa: E402
from core.scan import channels as chans  # noqa: E402
from core.scan.engine import Callbacks, RunRequest, ScanEngine  # noqa: E402
from core.storage.hdf5_writer import read_file  # noqa: E402
from tests.conftest import ADMIN, OPERATOR, start_service  # noqa: E402

FAST_LOCKIN = dict(volt=0.0, sensi=1e-3, lockin_freq=1000.0, time_const=0.001, phase=0.0)
TIME = dict(b=0.0, duration=0.2, interval=0.05, x=0, y=0, position_stage=False, record_hall=True, time_const=0.1,
            acq_time=0.01, **{k: v for k, v in FAST_LOCKIN.items() if k != "time_const"})
FOCUS = {"daq": {"channels": {"intensity": "Focus line"}}}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_until(app, cond, timeout=30.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def _run(client, tmp_path, workflow, params, channels):
    recs, logs = [], []
    req = RunRequest(workflow, params, save_dir=str(tmp_path), sample="S", channels=channels)
    eng = ScanEngine(client, req, Callbacks(point=lambda e: recs.append(e),
                                            log=lambda lvl, m: logs.append((lvl, m))))
    res = eng.run()
    return res, recs, logs


# --------------------------------------------------------------------------- names
def test_channel_names_are_validated_and_saved_like_samba():
    assert chans.dataset_key(" Focus line ") == "Focus_line"            # Samba's _hdf5_key rule
    assert chans.dataset_key("a/b\\c d") == "a_b_c_d" and chans.dataset_key("Lock-in θ") == "Lock-in_θ"
    d = copy.deepcopy(machine.DEFAULTS)
    assert machine.validate(d) == []
    names = {r["id"]: (r["name"], r["access"], r["line"]) for r in chans.channel_table(d)}
    assert names["dc_signal"] == ("BD", "r", "Dev1/ai0") and names["intensity"] == ("Intensity", "r", "Dev1/ai1")
    assert names["coil"] == ("Magnet", "rw", "Dev1/ao3")                # the magnet output: read/write
    assert "dc_std" not in names and "intensity_std" not in names       # no std channels
    d["daq"]["channels"]["intensity"] = "BD"                             # two channels saved as /data/BD
    d["lockin"]["channels"]["lockin_x"] = "Time"                         # used by the files themselves
    d["hall"]["channels"]["field2"] = "x"                                # no such channel
    probs = " | ".join(machine.validate(d))
    assert "names must differ" in probs and "used by the measurement files" in probs and "no such channel" in probs


def test_service_lists_channel_names_and_daq_has_no_std(samba_home):
    svc = start_service(samba_home, overrides=FOCUS)
    try:
        c = svc.client("t")
        c.hello()
        assert c.has_feature("channels-1")
        c.bootstrap(*ADMIN)
        c.login(*ADMIN)
        table = {r["id"]: r for r in c.call("channels.list")}
        assert table["intensity"]["name"] == "Focus line" and table["intensity"]["dataset"] == "Focus_line"
        assert table["intensity"]["line"] == "Dev1/ai1" and table["dc_signal"]["device_label"] == "NI-DAQ"
        assert table["coil"]["kind"] == "output" and table["lockin_x"]["kind"] == "sensor"
        c.acquire_lease()
        rec = c.device("moke").command("measure_point", coil_V=0.0, acq_time_s=0.01, hall=False, lockin=False)
        assert "balanced_V" in rec and not any("std" in k for k in rec)
    finally:
        svc.stop()


# --------------------------------------------------------------------------- engine
def test_run_saves_only_ticked_channels_under_their_names(samba_home, tmp_path):
    # The Hall probe is disabled: a run that does not tick its channel must not need it.
    svc = start_service(samba_home, overrides=dict(FOCUS, hall={"enabled": False}))
    try:
        c = svc.client("t")
        c.hello()
        c.bootstrap(*ADMIN)
        c.login(*ADMIN)
        table = {r["id"]: r for r in c.call("channels.list")}
        sel = [dict(table["dc_signal"], y_axis="Y1"), dict(table["intensity"], y_axis="hidden")]
        res, recs, _logs = _run(c, tmp_path, "time", dict(TIME), sel)
        assert res["status"] == "completed", res
        f = read_file(res["path"])
        data = f["data"]
        assert set(k for k, v in data.items() if v["attrs"].get("role") == "sensor") == {"BD", "Focus_line"}
        bd, fl = data["BD"], data["Focus_line"]
        assert np.isfinite(bd["values"]).all() and np.isfinite(fl["values"]).all()
        assert bd["attrs"]["channel"] == "dc_signal" and bd["attrs"]["label"] == "BD"
        assert fl["attrs"]["channel"] == "intensity" and fl["attrs"]["label"] == "Focus line"
        assert fl["attrs"]["line"] == "Dev1/ai1" and fl["attrs"]["y_axis"] == "hidden" and bd["attrs"]["unit"] == "V"
        assert "hall_integration" not in data and not any("std" in k for k in data)
        meta = f["metadata"]
        assert [c["dataset"] for c in json.loads(meta["channels_json"])] == ["BD", "Focus_line"]
        points = [e for e in recs if e["kind"] == "point"]
        assert points and all(not e["record"]["hall_measured"] for e in points)      # Hall never read
        assert all(not e["record"]["lockin_measured"] for e in points)               # lock-in never queried
        assert set(points[0]["values"]) == {"dc_signal", "intensity", "time"}        # live values by id
        # nothing ticked: refused with a clear message, nothing measured
        res2, _r, _l = _run(c, tmp_path, "time", dict(TIME), [])
        assert res2["status"] == "failed" and "tick at least one" in res2["message"]
    finally:
        svc.stop()


def test_b_sweep_averages_and_skips_unavailable_channels(samba_home, tmp_path):
    svc = start_service(samba_home)
    try:
        c = svc.client("t")
        c.hello()
        c.bootstrap(*ADMIN)
        c.login(*ADMIN)
        table = {r["id"]: r for r in c.call("channels.list")}
        params = dict(b_min=-0.2, b_max=0.2, b_step=0.1, sweep_freq=2.0, num_sweeps=2, position_stage=False,
                      x=0, y=0)
        sel = [table["dc_signal"], table["lockin_x"]]                    # no lock-in in a DC B-sweep
        res, _recs, logs = _run(c, tmp_path, "b_sweep_dc", params, sel)
        assert res["status"] == "completed", res
        data = read_file(res["path"])["data"]
        assert "BD" in data and "averaged/BD" in data and "Lock-in_X" not in data and "Hall_field" not in data
        assert np.isfinite(data["averaged/BD"]["values"]).all()
        assert data["averaged/BD"]["attrs"]["channel"] == "dc_signal"
        assert any("not measured by this measurement" in m and "Lock-in X" in m for _lvl, m in logs)
    finally:
        svc.stop()


# --------------------------------------------------------------------------- readers
def test_csv_and_bd_fit_find_channels_by_id(tmp_path):
    from core import bd_fit
    from core.storage.csv_export import export_csv
    path = tmp_path / "x_calibration.h5"
    levels = [-5.0, -3.0, -1.0, 1.0, 3.0, 5.0]
    dc = np.repeat(np.array(levels) / 1000, 40) + np.random.default_rng(0).normal(0, 2e-6, 240)
    t = np.arange(dc.size) * 0.1
    with h5py.File(path, "w") as f:
        f.attrs["scan_type"] = "TIME"
        d = f.create_group("data")
        d.create_dataset("time", data=t)
        k = d.create_dataset("Kerr", data=dc)            # the balanced diodes, renamed
        k.attrs["channel"] = "dc_signal"
        k.attrs["role"] = "sensor"
        fl = d.create_dataset("DC", data=np.full(dc.size, 2.0))   # another channel called "DC"
        fl.attrs["channel"] = "intensity"
        fl.attrs["role"] = "sensor"
        f.create_group("metadata")
    r = bd_fit.fit_file(str(path))
    assert r.ok and r.levels_mV == pytest.approx(levels, abs=0.02)        # the BD channel, not "DC"
    csv_path = export_csv(path)
    header = next(line for line in open(csv_path, encoding="utf-8") if not line.startswith("#")).strip()
    assert "Voltage DC (V)" in header and "Intensity (V)" in header and "STD" not in header
    row = next(line for line in list(open(csv_path, encoding="utf-8"))[1:] if line[0].isdigit() or line[0] == "-")
    cols = header.split(",")
    vals = row.strip().split(",")
    assert float(vals[cols.index("Intensity (V)")]) == 2.0


# --------------------------------------------------------------------------- GUI
def test_channels_tab_renames_and_samba_mini_measures_by_name(qapp, samba_home, tmp_path, monkeypatch):
    from core.gui.bridge import ServiceBridge
    from Samba_controls.panels.channel_editor import ChannelEditor
    from Samba_controls.panels.device_editor import DeviceEditor
    svc = start_service(samba_home)
    try:
        boot = svc.client("boot")
        boot.hello()
        boot.bootstrap(*ADMIN)
        boot.login(*ADMIN)
        boot.call("auth.create_user", {"username": OPERATOR[0], "password": OPERATOR[1]})
        bridge = ServiceBridge("Samba_controls", svc.rpc_port, svc.event_port)
        bridge.probe()
        bridge.client.login(*OPERATOR)
        ed = ChannelEditor(lambda: bridge)
        assert ed.refresh() and not ed.save_btn.isEnabled()              # operators: read-only
        bridge.client.logout()
        bridge.client.login(*ADMIN)
        assert ed.refresh() and ed.table.rowCount() == len(chans.ALL)
        ed.set_name("intensity", "BD")                                    # clash: refused before saving
        assert "names must differ" in ed.problems.text() and not ed.save_btn.isEnabled()
        ed.set_name("intensity", "Focus line")
        ed.set_name("lockin_x", "MOKE x1")
        assert ed.save_btn.isEnabled() and ed.save()
        dev = DeviceEditor(lambda: bridge)
        dev.refresh()
        dev.list.setCurrentRow(0)                                         # the DAQ: no channel keys here
        from PyQt6.QtWidgets import QWidget
        fields = {w.objectName() for w in dev.form_host.widget().findChildren(QWidget)}
        assert "field:device" in fields                                   # the DAQ form is shown ...
        assert "field:ai_intensity" not in fields and "field:channels" not in fields   # ... without them
        saved = machine.load(samba_home / "config" / "machine.toml").data
        assert saved["daq"]["channels"]["intensity"] == "Focus line"
        bridge.close()
    finally:
        svc.stop()
    # Restart with the saved names: Samba_mini shows them and saves under them.
    svc = start_service(samba_home, overrides={k: saved[k] for k in ("daq", "lockin", "hall")})
    try:
        from Samba_mini.app import MainWindow
        bridge = ServiceBridge("Samba_mini", svc.rpc_port, svc.event_port)
        w = MainWindow(bridge, auto_login=False)
        bridge.probe()
        bridge.client.login(*ADMIN)
        w._after_login()
        w.dir_edit.setText(str(tmp_path / "data"))
        w.trajectory.set_workflow("time")
        w.trajectory.set_params("time", {"duration": 0.2, "interval": 0.05, "acq_time": 0.01, "time_const": 0.1,
                                         "volt": 0.0, "position_stage": False})
        names = {row.ch.currentText() for row in w.right.rows}
        assert {"Focus line", "MOKE x1", "BD"} <= names
        for row in w.right.rows:                                          # tick only BD and Focus line
            row.cb.setChecked(row.channel() in ("dc_signal", "intensity"))
            if row.channel() == "intensity":
                row.axis.setCurrentText("Y2")
        assert w._save_config("longitudinal", "two channels", "create")
        stored = bridge.client.call("presets.get", {"workflow": "samba_mini/longitudinal", "name": "two channels"})
        assert {r["id"] for r in stored["value"]["sensors"] if r["on"]} == {"dc_signal", "intensity"}
        w.bottom_tabs.setCurrentWidget(w.trajectory)
        w.start_run()
        assert wait_until(qapp, lambda: w.engine is not None and w.engine.result is not None
                          and w.start_btn.isEnabled(), 60)
        res = w.engine.result
        assert res["status"] == "completed", res
        data = read_file(res["path"])["data"]
        assert {k for k, v in data.items() if v["attrs"].get("role") == "sensor"} == {"BD", "Focus_line"}
        assert data["Focus_line"]["attrs"]["y_axis"] == "Y2"
        # rows are remembered with the configuration: change them, reload it
        for row in w.right.rows:
            row.cb.setChecked(True)
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
        w._load_config("longitudinal", "two channels")
        assert {c["id"] for c in w.right.selected()} == {"dc_signal", "intensity"}
        # the calibration scan always records the BD channel (its fit needs it)
        for row in w.right.rows:
            row.cb.setChecked(row.channel() == "intensity")
        w.live_tabs.setCurrentWidget(w.calibration)
        w.calibration.timescan.load({"n": 4, "interval": 0.05, "acq_time": 0.01, "time_const": 0.1})
        w.start_run()
        assert wait_until(qapp, lambda: not w._calib_run and w.start_btn.isEnabled(), 60)
        cal = read_file(w.engine.result["path"])["data"]
        assert "BD" in cal and "Focus_line" in cal
        w.close()
        bridge.close()
    finally:
        svc.stop()
