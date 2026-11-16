"""The service as a real separate process: startup, duplicate prevention, shutdown."""

import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from core.client import ServiceClient
from core.config import machine
from core.protocol import ErrorCode, ServiceError
from tests.conftest import free_port

ROOT = Path(__file__).resolve().parents[1]


def _spawn(home, rpc, ev, token):
    env = dict(os.environ, SAMBA_MINI_HOME=str(home), SAMBA_MINI_CONTROL_TOKEN=token, PYTHONUNBUFFERED="1",
               SAMBA_MINI_FAST_HASH="1")
    return subprocess.Popen([sys.executable, "-m", "Samba_controls.service", "--mode", "simulation",
                             "--rpc-port", str(rpc), "--event-port", str(ev)], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_service_process_lifecycle(tmp_path):
    home = tmp_path / "home"
    rpc, ev = free_port(), free_port()
    p = _spawn(home, rpc, ev, "tok-1")
    try:
        lines = []
        t0 = time.time()
        while time.time() - t0 < 60:
            line = p.stdout.readline()
            if not line:
                break
            lines.append(line)
            if line.startswith("READY"):
                break
        assert any(l.startswith("READY") for l in lines), "".join(lines)
        c = ServiceClient(rpc, ev, "test")
        info = c.hello()
        assert info["mode"] == "simulation" and info["pid"] == p.pid
        # A second instance on the same machine (other ports) is refused by the lock file.
        p2 = _spawn(home, free_port(), free_port(), "tok-2")
        out2, _ = p2.communicate(timeout=60)
        assert p2.returncode == 3, out2
        # Shutdown needs the control token (or an administrator session).
        try:
            c.call("server.shutdown", {"control_token": "wrong"})
            raise AssertionError("shutdown accepted a wrong token")
        except ServiceError as exc:
            assert exc.code in (ErrorCode.AUTH_REQUIRED, ErrorCode.FORBIDDEN)
        c.call("server.shutdown", {"control_token": "tok-1"})
        assert p.wait(60) == 0
        c.close()
        log = (home / "logs" / "service.log").read_text(encoding="utf-8")
        assert "service stopping" in log
    finally:
        if p.poll() is None:
            p.kill()


def test_example_config_is_valid():
    path = ROOT / "config" / "examples" / "machine.example.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    merged = machine.deep_merge(machine.DEFAULTS, data)
    assert machine.validate(merged) == []
    assert merged["backend"]["mode"] == "simulation"
