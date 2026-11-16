"""Start/stop the device service as a child process of Samba_controls.

* The service is a separate Python process (``python -m Samba_controls.service``)
  so blocking driver calls never touch a GUI thread and a GUI crash does not
  take the instrument sessions down uncleanly.
* A random control token is passed through the environment; with it this
  Samba_controls can stop the service without an administrator login.
* Duplicate services are prevented by the service itself (lock file + port).
* Stop = ``server.shutdown`` (the service aborts operations, runs cleanup,
  disconnects devices, exits).  Only if it does not exit in time is the user
  asked whether to terminate it -- then no cleanup runs, and the UI says so.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from core.client import ServiceClient
from core.protocol import ServiceError

REPO = Path(__file__).resolve().parents[1]


class ServiceProcess(QObject):
    output = pyqtSignal(str)
    ready = pyqtSignal(str)
    exited = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: subprocess.Popen | None = None
        self.token: str | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, mode: str | None = None, extra_args: list[str] | None = None) -> None:
        if self.running:
            return
        self.token = secrets.token_urlsafe(24)
        env = dict(os.environ, SAMBA_MINI_CONTROL_TOKEN=self.token, PYTHONUNBUFFERED="1")
        args = [sys.executable, "-m", "Samba_controls.service"]
        if mode:
            args += ["--mode", mode]
        args += extra_args or []
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(args, cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", creationflags=flags)
        threading.Thread(target=self._pump, args=(self.proc,), name="service-stdout", daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            line = line.rstrip()
            self.output.emit(line)
            if line.startswith("READY"):
                self.ready.emit(line)
        code = proc.wait()
        self.exited.emit(code)

    def stop(self, client: ServiceClient, force: bool = False, wait_s: float = 90.0) -> bool:
        """Ask the service to shut down cleanly.  Returns True if it exited."""
        params = {"control_token": self.token or "", "force": force}
        try:
            client.call("server.shutdown", params, 10.0)
        except ServiceError as exc:
            self.output.emit(f"[Samba_controls] shutdown refused: {exc.message}")
            return False
        if self.proc is not None:
            try:
                self.proc.wait(wait_s)
            except subprocess.TimeoutExpired:
                return False
        return True

    def kill(self) -> None:
        if self.running:
            self.proc.terminate()
