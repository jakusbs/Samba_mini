"""Recovery behaviour through the real service boundary (simulation mode)."""

import time

import numpy as np

from core.scan.engine import Callbacks, RunRequest, ScanEngine
from core.storage.hdf5_writer import read_file
from tests.conftest import ADMIN, free_port, start_service

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
    eng.pause()
    assert wait(lambda: eng.state == "PAUSED")
    eng.client.device("lockin").reconnect()                     # controlled recovery while held
    eng.resume()
    t0 = time.time()
    while eng.result is None and time.time() - t0 < 60:
        if eng.state == "PAUSED":
            eng.resume()                                         # operator presses Resume
        time.sleep(0.05)
    assert eng.result["status"] == "completed"
    f = read_file(eng.result["path"])
    assert np.isfinite(f["data"]["lockin_x"]["values"]).all()


def test_resume_reconnects_the_instrument_that_failed(admin_client, service, tmp_path):
    """On the rig a DAQ task stopped finishing (NI -200560) and Resume only retried the point on
    the same stuck task.  Now the error names the DAQ (not the ``moke`` sequencer), and Resume
    reconnects it before the retry; a failed reconnect pauses again and Resume tries again."""
    states, logs = [], []
    eng = ScanEngine(admin_client, RunRequest("x_sweep", XS, save_dir=str(tmp_path)),
                     Callbacks(state=lambda s, m: states.append((s, m)), log=lambda lv, m: logs.append(m)))
    admin_client.acquire_lease()
    admin_client.call("server.sim_fault", {"key": "daq.stuck", "count": 1})
    admin_client.call("server.sim_fault", {"key": "daq.connect", "count": 1})    # first reconnect fails
    eng.start()
    assert wait(lambda: eng.state == "PAUSED", 60)
    msg = eng.message
    assert "daq: simulated: Wait Until Done" in msg and "Resume to reconnect daq" in msg, msg
    time.sleep(0.3)
    assert eng.state == "PAUSED"                                 # held: a retry alone would fail again
    eng.resume()
    assert wait(lambda: eng.state == "PAUSED" and "Reconnecting daq failed" in eng.message, 60)
    eng.resume()
    eng.join(60)
    assert eng.result["status"] == "completed", eng.result
    assert sum(m == "daq reconnected." for m in logs) == 1
    f = read_file(eng.result["path"])
    assert np.isfinite(f["data"]["dc_signal"]["values"]).all()


def test_older_service_errors_reconnect_the_daq(admin_client, tmp_path):
    """A service from before this change names the logical ``moke`` sequencer for a DAQ error;
    the engine then reconnects the DAQ.  A device that is merely busy is not reconnected."""
    from core.protocol import ErrorCode, ServiceError
    eng = ScanEngine(admin_client, RunRequest("x_sweep", XS, save_dir=str(tmp_path)))
    eng._hardware_pause("point 3/7", ServiceError(ErrorCode.DEVICE_FAULT, "moke.measure_point: x",
                                                  {"device": "moke"}), "moke")
    assert eng._reconnect == "daq" and "Resume to reconnect daq" in eng.message
    eng._hardware_pause("point 3/7", ServiceError(ErrorCode.TIMEOUT, "move did not finish"), "stage_longitudinal")
    assert eng._reconnect == "stage_longitudinal"
    eng._hardware_pause("point 3/7", ServiceError(ErrorCode.BUSY, "still finishing"), "moke")
    assert eng._reconnect is None and "Resume to retry this point" in eng.message


def test_field_sweep_continue_reconnects_the_daq(admin_client, service, tmp_path):
    """The same inside a field sweep (a service-side operation): Continue reconnects the DAQ the
    error came from, then the point is retried and the sweep completes."""
    params = dict(b_min=-0.5, b_max=0.5, b_step=0.25, sweep_freq=2.0, num_sweeps=1, x=0.0, y=0.0,
                  position_stage=False)
    states = []
    eng = ScanEngine(admin_client, RunRequest("b_sweep_dc", params, save_dir=str(tmp_path)),
                     Callbacks(state=lambda s, m: states.append((s, m))))
    admin_client.acquire_lease()
    admin_client.call("server.sim_fault", {"key": "daq.stuck", "count": 1})
    eng.start()
    assert wait(lambda: eng.state == "PAUSED", 60)
    assert any("Continue to reconnect daq" in m for s, m in states if s == "PAUSED"), states
    eng.resume()
    eng.join(60)
    assert eng.result["status"] == "completed", eng.result
    f = read_file(eng.result["path"])
    assert np.isfinite(f["data"]["dc_signal"]["values"]).all()


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


def test_a_service_restart_keeps_the_login(samba_home):
    """Stopping the service (e.g. to recover a blocked device) used to log everybody out.  The
    session survives a restart now; only a hash of the token is kept.  Logging out still ends it."""
    from core import paths
    from core.client import ServiceClient
    from core.protocol import ErrorCode, ServiceError
    rpc, ev = free_port(), free_port()
    rs = start_service(samba_home, rpc_port=rpc, event_port=ev)
    c = ServiceClient(rpc, ev, "Samba_mini")
    try:
        c.hello()
        c.bootstrap(*ADMIN)
        c.login(*ADMIN)
        token = c.session
        rs.stop()
        saved = (paths.state_dir() / "sessions.json").read_text(encoding="utf-8")
        assert token not in saved and '"user_id": 1' in saved
        rs = start_service(samba_home, rpc_port=rpc, event_port=ev)
        assert c.call("auth.whoami")["user"]["username"] == "admin"           # no new login
        c.logout()
        rs.stop()
        rs = start_service(samba_home, rpc_port=rpc, event_port=ev)
        c.session = token
        try:
            c.call("auth.whoami")
            raise AssertionError("a logged-out session came back")
        except ServiceError as exc:
            assert exc.code == ErrorCode.AUTH_REQUIRED
    finally:
        c.stop_heartbeat()
        c.close()
        rs.stop()


def test_a_map_scan_waits_for_a_service_restart_and_continues(samba_home, tmp_path):
    """Stopping the service during a map scan pauses it (the service answers the point in flight
    at once and leaves coil and lock-in on); after a restart, Resume takes control again on the
    new service and the scan completes, without a new login and without "lease lost"."""
    from core.client import ServiceClient
    rpc, ev = free_port(), free_port()
    rs = start_service(samba_home, rpc_port=rpc, event_port=ev)
    c = ServiceClient(rpc, ev, "Samba_mini")
    seen, points, logs = [], [], []
    c.on_connection_change = lambda what, info: seen.append(what)
    try:
        c.hello()
        c.bootstrap(*ADMIN)
        c.login(*ADMIN)
        params = dict(XS, x_max=200, b=0.3)
        eng = ScanEngine(c, RunRequest("x_sweep", params, save_dir=str(tmp_path)),
                         Callbacks(point=points.append, log=lambda lv, m: logs.append(m)))
        eng.start()
        assert wait(lambda: len(points) >= 3)
        daq = rs.server.host.device("daq")
        t0 = time.time()
        rs.stop()                                                    # mid-scan
        assert wait(lambda: eng.state == "PAUSED" and "Lost the device service" in eng.message, 30)
        assert time.time() - t0 < 15                                  # not after a 2 min timeout
        assert daq.coil_V == 0.3                                      # left on for the scan
        rs = start_service(samba_home, rpc_port=rpc, event_port=ev)   # same ports, new instance
        eng.resume()
        eng.join(60)
        assert eng.result["status"] == "completed", eng.result
        assert eng.result["points"] == eng.result["points_planned"] == 42      # 21 positions, +b and -b
        f = read_file(eng.result["path"])
        assert np.isfinite(f["data"]["dc_signal"]["values"]).all()
        assert f["metadata"]["service_restarts"] == 1
        assert any("Back on the restarted device service" in m for m in logs)
        assert "lease_lost" not in seen and "session_lost" not in seen, seen
    finally:
        c.stop_heartbeat()
        c.close()
        rs.stop()


def test_control_taken_on_the_same_service_still_ends_a_map_scan(admin_client, service, tmp_path):
    """Only a stopped or restarted service is waited for: when control is taken away on the same
    service (forced release), its cleanup switched the outputs off, so the scan ends."""
    points = []
    eng = ScanEngine(admin_client, RunRequest("x_sweep", dict(XS, x_max=600), save_dir=str(tmp_path)),
                     Callbacks(point=points.append))
    eng.start()
    assert wait(lambda: len(points) >= 2)
    other = service.client("admin2")
    other.login(*ADMIN)
    other.call("lease.force_release", {}, 60.0)
    eng.join(60)
    assert eng.result["status"] == "interrupted", eng.result


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
