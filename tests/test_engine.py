"""End-to-end simulated measurements through the real service/client boundary."""

import json
import math
import threading
import time

import h5py
import numpy as np
import pytest

from core.scan import plans
from core.scan.engine import Callbacks, RunRequest, ScanEngine
from core.storage.hdf5_writer import read_file
from tests.conftest import ADMIN


def run_engine(client, workflow, params, tmp_path, setup="longitudinal", on_state=None, export_csv=False):
    states, points = [], []
    cb = Callbacks(state=lambda s, m: (states.append((s, m)), on_state and on_state(s, m)),
                   point=points.append)
    req = RunRequest(workflow, params, setup=setup, sample="S1", operator="op", save_dir=str(tmp_path / "data"),
                     config_name="cfg", export_csv=export_csv, lab_notebook=str(tmp_path / "nb.csv"))
    eng = ScanEngine(client, req, cb)
    return eng, states, points


FAST_LOCKIN = dict(volt=1.0, sensi=1e-3, lockin_freq=1777.0, time_const=0.1, phase=0.0, acq_time=0.01)


def test_b_sweep_dc_end_to_end(admin_client, tmp_path):
    params = dict(b_min=-0.5, b_max=0.5, b_step=0.25, sweep_freq=2.0, num_sweeps=2, x=0.0, y=0.0,
                  position_stage=True)
    eng, states, points = run_engine(admin_client, "b_sweep_dc", params, tmp_path, export_csv=True)
    res = eng.run()
    assert res["status"] == "completed", res
    assert res["points"] == 2 * 9
    f = read_file(res["path"])
    assert f["attrs"]["scan_status"] == "completed"
    assert f["attrs"]["mode"] == "simulation"
    assert res["path"].endswith("_SIM.h5")
    sp = f["data"]["actuator_coil_setpoint"]["values"]
    assert sp.shape == (2, 9)
    assert np.allclose(sp[0], [-0.5, -0.25, 0, 0.25, 0.5, 0.25, 0, -0.25, -0.5])
    assert f["data"]["actuator_coil_setpoint"]["attrs"]["unit"] == "V"
    hall = f["data"]["hall_field"]["values"]
    assert np.all(np.isfinite(hall))
    # Sim: 100 mT/V with a 5 ms coil lag.  The Hall reading at the +0.5 V point
    # must belong to this point (~40-50 mT), not to the previous one (25 mT).
    assert 38.0 < hall[0, 4] < 52.0
    avg = f["data"]["averaged/dc_signal"]["values"]
    assert avg.shape == (9,) and np.all(np.isfinite(avg))
    assert np.all(f["data"]["averaged/n_sweeps"]["values"] == 2)
    meta = f["metadata"]
    assert meta["user"] == ADMIN[0] and meta["sample_id"] == "S1"
    assert json.loads(meta["params_json"])["num_sweeps"] == 2
    assert "daq" in json.loads(meta["devices_json"])
    assert meta["points_acquired"] == 18
    assert res["csv_path"] and open(res["csv_path"], encoding="utf-8").read().count("SIMULATED") == 1
    assert [s for s, _ in states][-1] == "FINISHED"


def test_b_sweep_lockin_reads_current_point(admin_client, tmp_path):
    # Lock-in X must follow the field at the *same* point (finding F1).
    params = dict(b_min=-0.6, b_max=0.6, b_step=0.3, sweep_freq=10.0, num_sweeps=1, x=0.0, y=0.0,
                  position_stage=False, **dict(FAST_LOCKIN, time_const=0.001))
    # Measurements never configure the lock-in: set it by hand first (Hardware box "Apply now").
    admin_client.acquire_lease()
    admin_client.device("lockin").command("configure", volt=1.0, sensi=1e-3, freq=1777.0, time_const=0.001,
                                          phase=0.0)
    eng, states, points = run_engine(admin_client, "b_sweep_lockin", params, tmp_path)
    res = eng.run()
    assert res["status"] == "completed", res
    f = read_file(res["path"])
    x = f["data"]["lockin_x"]["values"][0]
    coil = f["data"]["actuator_coil_setpoint"]["values"][0]
    # setpoints: -0.6 -0.3 0 0.3 0.6 0.3 0 -0.3 -0.6; saturated ends must have the field's sign
    assert np.sign(x[0]) != np.sign(x[4])
    assert coil[4] == 0.6 and x[4] > 0
    assert np.all(np.isfinite(f["data"]["lockin_theta"]["values"]))
    assert f["data"]["lockin_theta"]["attrs"]["unit"] == "deg"


def test_too_fast_dc_sweep_rejected_like_minimoke(admin_client, tmp_path):
    # MiniMoke's queue validation: every point needs >= 2 ms for the Hall probe.
    # Its synthetic-field "fast mode" (F2/F3) is therefore unreachable; same here.
    assert plans.b_sweep_dc_validation(-0.2, 0.2, 0.1, 60.0) is not None
    assert plans.b_sweep_dc_validation(-0.2, 0.2, 0.1, 50.0) is None
    params = dict(b_min=-0.2, b_max=0.2, b_step=0.1, sweep_freq=60.0, num_sweeps=1, x=0.0, y=0.0,
                  position_stage=False)
    eng, *_ = run_engine(admin_client, "b_sweep_dc", params, tmp_path)
    res = eng.run()
    assert res["status"] == "failed" and "too high for the Hall probe" in res["message"]
    assert res["path"] is None


@pytest.mark.parametrize("workflow,params,shape", [
    ("x_sweep", dict(x_min=0, x_max=20, x_step=10, y=0, b=0.3, repeat_num=1, settle_time=0.0,
                     field_settle_s=0.01, record_hall=True), (2, 3)),
    ("y_sweep", dict(y_min=0, y_max=10, y_step=10, x=5, b=0.0, repeat_num=2, settle_time=0.0,
                     field_settle_s=0.0, record_hall=False), (4, 2)),
    ("xy_sweep", dict(x_min=0, x_max=20, x_step=10, y_min=0, y_max=10, y_step=10, b=0.0, repeat_num=1,
                      settle_time=0.0, field_settle_s=0.0, record_hall=True), (2, 3)),
])
def test_position_sweeps(admin_client, tmp_path, workflow, params, shape):
    params = dict(params, **FAST_LOCKIN)
    eng, states, points = run_engine(admin_client, workflow, params, tmp_path)
    res = eng.run()
    assert res["status"] == "completed", res
    f = read_file(res["path"])
    dc = f["data"]["dc_signal"]["values"]
    assert dc.shape == shape and np.all(np.isfinite(dc))
    if workflow == "xy_sweep":
        assert f["attrs"]["_is_2d"]
        assert np.allclose(f["data"]["actuator_x_setpoint"]["values"][0], [0, 10, 20])
        assert np.allclose(f["data"]["actuator_y"]["values"][:, 0], [0, 10], atol=1e-6)
    if workflow == "y_sweep":
        assert np.all(np.isnan(f["data"]["hall_field"]["values"]))      # not recorded -> NaN
        assert np.allclose(f["data"]["position_x"]["values"], 5.0, atol=1e-6)
        assert list(f["data"]["coil_setpoint"]["values"][:, 0]) == [0.0, -0.0, 0.0, -0.0]
    if workflow == "x_sweep":
        assert list(f["data"]["coil_setpoint"]["values"][:, 0]) == [0.3, -0.3]


def test_time_trace(admin_client, tmp_path):
    params = dict(b=0.1, duration=0.3, interval=0.05, x=0, y=0, position_stage=False, record_hall=True,
                  **FAST_LOCKIN)
    eng, *_ = run_engine(admin_client, "time", params, tmp_path)
    res = eng.run()
    assert res["status"] == "completed", res
    f = read_file(res["path"])
    t = f["data"]["time"]["values"]
    assert len(t) == 6 and np.all(np.diff(t) > 0.03)
    assert f["data"]["time"]["attrs"]["role"] == "x"


def test_pause_resume_and_abort_engine(admin_client, tmp_path):
    params = dict(x_min=0, x_max=200, x_step=10, y=0, b=0.0, repeat_num=1, settle_time=0.0,
                  field_settle_s=0.0, record_hall=False, **FAST_LOCKIN)
    eng, states, points = run_engine(admin_client, "x_sweep", params, tmp_path)
    eng.start()
    t0 = time.time()
    while len(points) < 3 and time.time() - t0 < 20:
        time.sleep(0.02)
    eng.pause()
    t0 = time.time()
    while eng.state != "PAUSED" and time.time() - t0 < 10:
        time.sleep(0.02)
    assert eng.state == "PAUSED"
    n = len(points)
    time.sleep(0.5)
    assert len(points) == n                     # held while paused
    eng.resume()
    t0 = time.time()
    while len(points) == n and time.time() - t0 < 10:
        time.sleep(0.02)
    assert len(points) > n
    eng.abort()
    eng.join(30)
    assert eng.result["status"] == "aborted"
    f = read_file(eng.result["path"])
    assert f["attrs"]["scan_status"] == "aborted"
    assert f["metadata"]["points_acquired"] == eng.result["points"] < 42
    dc = f["data"]["dc_signal"]["values"].ravel()
    assert np.isfinite(dc[: eng.result["points"]]).all() and np.isnan(dc[eng.result["points"]:]).all()


def test_auto_pause_on_hardware_error_then_retry(admin_client, service, tmp_path):
    params = dict(x_min=0, x_max=30, x_step=10, y=0, b=0.0, repeat_num=1, settle_time=0.0,
                  field_settle_s=0.0, record_hall=False, **FAST_LOCKIN)
    msgs = []
    eng, states, points = run_engine(admin_client, "x_sweep", params, tmp_path,
                                     on_state=lambda s, m: msgs.append((s, m)))
    admin_client.acquire_lease()
    admin_client.call("server.sim_fault", {"key": "lockin.read", "count": 1})
    eng.start()
    t0 = time.time()
    while eng.state != "PAUSED" and time.time() - t0 < 20:
        time.sleep(0.02)
    assert eng.state == "PAUSED"
    assert any("Hardware error" in m for s, m in msgs if s == "PAUSED")
    eng.resume()
    eng.join(30)
    assert eng.result["status"] == "completed"
    f = read_file(eng.result["path"])
    assert np.isfinite(f["data"]["lockin_x"]["values"]).all()


def test_unavailable_device_refuses_to_start(admin_client, tmp_path):
    admin_client.acquire_lease()
    admin_client.device("lockin").disconnect()
    params = dict(b=0.0, duration=0.1, interval=0.05, x=0, y=0, position_stage=False, record_hall=False,
                  **FAST_LOCKIN)
    eng, *_ = run_engine(admin_client, "time", params, tmp_path)
    res = eng.run()
    assert res["status"] == "failed"
    assert "lockin" in res["message"]
    assert res["path"] is None                  # no file with fake data


def test_lease_loss_interrupts_run(admin_client, service, tmp_path):
    params = dict(b=0.0, duration=30, interval=0.05, x=0, y=0, position_stage=False, record_hall=False,
                  **FAST_LOCKIN)
    eng, states, points = run_engine(admin_client, "time", params, tmp_path)
    eng.start()
    t0 = time.time()
    while len(points) < 2 and time.time() - t0 < 20:
        time.sleep(0.02)
    # An administrator forces the lease free (e.g. the operator's PC session hung).
    other = service.client("admin2")
    other.login(*ADMIN)
    other.call("lease.force_release", {}, 60.0)
    eng.join(60)
    assert eng.result["status"] in ("interrupted", "aborted")
    f = read_file(eng.result["path"])
    assert f["attrs"]["scan_status"] == eng.result["status"]
    assert service.server.lease.status()["state"] == "FREE"


def test_samba_metadata_fields_reach_the_file(admin_client, tmp_path):
    """The Samba metadata box (incidence, polarization, λ/2 …) is stored under Samba's keys."""
    params = dict(b=0.0, duration=0.06, interval=0.02, x=0.0, y=0.0, position_stage=False, record_hall=True,
                  **dict(FAST_LOCKIN, volt=0.0))
    extra = {"device_id": "D7", "incidence": "LMOKE+", "mirror_shift": 3.5, "polarization": "p", "lam2": True,
             "lam4": False, "noDC": True, "r_4wire_ohm": 120.5, "fm_thickness_nm": 1.2, "t_stack_nm": 9.0,
             "sample_id": "must-not-override"}
    req = RunRequest("time", params, setup="longitudinal", geometry="LMOKE+", sample="S1", operator="op",
                     save_dir=str(tmp_path / "data"), config_name="cfg", extra_metadata=extra)
    res = ScanEngine(admin_client, req, Callbacks()).run()
    assert res["status"] == "completed", res
    meta = read_file(res["path"])["metadata"]
    assert meta["device_id"] == "D7" and meta["incidence"] == "LMOKE+" and meta["polarization"] == "p"
    assert bool(meta["lam2"]) and not bool(meta["lam4"]) and bool(meta["noDC"])
    assert meta["mirror_shift"] == pytest.approx(3.5) and meta["r_4wire_ohm"] == pytest.approx(120.5)
    assert meta["sample_id"] == "S1" and meta["geometry"] == "LMOKE+"     # engine keys win


def test_bd_calibration_written_like_samba(admin_client, tmp_path):
    """Six mV values -> /data/calibration (role calibration, mV); all zeros = not written."""
    params = dict(b=0.0, duration=0.04, interval=0.02, x=0.0, y=0.0, position_stage=False, record_hall=False,
                  **dict(FAST_LOCKIN, volt=0.0))
    cal = [-5.1, -3.0, -1.0, 1.0, 3.0, 5.2]
    for values, expect in ((cal, True), ([0.0] * 6, False), (None, False)):
        req = RunRequest("time", params, sample="S1", save_dir=str(tmp_path / "data"), bd_calibration=values)
        res = ScanEngine(admin_client, req, Callbacks()).run()
        assert res["status"] == "completed", res
        with h5py.File(res["path"], "r") as f:
            assert ("calibration" in f["data"]) == expect
            if expect:
                assert np.allclose(f["data"]["calibration"][:], cal)
                assert f["data"]["calibration"].attrs["unit"] == "mV"
                assert f["data"]["calibration"].attrs["role"] == "calibration"
