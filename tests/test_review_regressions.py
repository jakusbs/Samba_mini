"""Regression coverage for lease, run ownership, deadlines, paging and timing."""

import json
import threading
import time

import pytest

from core import protocol
from core.client import OperationHandle
from core.protocol import ErrorCode, ServiceError
from core.scan.engine import Interrupted, RunRequest, ScanEngine
from core.storage.hdf5_writer import read_file
from Samba_controls.service.lease import LeaseManager
from Samba_controls.service.operations import Operation
from tests.test_engine import FAST_LOCKIN, run_engine


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.mark.parametrize("report", [
    {"stopped": True, "ok": False, "problems": ["coil cleanup failed"]},
    {"stopped": False, "ok": True},
    {"stopped": True},
])
def test_lease_requires_verified_cleanup(report):
    reports = [report, report, {"stopped": True, "ok": True}]
    lease = LeaseManager(10, lambda reason: reports.pop(0))
    lease.acquire("owner", {"user": "operator", "client": "test"})
    old_token = lease.request_token("owner")
    assert lease.release("owner")["state"] == "RECONCILING"
    assert old_token.cancelled
    with pytest.raises(ServiceError) as exc:
        lease.acquire("next", {"user": "next", "client": "test"})
    assert exc.value.code == ErrorCode.LEASE_RECONCILING
    assert lease.force("retry still fails")["state"] == "RECONCILING"
    assert lease.force("verified retry")["state"] == "FREE"


def test_force_cannot_bypass_in_progress_reconciliation():
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def reconcile(reason):
        calls.append(reason)
        entered.set()
        assert finish.wait(5)
        return {"stopped": True, "ok": True}

    lease = LeaseManager(10, reconcile)
    lease.acquire("owner", {"user": "operator", "client": "test"})
    lease.release("owner", wait=False)
    try:
        assert entered.wait(2)
        assert lease.force("concurrent retry")["state"] == "RECONCILING"
        assert len(calls) == 1
    finally:
        finish.set()
        lease._reconcile_thread.join(5)
    assert lease.status()["state"] == "FREE"


def begin(client, required):
    result = client.call("run.begin", {"required_devices": required, "workflow": "test"})
    return client.for_run(result["run_id"])


def test_run_locks_logical_aliases_even_between_operations(admin_client):
    c = admin_client
    c.acquire_lease()
    scan = begin(c, ["daq", "hall", "lockin", "stage_longitudinal"])
    mutations = [
        lambda: c.device("daq").write("coil_command_V", 0.3),
        lambda: c.device("magnet").write("coil_command_V", 0.3),
        lambda: c.device("magnet").command("set_coil", coil_V=0.3),
        lambda: c.device("moke").command("measure_point", coil_V=0.3, acq_time_s=0.01),
        lambda: c.device("moke").command("prepare", hall_integration_s=0.002),
        lambda: c.device("stage_longitudinal").command("jog", axis="x", delta_mm=0.01),
        lambda: c.device("lockin").reconnect(),
        lambda: c.device("moke").start_operation(
            "time_trace", coil_V=0.0, n_points=1, interval_s=0.02,
            acq_time_s=0.005, hall=False, lockin=False),
    ]
    try:
        for mutate in mutations:
            with pytest.raises(ServiceError) as exc:
                mutate()
            assert exc.value.code == ErrorCode.BUSY
        # Same login, but only the engine's private client can mutate reserved devices.
        scan.device("magnet").write("coil_command_V", 0.2)
        rec = scan.device("moke").command(
            "measure_point", coil_V=0.0, acq_time_s=0.005, hall=False, lockin=False)
        assert rec["coil_V"] == 0.0
        assert "run_id" not in c.status()["run_info"]
    finally:
        scan.call("run.end", {})
    with pytest.raises(ServiceError) as exc:
        scan.device("daq").write("coil_command_V", 0.9)
    assert exc.value.code == ErrorCode.INVALID_STATE


def test_calibration_run_keeps_unused_stage_available_between_points(admin_client):
    c = admin_client
    c.acquire_lease()
    scan = begin(c, ["daq", "lockin"])
    try:
        c.device("stage_longitudinal").command("jog", axis="x", delta_mm=0.01)
        assert c.device("stage_longitudinal").read_value("x_mm") == pytest.approx(0.01)
        with pytest.raises(ServiceError) as exc:
            c.device("magnet").command("zero")
        assert exc.value.code == ErrorCode.BUSY
    finally:
        scan.call("run.end", {})


@pytest.mark.parametrize("device,method", [
    ("daq", "write"), ("magnet", "write"),
    ("daq", "command"), ("magnet", "command"),
])
def test_rpc_deadline_drops_queued_mutation(admin_client, service, monkeypatch, device, method):
    c = admin_client
    c.acquire_lease()
    host = service.server.host
    worker = host._workers["daq"]
    entered, release = threading.Event(), threading.Event()
    touched = []
    daq = host.device("daq")
    original = daq.static_outputs

    def record(*args, **kwargs):
        touched.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(daq, "static_outputs", record)
    worker.submit(lambda: (entered.set(), release.wait(5)), 6, label="test blocker")
    assert entered.wait(2)
    try:
        with pytest.raises(ServiceError) as exc:
            if method == "write":
                c.device(device).write("coil_command_V", 0.7, timeout_s=0.15)
            else:
                command = "set_outputs" if device == "daq" else "set_coil"
                c.device(device).command(command, coil_V=0.7, timeout_s=0.15)
        assert exc.value.code == ErrorCode.TIMEOUT
    finally:
        release.set()
        worker.call(lambda: None, 3, label="test barrier")
    assert touched == []


def test_executor_queue_time_counts_towards_deadline(admin_client, service, monkeypatch):
    c = admin_client
    c.acquire_lease()
    touched = []
    monkeypatch.setattr(service.server.host, "write", lambda *a, **k: touched.append(a))
    request = protocol.make_request("device.write",
                                    {"device": "daq", "attr": "coil_command_V", "value": 0.7},
                                    c.session, 0.1)
    # Emulate an RPC that spent longer than its budget waiting for an executor.
    service.server._handle([b"expired-test-request"], protocol.dumps(request),
                           received_at=time.monotonic() - 1)
    assert touched == []


@pytest.mark.parametrize("ending", ["lease", "run"])
def test_old_queued_write_is_cancelled_when_owner_ends(admin_client, service, monkeypatch, ending):
    c = admin_client
    c.acquire_lease()
    scan = begin(c, ["daq"]) if ending == "run" else c
    host = service.server.host
    worker = host._workers["daq"]
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
    writes, errors = [], []
    daq = host.device("daq")
    original_write = daq.write_attribute
    original_host_write = host.write

    def record(*args, **kwargs):
        writes.append(args)
        return original_write(*args, **kwargs)

    def signal_submission(*args, **kwargs):
        submitted.set()
        return original_host_write(*args, **kwargs)

    monkeypatch.setattr(daq, "write_attribute", record)
    monkeypatch.setattr(host, "write", signal_submission)
    # Controlled cleanup stub isolates generation invalidation from device I/O.
    monkeypatch.setattr(host, "cleanup_all", lambda *a, **k: [])
    worker.submit(lambda: (entered.set(), release.wait(5)), 6, label="test blocker")
    assert entered.wait(2)

    def write():
        try:
            scan.device("daq").write("coil_command_V", 0.8, timeout_s=4)
        except ServiceError as exc:
            errors.append(exc.code)

    thread = threading.Thread(target=write)
    thread.start()
    try:
        assert submitted.wait(2)
        assert wait_for(lambda: worker.backlog > 0)
        if ending == "run":
            scan.call("run.end", {"cleanup": False})
        else:
            assert c.release_lease()["state"] == "FREE"
            c.acquire_lease()  # the same session must still get a new generation
    finally:
        release.set()
        thread.join(5)
        worker.call(lambda: None, 3, label="test barrier")
    assert not thread.is_alive()
    assert errors == [ErrorCode.CANCELLED]
    assert writes == []


@pytest.mark.parametrize("state", ["COMPLETED", "FAILED", "ABORTED"])
def test_engine_drains_all_pages_before_handling_final_state(admin_client, service, state):
    c = admin_client
    c.acquire_lease()
    operation = Operation("moke", "test", {}, c.session, "admin")
    for i in range(4501):
        operation.emit({"kind": "point", "index": i})
    operation._finish(state, error={"code": ErrorCode.DEVICE_FAULT, "message": "test failure"}
                      if state != "COMPLETED" else None)
    service.server.ops._ops[operation.id] = operation
    engine = ScanEngine(c, RunRequest("time", {}))
    records = []
    handle = OperationHandle(c, operation.id, operation.summary())
    if state == "COMPLETED":
        engine._consume(handle, records.append)
    else:
        with pytest.raises(ServiceError if state == "FAILED" else Interrupted):
            engine._consume(handle, records.append)
    assert [r["index"] for r in records] == list(range(4501))
    assert [r["seq"] for r in records] == list(range(1, 4502))


def test_lockin_settle_uses_applied_time_constant(admin_client, service, tmp_path, monkeypatch):
    requested = 0.11  # the simulated 7270, like the real driver, rounds this to 0.2 s
    params = dict(b_min=0.0, b_max=0.1, b_step=0.1, sweep_freq=10.0,
                  num_sweeps=1, x=0.0, y=0.0, position_stage=False,
                  **dict(FAST_LOCKIN, time_const=requested))
    moke = service.server.host.device("moke")
    actual = moke.op_field_sweep
    used = []

    def capture(ctx, **kwargs):
        used.append(dict(kwargs))
        return actual(ctx, **kwargs)

    monkeypatch.setattr(moke, "op_field_sweep", capture)
    engine, _, _ = run_engine(admin_client, "b_sweep_lockin", params, tmp_path)
    result = engine.run()
    assert result["status"] == "completed", result
    data = read_file(result["path"])
    meta = data["metadata"]
    assert json.loads(meta["params_json"])["time_const"] == requested
    assert json.loads(meta["lockin_applied_json"])["time_constant_s"] == pytest.approx(0.2)
    assert meta["field_settle_s"] == pytest.approx(1.0)
    assert used[0]["settle_s"] == pytest.approx(1.0)
    assert used[0]["t_point_s"] >= 1.0 + FAST_LOCKIN["acq_time"]


def test_old_executor_request_does_not_use_a_reacquired_lease(admin_client, service, monkeypatch):
    c = admin_client
    c.acquire_lease()
    server = service.server
    generation = server.lease.generation()
    request = protocol.make_request(
        "device.write", {"device": "daq", "attr": "coil_command_V", "value": 0.7},
        c.session, 10)
    c.release_lease()
    c.acquire_lease()
    writes = []
    monkeypatch.setattr(server.host, "write", lambda *a, **k: writes.append(a))
    server._handle([b"old-lease-request"], protocol.dumps(request),
                   received_at=time.monotonic(), lease_generation=generation)
    assert writes == []
