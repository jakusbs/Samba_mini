"""Backend service through the real ZeroMQ boundary (simulation mode)."""

import math
import time

import pytest

from core.protocol import ErrorCode, ServiceError
from tests.conftest import ADMIN, OPERATOR


def test_hello_and_bootstrap(service):
    c = service.client()
    info = c.hello()
    assert info["mode"] == "simulation"
    assert info["needs_bootstrap"] is True
    c.bootstrap(*ADMIN)
    assert c.hello()["needs_bootstrap"] is False
    with pytest.raises(ServiceError) as e:
        c.bootstrap("mallory", "mallory-password")
    assert e.value.code == ErrorCode.FORBIDDEN


def test_unauthenticated_requests_rejected(service):
    c = service.client()
    for method, params in [("device.list", {}), ("lease.acquire", {}),
                           ("device.command", {"device": "daq", "command": "zero_outputs"})]:
        with pytest.raises(ServiceError) as e:
            c.call(method, params)
        assert e.value.code == ErrorCode.AUTH_REQUIRED


def test_wrong_password(admin_client, service):
    c = service.client()
    with pytest.raises(ServiceError) as e:
        c.login("admin", "nope-nope-nope")
    assert e.value.code == ErrorCode.AUTH_FAILED


def test_device_list_and_read(admin_client):
    devs = {d["name"]: d for d in admin_client.devices()}
    assert {"daq", "lockin", "hall", "stage_longitudinal", "stage_polar", "magnet", "moke"} <= set(devs)
    assert devs["daq"]["state"] == "ON" and devs["daq"]["simulated"] is True
    hall = admin_client.device("hall")
    r = hall.read("field_mT")["field_mT"]
    assert r.quality.value == "SIMULATED" and math.isfinite(r.value)


def test_mutation_needs_lease(admin_client):
    daq = admin_client.device("daq")
    with pytest.raises(ServiceError) as e:
        daq.command("set_outputs", coil_V=0.5)
    assert e.value.code == ErrorCode.LEASE_REQUIRED
    admin_client.acquire_lease()
    assert daq.command("set_outputs", coil_V=0.5)["coil_V"] == 0.5
    assert daq.read_value("coil_command_V") == 0.5


def test_limits_enforced_in_backend(admin_client):
    admin_client.acquire_lease()
    daq = admin_client.device("daq")
    with pytest.raises(ServiceError) as e:
        daq.command("set_outputs", coil_V=7.5)
    assert e.value.code == ErrorCode.OUT_OF_RANGE
    with pytest.raises(ServiceError) as e:
        daq.write("coil_command_V", -9.0)
    assert e.value.code == ErrorCode.OUT_OF_RANGE


def test_competing_lease_rejected(admin_client, service):
    admin_client.call("auth.create_user", {"username": OPERATOR[0], "password": OPERATOR[1]})
    other = service.client("other")
    other.login(*OPERATOR)
    admin_client.acquire_lease()
    with pytest.raises(ServiceError) as e:
        other.acquire_lease()
    assert e.value.code == ErrorCode.LEASE_HELD
    with pytest.raises(ServiceError) as e:
        other.device("daq").command("zero_outputs")
    assert e.value.code == ErrorCode.LEASE_HELD
    admin_client.release_lease()
    assert other.acquire_lease()["state"] == "HELD"


def test_measure_point(admin_client):
    admin_client.acquire_lease()
    moke = admin_client.device("moke")
    p1 = moke.command("measure_point", coil_V=0.3, acq_time_s=0.02, settle_s=0.01, hall=True,
                      lockin=True, token="a")
    p2 = moke.command("measure_point", coil_V=0.3, acq_time_s=0.02, hall=False, lockin=False, token="b")
    assert p2["acq_id"] > p1["acq_id"]
    assert p1["token"] == "a" and p2["token"] == "b"
    assert math.isfinite(p1["balanced_V"]) and math.isfinite(p1["hall_field_mT"])
    assert p1["n_samples"] == round(0.02 * 50000)
    assert math.isnan(p2["hall_field_mT"]) and math.isnan(p2["lockin_x_V"])
    # Hall integrates inside the window: field ~ 0.3 V * 100 mT/V (sim)
    assert abs(p1["hall_field_mT"] - 30.0) < 3.0


def test_field_sweep_operation(admin_client):
    admin_client.acquire_lease()
    moke = admin_client.device("moke")
    sp = [-0.5, 0.0, 0.5, 0.0, -0.5]
    op = moke.start_operation("field_sweep", setpoints_V=sp, branch=[0, 0, 0, 1, 1], t_point_s=0.03,
                              settle_s=0.005, acq_time_s=0.01, hall=True, lockin=False)
    records = []
    deadline = time.time() + 20
    while not op.done and time.time() < deadline:
        res = op.poll(1.0)
        records += res["records"]
    assert op.state == "COMPLETED"
    pts = [r for r in records if r["kind"] == "point"]
    assert [p["setpoint_V"] for p in pts] == sp
    assert [p["seq"] for p in records] == list(range(1, len(records) + 1))


def test_pause_resume_abort_operation(admin_client):
    admin_client.acquire_lease()
    moke = admin_client.device("moke")
    sp = [0.0] * 60
    op = moke.start_operation("field_sweep", setpoints_V=sp, branch=[0] * 60, t_point_s=0.02,
                              settle_s=0.0, acq_time_s=0.005, hall=False, lockin=False)
    op.poll(0.3)
    op.pause()
    t0 = time.time()
    while op.state != "PAUSED" and time.time() - t0 < 5:
        op.poll(0.5)
    assert op.state == "PAUSED"
    n_paused = op.last_seq
    time.sleep(0.3)
    op.poll(0.2)
    assert op.last_seq == n_paused          # nothing acquired while paused
    op.resume()
    t0 = time.time()
    while op.last_seq <= n_paused and time.time() - t0 < 5:
        op.poll(0.5)
    assert op.last_seq > n_paused
    op.abort()
    t0 = time.time()
    while not op.done and time.time() - t0 < 5:
        op.poll(0.5)
    assert op.state == "ABORTED"
    assert op.last_seq < 60


def test_unavailable_device_blocks_measurement(admin_client, service):
    admin_client.acquire_lease()
    hall = admin_client.device("hall")
    hall.disconnect()
    with pytest.raises(ServiceError) as e:
        admin_client.device("moke").command("measure_point", coil_V=0.0, acq_time_s=0.01, hall=True,
                                            lockin=False)
    assert e.value.code == ErrorCode.DEVICE_UNAVAILABLE
    with pytest.raises(ServiceError) as e:
        admin_client.call("run.begin", {"required_devices": ["daq", "hall"]})
    assert e.value.code == ErrorCode.DEVICE_UNAVAILABLE
    r = hall.read("field_mT")["field_mT"]
    assert r.quality.value == "INVALID" and r.value is None   # never a fake 0


def test_lease_expiry_reconciles(samba_home):
    from tests.conftest import start_service
    rs = start_service(samba_home, lease_ttl_s=1.0)
    try:
        c = rs.client()
        c.bootstrap(*ADMIN)
        c.login(*ADMIN)
        c.acquire_lease()
        c.device("daq").command("set_outputs", coil_V=1.0)
        moke = c.device("moke")
        op = moke.start_operation("field_sweep", setpoints_V=[0.2] * 400, branch=[0] * 400,
                                  t_point_s=0.02, settle_s=0.0, acq_time_s=0.005, hall=False, lockin=False)
        c.stop_heartbeat()          # simulate a client that disappeared
        t0 = time.time()
        st = None
        while time.time() - t0 < 15:
            st = rs.server.lease.status()
            if st["state"] == "FREE":
                break
            time.sleep(0.1)
        assert st["state"] == "FREE"
        assert st["last_reconcile"]["reason"] == "heartbeat lost"
        assert st["last_reconcile"]["stopped"] is True
        assert rs.server.ops.get(op.op_id).state == "ABORTED"
        assert rs.server.host.device("daq").coil_V == 0.0       # cleanup zeroed the coil
    finally:
        rs.stop()


def test_stage_move_and_stop(admin_client):
    admin_client.acquire_lease()
    st = admin_client.device("stage_longitudinal")
    res = st.command("move_to", axis="x", position_mm=0.05)
    assert abs(res["position_mm"] - 0.05) < 1e-6
    res = st.command("jog", axis="x", delta_mm=0.01)
    assert abs(res["position_mm"] - 0.06) < 1e-6


def test_read_only_mode_refuses_control_and_writes(samba_home):
    from tests.conftest import start_service
    rs = start_service(samba_home, {"backend": {"read_only": True}})
    calls = []
    rs.server.host.cleanup_all = lambda *a, **k: calls.append(a) or []
    try:
        c = rs.client()
        assert c.hello()["read_only"] is True
        c.bootstrap(*ADMIN)
        c.login(*ADMIN)
        for fn in (lambda: c.acquire_lease(),
                   lambda: c.device("daq").command("set_outputs", coil_V=0.0),
                   lambda: c.device("lockin").command("output_off"),
                   lambda: c.device("stage_longitudinal").command("stop"),
                   lambda: c.device("daq").write("coil_command_V", 0.0),
                   lambda: c.call("run.begin", {})):
            with pytest.raises(ServiceError) as e:
                fn()
            assert e.value.code == ErrorCode.FORBIDDEN
        assert math.isfinite(c.device("hall").read_value("field_mT"))
        assert "x_V" in c.device("lockin").command("read_xyrt")      # non-mutating query allowed
    finally:
        rs.stop()
    assert calls == []                                               # no cleanup writes at shutdown


def test_manual_connect_results_are_logged(admin_client, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="Samba_controls.service.server")
    admin_client.acquire_lease()
    stage = admin_client.device("stage_polar")
    assert stage.disconnect()["state"] == "OFF"
    assert stage.connect()["state"] == "ON"
    stage.reconnect()
    with pytest.raises(ServiceError):
        admin_client.device("no_such_device").connect()
    lines = [r.getMessage() for r in caplog.records if r.name == "Samba_controls.service.server"]
    who = "admin (pytest-admin)"
    assert any(l.startswith(f"disconnect stage_polar by {who}: OFF") for l in lines)
    assert any(l.startswith(f"connect stage_polar by {who}: ON") for l in lines)
    assert any(l.startswith(f"reconnect stage_polar by {who}: ON") for l in lines)
    assert any(l.startswith(f"connect no_such_device by {who} failed:") for l in lines)


def test_devices_a_run_does_not_use_stay_free(admin_client, caplog):
    """During a run only the devices it declared are locked: the stage can be moved to focus
    during a time scan, but not during a run that uses it; the run's own devices stay locked."""
    c = admin_client
    c.acquire_lease()
    stage = c.device("stage_longitudinal")

    def run(required, use_stage_expected_free):
        c.call("run.begin", {"required_devices": required, "workflow": "time"})
        op = c.device("moke").start_operation("time_trace", coil_V=0.0, n_points=200, interval_s=0.02,
                                              acq_time_s=0.005, hall=False, lockin=True)
        op.poll(0.2)
        try:
            if use_stage_expected_free:
                before = stage.read_value("x_mm")
                stage.command("jog", axis="x", delta_mm=0.01)
                r = stage.read("x_mm")["x_mm"]
                assert r.source == "device" and abs(r.value - (before + 0.01)) < 1e-6
            else:
                with pytest.raises(ServiceError) as e:
                    stage.command("jog", axis="x", delta_mm=0.01)
                assert e.value.code == ErrorCode.BUSY
            with pytest.raises(ServiceError) as e:                     # the run's own device
                c.device("daq").command("set_outputs", coil_V=0.1)
            assert e.value.code == ErrorCode.BUSY
            assert c.device("daq").read("balanced_V")["balanced_V"].source != "device"   # coordinated
        finally:
            op.abort()
            t0 = time.time()
            while not op.done and time.time() - t0 < 10:
                op.poll(0.5)
            c.call("run.end", {})

    import logging
    caplog.set_level(logging.INFO, logger="Samba_controls.service.server")
    run(["daq", "moke", "lockin"], use_stage_expected_free=True)                       # calibration scan
    run(["daq", "moke", "lockin", "stage_longitudinal"], use_stage_expected_free=False)  # e.g. X-sweep
    lines = [r.getMessage() for r in caplog.records if r.name == "Samba_controls.service.server"]
    assert any(m.startswith("manual stage_longitudinal.jog(axis=x, delta_mm=0.01) by admin")
               and m.endswith("during a measurement") for m in lines)        # the focusing move is logged
    assert any(m.startswith("refused stage_longitudinal.jog by admin") for m in lines)
    assert any(m.startswith("refused daq.set_outputs by admin") for m in lines)
