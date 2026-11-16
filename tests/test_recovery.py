"""Recovery behaviour through the real service boundary (simulation mode)."""

import time

import numpy as np

from core.scan.engine import Callbacks, RunRequest, ScanEngine
from core.storage.hdf5_writer import read_file
from tests.conftest import ADMIN, start_service

LOCKIN = dict(volt=1.0, sensi=1e-3, lockin_freq=1777.0, time_const=0.1, phase=0.0, acq_time=0.005)
XS = dict(x_min=0, x_max=60, x_step=10, y=0, b=0.0, repeat_num=1, settle_time=0.0, field_settle_s=0.0,
          record_hall=False, **LOCKIN)


def wait(cond, timeout=30.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        time.sleep(0.02)
    return cond()


def test_driver_timeout_auto_pauses_then_retries(admin_client, service, tmp_path):
    # A DAQ acquisition that never completes -> bounded wait -> TIMEOUT -> auto-pause.
    msgs = []
    eng = ScanEngine(admin_client, RunRequest("x_sweep", XS, save_dir=str(tmp_path)),
                     Callbacks(state=lambda s, m: msgs.append((s, m))))
    admin_client.acquire_lease()
    admin_client.call("server.sim_fault", {"key": "daq.hang", "count": 1})
    eng.start()
    assert wait(lambda: eng.state == "PAUSED", 60)
    assert any("Hardware error" in m and "TIMEOUT" not in m or "time" in m.lower() for s, m in msgs if s == "PAUSED")
    eng.resume()
    eng.join(60)
    assert eng.result["status"] == "completed"
    f = read_file(eng.result["path"])
    assert np.isfinite(f["data"]["dc_signal"]["values"]).all()
    ids = f["data"]["acq_id"]["values"].ravel()
    assert len(set(ids.tolist())) == len(ids)                    # every point from a fresh acquisition


def test_reconnect_device_mid_run(admin_client, service, tmp_path):
    params = dict(XS, x_max=150)
    points = []
    eng = ScanEngine(admin_client, RunRequest("x_sweep", params, save_dir=str(tmp_path)),
                     Callbacks(point=points.append))
    eng.start()
    assert wait(lambda: len(points) >= 3)
    admin_client.device("lockin").reconnect()                    # e.g. after a cable glitch
    t0 = time.time()
    while eng.result is None and time.time() - t0 < 60:
        if eng.state == "PAUSED":
            eng.resume()                                         # operator presses Resume
        time.sleep(0.05)
    assert eng.result["status"] == "completed"
    f = read_file(eng.result["path"])
    assert np.isfinite(f["data"]["lockin_x"]["values"]).all()


def test_service_lost_marks_run_interrupted(samba_home, tmp_path):
    rs = start_service(samba_home)
    try:
        c = rs.client()
        c.bootstrap(*ADMIN)
        c.login(*ADMIN)
        params = dict(b=0.0, duration=60, interval=0.05, x=0, y=0, position_stage=False, record_hall=False,
                      **LOCKIN)
        points = []
        eng = ScanEngine(c, RunRequest("time", params, save_dir=str(tmp_path)), Callbacks(point=points.append))
        eng.start()
        assert wait(lambda: len(points) >= 3)
        rs.server.stop("simulated crash of the service")        # service goes away mid-run
        eng.join(90)
        assert eng.result["status"] == "interrupted", eng.result
        assert "not verified" in eng.result["message"]
        f = read_file(eng.result["path"])
        assert f["attrs"]["scan_status"] == "interrupted"
        n = f["metadata"]["points_acquired"]
        assert n >= 3 and np.isfinite(f["data"]["dc_signal"]["values"][:n]).all()
    finally:
        rs.stop()


def test_profile_persists_across_service_restart(samba_home):
    rs = start_service(samba_home)
    c = rs.client()
    c.bootstrap(*ADMIN)
    c.login(*ADMIN)
    c.call("prefs.set", {"values": {"data_dir": "E:/MOKE", "default_sample": "Pt/Co"}})
    c.call("presets.save", {"workflow": "samba_mini/longitudinal", "name": "fast",
                            "value": {"workflow": "time", "params": {"duration": 5}}})
    rs.stop()
    rs = start_service(samba_home)
    try:
        c = rs.client()
        res = c.login(*ADMIN)
        assert res["prefs"]["data_dir"] == "E:/MOKE"
        assert c.call("presets.list", {"workflow": "samba_mini/longitudinal"})[0]["name"] == "fast"
    finally:
        rs.stop()
