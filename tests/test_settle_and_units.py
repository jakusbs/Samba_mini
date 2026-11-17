"""One Settle, the calibration box, the lock-in sensitivity list and the stage units (2026-11-17).

* Spatial scans had two waits ("Settle" after moves, "B settle" after coil changes); there is one
  Settle now, used after either, and once before a time scan's first sample.  Old parameters
  take the longer of their two values.
* The lock-in sensitivity is picked from the 7270's ranges, written in mV.
"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.lockin_ranges import mv_text, sensitivity_ranges  # noqa: E402
from core.scan.engine import Callbacks, RunRequest, ScanEngine  # noqa: E402
from core.scan.workflows import WORKFLOWS, migrate_params  # noqa: E402
from core.storage.hdf5_writer import read_file  # noqa: E402
from tests.test_engine import FAST_LOCKIN  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_old_parameters_get_one_settle_the_longer_of_the_two():
    assert migrate_params({"settle_time": 0.1, "field_settle_s": 0.6}) == {"settle_time": 0.6}
    assert migrate_params({"settle_time": 0.8, "field_settle_s": 0.2}) == {"settle_time": 0.8}
    assert migrate_params({"field_settle_s": 0.4}) == {"settle_time": 0.4}
    p = WORKFLOWS["x_sweep"].coerce({"settle_time": 0.0, "field_settle_s": 1.0})
    assert p["settle_time"] == 1.0 and "field_settle_s" not in p
    assert "settle_time" in WORKFLOWS["time"].coerce({})                 # the time scan settles too
    assert "settle_time" not in WORKFLOWS["b_sweep_dc"].coerce({})      # field sweeps set their own


def test_spatial_scan_settles_after_every_move_and_coil_change(admin_client, tmp_path, monkeypatch, service):
    moke = service.server.host.device("moke")
    real = moke.cmd_measure_point
    settles = []
    monkeypatch.setattr(moke, "cmd_measure_point", lambda ctx, **kw: (settles.append(kw["settle_s"]),
                                                                       real(ctx, **kw))[1])
    params = dict(x_min=0, x_max=20, x_step=10, y=0, b=0.2, repeat_num=1, settle_time=0.05, record_hall=False,
                  **FAST_LOCKIN)
    logs = []
    req = RunRequest("x_sweep", params, setup="longitudinal", sample="S1", save_dir=str(tmp_path / "data"))
    res = ScanEngine(admin_client, req, Callbacks(log=lambda lvl, m: logs.append(m))).run()
    assert res["status"] == "completed", res
    assert len(settles) == res["points"] == 6
    assert settles == [pytest.approx(0.05)] * 6          # every point follows a move or the field reversal
    assert read_file(res["path"])["metadata"]["settle_time"] == pytest.approx(0.05)


def test_time_scan_settles_once_before_the_first_sample(admin_client, tmp_path):
    params = dict(b=0.0, duration=0.03, interval=0.01, x=0, y=0, position_stage=False, record_hall=False,
                  settle_time=0.4, **FAST_LOCKIN)
    logs = []
    req = RunRequest("time", params, setup="longitudinal", sample="S1", save_dir=str(tmp_path / "data"))
    t0 = time.monotonic()
    res = ScanEngine(admin_client, req, Callbacks(log=lambda lvl, m: logs.append(m))).run()
    assert res["status"] == "completed", res
    assert time.monotonic() - t0 >= 0.4
    assert any("Settling 0.4 s before the first sample" in m for m in logs)


def test_timing_box_has_one_settle_and_shows_old_values_migrated(qapp):
    from Samba_mini.panels.trajectory import TrajectoryPanel
    t = TrajectoryPanel()
    assert "field_settle_s" not in t.timing.w and "settle_time" in t.timing.w
    t.set_workflow("x_sweep")
    t.set_params("x_sweep", {"settle_time": 0.1, "field_settle_s": 0.6})      # a configuration saved before
    assert t.timing.w["settle_time"].value() == pytest.approx(0.6)
    assert t.params()["settle_time"] == pytest.approx(0.6)
    n, secs, _msg = t.estimate()
    assert secs == pytest.approx(n * (t.params()["acq_time"] + 0.05 + 0.6))  # the estimate counts the settle


def test_lockin_sensitivity_is_a_list_of_the_7270_ranges_in_mv(qapp):
    from Samba_mini.panels.hardware import HardwareGroup, SensitivityCombo
    assert mv_text(500e-6) == "0.5 mV" and mv_text(1.0) == "1000 mV" and mv_text(2e-9) == "0.000002 mV"
    hw = HardwareGroup(lambda: None)
    sens = hw.li["sensi"]
    assert isinstance(sens, SensitivityCombo)
    assert [sens.itemText(i) for i in range(sens.count())][15:19] == ["0.2 mV", "0.5 mV", "1 mV", "2 mV"]
    assert sens.count() == len(sensitivity_ranges()) == 27
    seen = []
    hw.changed.connect(lambda: seen.append(hw.lockin_values()["sensi"]))
    hw.set_lockin_values({"sensi": 3e-4})                    # between ranges: the 7270 would use 0.5 mV
    assert sens.currentText() == "0.5 mV" and hw.lockin_values()["sensi"] == 5e-4
    assert seen == []                                          # loading values does not count as an edit
    sens.setCurrentIndex(sens.findText("2 mV"))
    assert seen == [2e-3]
    hw._show_lockin_applied({"sensitivity_V": 5e-4, "time_constant_s": 0.2})
    assert "Sens 0.5 mV" in hw.li_applied.text()


def test_calibration_box_keeps_points_int_and_settle(qapp):
    from Samba_mini.panels.calibration import TimeScanBox
    box = TimeScanBox()
    assert box.values() == {"n": 300, "acq_time": 0.1, "settle_time": 1.0}
    # a box saved by an earlier version (interval, coil, lock-in values): only what is still used
    box.load({"n": 50, "interval": 0.3, "acq_time": 0.02, "b": 0.4, "volt": 0.5, "sensi": 1e-3})
    assert box.values() == {"n": 50, "acq_time": 0.02, "settle_time": 1.0}
    assert box.total.text() == "≈ 2.0 s"                      # settle + points × Int


def test_time_scans_take_their_samples_back_to_back(qapp, admin_client, tmp_path):
    """No sampling interval any more (2026-11-17): interval = Int; an older configuration with
    MiniMoke's duration / interval keeps its number of samples."""
    wf = WORKFLOWS["time"]
    p = wf.coerce({"duration": 6.0, "interval": 0.5, "acq_time": 0.1})
    assert p["interval"] == 0.1 and p["duration"] == pytest.approx(1.2)          # still 12 samples
    assert wf.coerce(p) == p                                                       # stable
    from Samba_mini.panels.trajectory import TrajectoryPanel
    t = TrajectoryPanel()
    assert "interval" not in t.timing.w                                           # no Interval field
    t.set_workflow("time")
    t.set_params("time", {"duration": 6.0, "interval": 0.5, "acq_time": 0.1})    # saved before
    assert t.x_grp.samples.value() == 12
    got = wf.coerce(t.params())
    assert got["interval"] == pytest.approx(0.1) and got["duration"] == pytest.approx(1.2)
    t.x_grp.samples.setValue(30)
    t.timing.w["acq_time"].setValue(0.02)
    assert t.params()["duration"] == pytest.approx(0.6) and t.params()["interval"] == pytest.approx(0.02)
    # a run handed an old interval measures back to back all the same
    params = dict(b=0.0, duration=2.0, interval=0.5, x=0, y=0, position_stage=False, record_hall=False,
                  settle_time=0.0, **FAST_LOCKIN)
    req = RunRequest("time", params, setup="longitudinal", sample="S1", save_dir=str(tmp_path / "data"))
    res = ScanEngine(admin_client, req, Callbacks()).run()
    assert res["status"] == "completed" and res["points"] == 4
    import json

    import numpy as np
    f = read_file(res["path"])
    assert json.loads(f["metadata"]["params_json"])["interval"] == pytest.approx(FAST_LOCKIN["acq_time"])
    gaps = np.diff(np.asarray(f["data"]["time"]["values"], dtype=float))
    assert len(gaps) == 3 and (gaps < 0.3).all(), gaps     # one after another, not 0.5 s apart
