import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SAMBA_MINI_FAST_HASH", "1")
# Per-user data folders are created at login: never in the real Documents folder during tests.
import tempfile  # noqa: E402

os.environ.setdefault("SAMBA_MINI_DATA_ROOT", tempfile.mkdtemp(prefix="samba-mini-data-"))

ADMIN = ("admin", "admin-password-1")
OPERATOR = ("alice", "alice-password-1")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def samba_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SAMBA_MINI_HOME", str(home))
    monkeypatch.setenv("SAMBA_MINI_DATA_ROOT", str(tmp_path / "measurements"))
    return home


class RunningService:
    def __init__(self, server, thread):
        self.server = server
        self.thread = thread
        self.rpc_port = server.rpc_port
        self.event_port = server.event_port

    def client(self, name="test"):
        from core.client import ServiceClient
        c = ServiceClient(self.rpc_port, self.event_port, name)
        self._clients.append(c)
        return c

    def stop(self):
        for c in self._clients:
            c.close()
        self.server.stop("test teardown")
        self.thread.join(20)


def start_service(home: Path, overrides: dict | None = None, connect: bool = True,
                  lease_ttl_s: float = 15.0) -> RunningService:
    from core import paths
    from core.config import machine
    from Samba_controls.service.server import ServiceServer

    data = machine.deep_merge(machine.DEFAULTS, overrides or {})
    data["backend"]["lease_ttl_s"] = lease_ttl_s
    data["backend"]["reconcile_wait_s"] = 10.0
    cfg = machine.MachineConfig(data, None, "loaded", [])
    server = ServiceServer(cfg, paths.accounts_db_path(), paths.state_dir(), control_token="ctl-token",
                           connect=False, rpc_port=free_port(), event_port=free_port())
    server.bind()
    server.start_background()
    if connect:
        server.host.connect_all()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    rs = RunningService(server, t)
    rs._clients = []
    return rs


@pytest.fixture
def service(samba_home):
    rs = start_service(samba_home)
    yield rs
    rs.stop()


@pytest.fixture
def admin_client(service):
    c = service.client("pytest-admin")
    c.hello()
    c.bootstrap(*ADMIN)
    c.login(*ADMIN)
    return c
