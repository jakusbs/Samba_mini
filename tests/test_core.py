"""Protocol, workers, configuration, accounts, storage and process boundaries."""

import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import h5py
import numpy as np
import pytest

from core import protocol
from core.protocol import ErrorCode, ServiceError

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


# --------------------------------------------------------------------------- protocol
def test_protocol_roundtrip_nan_and_arrays():
    msg = {"a": math.nan, "b": [1.5, math.inf, -math.inf], "arr": np.arange(6, dtype=float).reshape(2, 3)}
    back = protocol.loads(protocol.dumps(msg))
    assert math.isnan(back["a"]) and back["b"][1] == math.inf and back["b"][2] == -math.inf
    assert np.array_equal(back["arr"], msg["arr"])


def test_protocol_rejects_unsafe_payloads():
    with pytest.raises(TypeError):
        protocol.dumps({"x": np.array(["a"], dtype=object)})
    bad = json.dumps({"__ndarray__": "AAAA", "dtype": "|O", "shape": [1]}).encode()
    with pytest.raises(ServiceError):
        protocol.loads(bad)
    with pytest.raises(ServiceError) as e:
        protocol.validate_request({"proto": "samba-mini/2", "id": "x" * 32, "method": "m"})
    assert e.value.code == ErrorCode.PROTOCOL_MISMATCH
    with pytest.raises(ServiceError):
        protocol.loads(b"{not json")


def test_unknown_method_and_command_rejected(admin_client):
    with pytest.raises(ServiceError) as e:
        admin_client.call("os.system", {"cmd": "calc"})
    assert e.value.code == ErrorCode.UNKNOWN_METHOD
    with pytest.raises(ServiceError) as e:
        admin_client.device("daq").command("_hw_static", values=[0, 0, 5])
    assert e.value.code == ErrorCode.UNKNOWN_COMMAND


# --------------------------------------------------------------------------- workers
def test_expired_job_never_runs():
    from Samba_controls.service.worker import CancelToken, DeviceWorker
    w = DeviceWorker("t")
    ran = []
    blocker = threading.Event()
    w.submit(lambda: blocker.wait(2), 5.0, label="busy")
    job = w.submit(lambda: ran.append("late write"), 0.2, label="write")
    with pytest.raises(ServiceError) as e:
        w.wait(job, 0.2)
    assert e.value.code == ErrorCode.TIMEOUT and e.value.details["started"] is False
    tok = CancelToken()
    job2 = w.submit(lambda: ran.append("cancelled write"), 5.0, tok, "write2")
    tok.cancel("abort")
    blocker.set()
    with pytest.raises(ServiceError) as e:
        w.wait(job2, 5.0)
    assert e.value.code == ErrorCode.CANCELLED
    time.sleep(0.1)
    assert ran == []                                  # neither late write touched "hardware"
    w.stop()


def test_driver_timeout_reports_outcome_unknown():
    from Samba_controls.service.worker import DeviceWorker
    w = DeviceWorker("t")
    job = w.submit(lambda: time.sleep(0.5), 0.1, label="slow")
    with pytest.raises(ServiceError) as e:
        w.wait(job, 0.1)
    assert e.value.details["started"] is True and "unknown" in e.value.message
    w.stop()


# --------------------------------------------------------------------------- configuration
def test_config_defaults_migration_and_malformed(samba_home):
    from core import paths
    from core.config import machine
    cfg = machine.load()
    assert cfg.status == "defaults" and cfg.mode == "simulation"
    p = paths.machine_config_path()
    p.write_text('schema_version = 0\n[backend]\nmode = "real"\n[lockin]\nresource = "TCPIP0::1.2.3.4::50000::SOCKET"\n',
                 encoding="utf-8")
    cfg = machine.load()
    assert cfg.status == "migrated" and cfg.data["schema_version"] == machine.SCHEMA_VERSION
    assert cfg.data["lockin"]["resource"].startswith("TCPIP0::1.2.3.4")
    assert list(paths.backups_dir().glob("machine.v0.*.toml"))          # backup before migration
    p.write_text("this is [not toml", encoding="utf-8")
    cfg = machine.load()
    assert cfg.status == "invalid" and cfg.mode == "real"               # never silently simulation
    assert not cfg.data["daq"]["enabled"] and not cfg.data["lockin"]["enabled"]
    assert list(p.parent.glob("machine.toml.bad-*"))                     # malformed copy preserved
    p.write_text('[coil]\nmin_V = 5\nmax_V = 2\n', encoding="utf-8")
    cfg = machine.load()
    assert cfg.status == "invalid" and any("coil" in x for x in cfg.problems)


def test_minimoke_import_reads_only(tmp_path, samba_home):
    from core.config import machine, minimoke_import
    src = tmp_path / "MiniMoke"
    (src / "configs").mkdir(parents=True)
    (src / "libs").mkdir()
    (src / "libs" / "MagnetPhysik.Usb.dll").write_bytes(b"x")
    (src / "configs" / "dac_config.ini").write_text("[IO ports]\nCoils_Output = Dev2/ao3\nBalanced_diodes_Input = Dev2/ai0\n")
    (src / "configs" / "instruments_config.local.ini").write_text("[LockIn]\nresource = TCPIP0::192.168.77.2::50000::SOCKET\n")
    (src / "configs" / "longitudinal_stage_config.ini").write_text(
        "[Offsets]\noffset_x = 1\noffset_y = 2\noffset_z = 3\n[Serials]\nx = 11\ny = 12\nz = 13\n"
        "[Constants]\nmm2steps = 2184533\nbacklash_mm = 0.02\n")
    (src / "configs" / "procedures_config.ini").write_text(
        "[B-Sweep]\nb_max = 0.7\nb_min = -0.7\nb_step = 0.005\nnum_sweeps = 2\nsweep_freq = 0.05\n"
        "[Unknown]\nfoo = 1\n")
    before = {p: p.stat().st_mtime for p in src.rglob("*")}
    res = minimoke_import.read_minimoke(src)
    assert res["machine"]["daq"]["ao_coil"] == "Dev2/ao3"
    assert res["machine"]["daq"]["trigger_terminal"] == "/Dev2/PFI0"
    assert res["machine"]["stages"]["longitudinal"]["serial_y"] == "12"
    assert res["presets"]["b_sweep_dc"]["b_step"] == 0.005
    written = minimoke_import.apply(res, mode="real")
    cfg = machine.load()
    assert cfg.mode == "real" and cfg.data["lockin"]["resource"].startswith("TCPIP0::192.168.77.2")
    assert json.loads(Path(written["offsets"]).read_text())["longitudinal"]["x"] == 1.0
    assert {p: p.stat().st_mtime for p in src.rglob("*")} == before     # MiniMoke untouched


# --------------------------------------------------------------------------- accounts
def test_accounts_hashing_prefs_and_migration_guards(tmp_path):
    from Samba_controls.service.auth import AccountStore
    path = tmp_path / "acc.sqlite3"
    a = AccountStore(path)
    u = a.create_user("bob", "correct horse 1", "admin")
    a.set_prefs(u["id"], {"data_dir": "D:/data", "export_csv": True})
    a.save_preset(u["id"], "samba_mini/polar", "loop", {"workflow": "b_sweep_dc", "params": {"b_max": 1}})
    a.close()
    raw = path.read_bytes() + (path.with_name(path.name + "-wal").read_bytes()
                               if path.with_name(path.name + "-wal").exists() else b"")
    assert b"correct horse 1" not in raw                                # no plaintext
    b = AccountStore(path)                                             # reopen: persistence
    assert b.verify("bob", "correct horse 1")["role"] == "admin"
    row = b._db.execute("SELECT pw_hash FROM users").fetchone()[0]
    assert row.startswith("$argon2id$")
    assert b.get_prefs(u["id"])["data_dir"] == "D:/data"
    assert b.list_presets(u["id"], "samba_mini/polar")[0]["value"]["params"]["b_max"] == 1
    with pytest.raises(ServiceError):
        b.verify("bob", "wrong")
    with pytest.raises(ServiceError):
        b.set_role(u["id"], "operator")                                 # last admin protected
    with pytest.raises(ServiceError):
        b.create_user("x", "short")
    b.close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO schema_version VALUES (99, 0)")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError):
        AccountStore(path)                                             # newer schema refused


def test_logout_blocked_during_run_and_manual_control_busy(admin_client):
    admin_client.acquire_lease()
    admin_client.call("run.begin", {"required_devices": ["daq"]})
    moke = admin_client.device("moke")
    op = moke.start_operation("field_sweep", setpoints_V=[0.0] * 200, branch=[0] * 200, t_point_s=0.02,
                              settle_s=0.0, acq_time_s=0.005, hall=False, lockin=False)
    with pytest.raises(ServiceError) as e:
        admin_client.device("daq").command("set_outputs", coil_V=1.0)  # races the acquisition
    assert e.value.code == ErrorCode.BUSY
    with pytest.raises(ServiceError) as e:
        admin_client.call("auth.logout")
    assert e.value.code == ErrorCode.BUSY
    r = admin_client.device("hall").read("field_mT")["field_mT"]          # coordinated readback
    assert r.source.startswith("cache")
    op.abort()
    while not op.done:
        op.poll(0.5)
    admin_client.call("run.end", {})


# --------------------------------------------------------------------------- storage
WRITER_SCRIPT = r"""
import sys, time
sys.path.insert(0, sys.argv[2])
from core.storage.hdf5_writer import ScanWriter, DatasetSpec
w = ScanWriter(sys.argv[1], (100,), [DatasetSpec("dc_signal", "sensor", "DC", "V")], {"scan_type": "TIME"},
               {"sample_id": "s"}, flush_every=10)
w.create()
for i in range(100):
    w.write((i,), {"dc_signal": float(i)})
    if i == 34:
        print("WROTE35", flush=True)
        time.sleep(30)
"""


def test_interrupted_run_file_is_readable_and_marked_partial(tmp_path):
    path = tmp_path / "crash.h5"
    script = tmp_path / "writer.py"
    script.write_text(WRITER_SCRIPT)
    p = subprocess.Popen([PY, str(script), str(path), str(ROOT)], stdout=subprocess.PIPE, text=True)
    assert "WROTE35" in p.stdout.readline()
    p.kill()                                            # simulated crash: no finalisation
    p.wait()
    with h5py.File(path, "r") as f:
        assert f.attrs["scan_status"] == "running"      # identifiable as partial
        dc = f["data/dc_signal"][:]
        n_ok = int(np.isfinite(dc).sum())
        assert 30 <= n_ok <= 35                         # up to the last flush (every 10 points)
        assert np.array_equal(dc[:30], np.arange(30.0))
        assert np.isnan(dc[35:]).all()                  # never zeros


def test_disk_error_auto_pauses_and_keeps_points(admin_client, tmp_path, monkeypatch):
    from core.scan.engine import Callbacks, RunRequest, ScanEngine
    from core.storage import hdf5_writer
    fails = {"n": 0}
    orig = hdf5_writer.ScanWriter.write

    def flaky(self, index, values, group="data", count_point=True):
        if group == "data" and count_point and self.points_acquired == 2 and fails["n"] < 2:
            fails["n"] += 1
            raise hdf5_writer.StorageError("disk full (simulated)")
        return orig(self, index, values, group, count_point)
    monkeypatch.setattr(hdf5_writer.ScanWriter, "write", flaky)
    states = []
    params = dict(b=0.0, duration=0.4, interval=0.05, x=0, y=0, position_stage=False, record_hall=False,
                  volt=1.0, sensi=1e-3, lockin_freq=1777.0, time_const=0.1, phase=0.0, acq_time=0.005)
    eng = ScanEngine(admin_client, RunRequest("time", params, save_dir=str(tmp_path)),
                     Callbacks(state=lambda s, m: states.append((s, m))))
    eng.start()
    t0 = time.time()
    while eng.state != "PAUSED" and time.time() - t0 < 20:
        time.sleep(0.02)
    assert eng.state == "PAUSED" and "Cannot write" in eng.message
    eng.resume()                                         # write fails once more -> paused again
    t0 = time.time()
    while fails["n"] < 2 and time.time() - t0 < 10:
        time.sleep(0.02)
    t0 = time.time()
    while eng.result is None and time.time() - t0 < 20:
        if eng.state == "PAUSED":
            eng.resume()
        time.sleep(0.05)
    assert eng.result["status"] == "completed"
    from core.storage.hdf5_writer import read_file
    f = read_file(eng.result["path"])
    assert np.isfinite(f["data"]["dc_signal"]["values"]).all()   # nothing lost
    assert f["metadata"]["unsaved_points"] == 0


# --------------------------------------------------------------------------- boundaries
FORBIDDEN = ["nidaqmx", "pyvisa", "clr", "pythonnet", "pylablib", "pytrinamic", "PyQt5",
             "Samba_controls.service", "pymeasure"]


def test_samba_mini_never_loads_drivers_or_service():
    code = ("import sys, os; os.environ['QT_QPA_PLATFORM']='offscreen'; sys.path.insert(0, r'%s');"
            "import Samba_mini.app, Samba_controls.app; "
            "bad=[m for m in sys.modules if any(m==f or m.startswith(f+'.') for f in %r)]; print(bad)"
            % (ROOT, FORBIDDEN))
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


def test_qt_free_core_and_service():
    mods = ["core.protocol", "core.model", "core.client", "core.scan.engine", "core.scan.workflows",
            "core.storage.hdf5_writer", "core.storage.csv_export", "core.config.machine",
            "Samba_controls.service.server"]
    code = ("import sys; sys.path.insert(0, r'%s'); import %s; "
            "print([m for m in sys.modules if m.startswith(('PyQt5','PyQt6'))])" % (ROOT, ", ".join(mods)))
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


def test_minimoke_import_prefers_unmarked_dll(tmp_path, samba_home, monkeypatch):
    from core.config import minimoke_import
    src = tmp_path / "MiniMoke-zip"
    (src / "configs").mkdir(parents=True)
    (src / "libs").mkdir()
    marked = src / "libs" / "MagnetPhysik.Usb.dll"
    marked.write_bytes(b"x")
    with open(f"{marked}:Zone.Identifier", "w") as f:
        f.write("[ZoneTransfer]\nZoneId=3\n")
    fake_home = tmp_path / "user"
    good = fake_home / "Documents" / "minimoke" / "libs" / "MagnetPhysik.Usb.dll"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"x")
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    res = minimoke_import.read_minimoke(src)
    assert res["machine"]["hall"]["dll_path"] == str(good)
    assert any("marked as downloaded" in n for n in res["notes"])
