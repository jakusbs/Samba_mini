"""Samba_controls: hosts the device service and manages the instruments.

* Starts the backend service as a child process (or attaches to a running
  one), shows its log, and stops it cleanly on request.
* Shows every device's state, status, errors, readbacks and configuration;
  connect / disconnect / reconnect; manual controls through the same service
  API that Samba_mini uses (manual control needs the lease).
* Administrators manage local accounts and import MiniMoke settings.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                             QPushButton, QSplitter, QStatusBar, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from core.config import machine
from core.gui.bridge import ServiceBridge, call_async, error_text
from core.gui.login import LoginDialog
from core.protocol import ServiceError
from core.theme import app_style
from core.version import __version__
from Samba_controls.panels.admin import ConfigPanel, UsersPanel
from Samba_controls.panels.devices import DevicePanel, DeviceTable
from Samba_controls.process import ServiceProcess

log = logging.getLogger("Samba_controls")
REPO = Path(__file__).resolve().parents[1]


class ControlsWindow(QMainWindow):
    def __init__(self, bridge: ServiceBridge | None = None, start_service: bool = True):
        super().__init__()
        self.bridge = bridge or ServiceBridge("Samba_controls")
        self.proc = ServiceProcess(self)
        self._we_started = False
        self.setWindowTitle(f"Samba_controls v{__version__} — device service · MiniMOKE · ETH Zürich")
        icon = REPO / "assets" / "samba_controls.ico"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.setStyleSheet(app_style())
        self._build()
        self._wire()
        g = QSettings("ETH-Intermag", "SambaControls").value("geometry")
        if g is not None:
            self.restoreGeometry(g)
        else:
            self.resize(1280, 820)
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        if start_service:
            QTimer.singleShot(100, self._startup)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("action_bar")
        bar.setFixedHeight(44)
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        self.svc_badge = QLabel("SERVICE STOPPED")
        self.svc_badge.setObjectName("offline_badge")
        h.addWidget(self.svc_badge)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["mode from configuration", "real", "simulation"])
        self.mode_combo.setToolTip("Mode used when this window starts the service")
        h.addWidget(self.mode_combo)
        self.start_btn = QPushButton(" Start service")
        self.start_btn.setObjectName("start_btn")
        self.stop_btn = QPushButton(" Stop service")
        self.stop_btn.setObjectName("abort_btn")
        h.addWidget(self.start_btn)
        h.addWidget(self.stop_btn)
        h.addStretch(1)
        self.lease_lbl = QLabel("Setup: free")
        self.lease_lbl.setStyleSheet("color:#a6adc8;")
        h.addWidget(self.lease_lbl)
        self.control_btn = QPushButton("Take control")
        h.addWidget(self.control_btn)
        self.force_btn = QPushButton("Force release…")
        self.force_btn.setToolTip("Administrator: stop the current owner's work and free the setup")
        h.addWidget(self.force_btn)
        self.user_lbl = QLabel("not logged in")
        self.user_lbl.setStyleSheet("color:#a6adc8;")
        h.addWidget(self.user_lbl)
        self.login_btn = QPushButton("Log in")
        h.addWidget(self.login_btn)
        root.addWidget(bar)

        vs = QSplitter(Qt.Orientation.Vertical)
        hs = QSplitter(Qt.Orientation.Horizontal)
        self.table = DeviceTable()
        hs.addWidget(self.table)
        self.panel = DevicePanel(lambda: self.bridge)
        hs.addWidget(self.panel)
        hs.setSizes([620, 660])
        vs.addWidget(hs)
        self.bottom = QTabWidget()
        self.svc_log = QTextEdit()
        self.svc_log.setReadOnly(True)
        self.bottom.addTab(self.svc_log, "Service log")
        self.users = UsersPanel(lambda: self.bridge)
        self.bottom.addTab(self.users, "Users")
        self.cfg = ConfigPanel(lambda: self.bridge)
        self.bottom.addTab(self.cfg, "Machine configuration")
        vs.addWidget(self.bottom)
        vs.setSizes([560, 260])
        root.addWidget(vs, 1)
        self.setCentralWidget(central)
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status = QLabel("")
        sb.addWidget(self.status, 1)

    def _wire(self) -> None:
        self.start_btn.clicked.connect(self.start_service)
        self.stop_btn.clicked.connect(self.stop_service)
        self.login_btn.clicked.connect(self._login_logout)
        self.control_btn.clicked.connect(self._toggle_control)
        self.force_btn.clicked.connect(self._force_release)
        self.table.selected.connect(self.panel.show_device)
        for w in (self.panel, self.users, self.cfg):
            w.message.connect(self._msg)
        self.proc.output.connect(self._svc_line)
        self.proc.ready.connect(lambda line: QTimer.singleShot(200, self._after_ready))
        self.proc.exited.connect(self._svc_exited)
        self.bridge.event.connect(self._on_event)
        self.bridge.connection.connect(self._on_connection)
        self.bottom.currentChanged.connect(self._tab_changed)

    # ------------------------------------------------------------------ helpers
    def _msg(self, level: str, text: str) -> None:
        color = {"error": "#f38ba8", "warning": "#fab387"}.get(level, "#a6e3a1")
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{color};")
        self._svc_line(f"[{level}] {text}", color)

    def _svc_line(self, line: str, color: str | None = None) -> None:
        if color is None:
            color = "#f38ba8" if " ERROR " in line else ("#fab387" if " WARNING " in line else "#a6e3a1")
        self.svc_log.setTextColor(QColor(color))
        self.svc_log.append(line)

    def _set_badge(self, text: str, name: str) -> None:
        self.svc_badge.setText(text)
        self.svc_badge.setObjectName(name)
        self.svc_badge.style().unpolish(self.svc_badge)
        self.svc_badge.style().polish(self.svc_badge)

    # ------------------------------------------------------------------ service
    def _startup(self) -> None:
        info = self.bridge.probe(1.0)
        if info is None:
            self.start_service()
        else:
            self._msg("info", f"Attached to the running service (pid {info['pid']}, mode {info['mode']}).")
            self._after_ready()

    def start_service(self) -> None:
        if self.bridge.probe(1.0) is not None:
            self._after_ready()
            return
        mode = None if self.mode_combo.currentIndex() == 0 else self.mode_combo.currentText()
        cfg = machine.load()
        for p in cfg.problems:
            self._msg("warning", f"configuration: {p}")
        self._set_badge("STARTING…", "sim_badge")
        self.proc.start(mode)
        self._we_started = True

    def _after_ready(self) -> None:
        info = self.bridge.probe(2.0)
        if info is None:
            return
        mode = info["mode"]
        self._set_badge("SERVICE: SIMULATION" if mode == "simulation" else "SERVICE: REAL HARDWARE",
                        "sim_badge" if mode == "simulation" else "real_badge")
        if not self.bridge.client.session:
            self._login()
        self._poll()
        self.cfg.refresh()

    def stop_service(self) -> bool:
        c = self.bridge.client
        if self.bridge.probe(1.0) is None:
            return True
        try:
            st = c.call("server.status") if c.session else {}
        except ServiceError:
            st = {}
        force = False
        if st.get("run_active") or st.get("active_operation"):
            ans = QMessageBox.question(self, "Measurement running",
                                       "A measurement is running. Stopping the service aborts it and runs the "
                                       "cleanup (coil 0 V, lock-in output 0 V, stages stopped). Continue?")
            if ans != QMessageBox.StandardButton.Yes:
                return False
            force = True
        if not self._we_started and (c.user or {}).get("role") != "admin":
            QMessageBox.warning(self, "Stop service", "This service was not started by this window; "
                                "log in as an administrator to stop it.")
            return False
        self._msg("info", "Stopping the service (abort, cleanup, disconnect)…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok = self.proc.stop(c, force=force)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok and self.proc.running:
            ans = QMessageBox.question(self, "Service not stopping",
                                       "The service did not exit. Terminate it?\n\nWARNING: no cleanup will run; "
                                       "outputs keep their last values. Check the setup by hand.")
            if ans == QMessageBox.StandardButton.Yes:
                self.proc.kill()
        return ok

    def _svc_exited(self, code: int) -> None:
        self._set_badge(f"SERVICE STOPPED (exit {code})", "offline_badge")
        self.bridge.client.session = None
        self.bridge.client.lease_holder = False
        self.user_lbl.setText("not logged in")
        self.login_btn.setText("Log in")
        meaning = {0: "", 3: " — another service instance is already running", 4: " — port in use"}.get(code, "")
        self._msg("warning" if code else "info", f"Service exited with code {code}{meaning}")

    # ------------------------------------------------------------------ login / lease
    def _login(self) -> None:
        dlg = LoginDialog(self.bridge, "Samba_controls", self)
        if dlg.exec() == LoginDialog.DialogCode.Accepted:
            u = self.bridge.client.user
            self.user_lbl.setText(f"{u['username']} ({u['role']})")
            self.login_btn.setText("Log out")
            self.users.refresh()
            self._poll()

    def _login_logout(self) -> None:
        c = self.bridge.client
        if c.session:
            try:
                if c.lease_holder:
                    c.release_lease()
                c.logout()
            except ServiceError as exc:
                QMessageBox.warning(self, "Log out", error_text(exc))
                return
            self.user_lbl.setText("not logged in")
            self.login_btn.setText("Log in")
        else:
            if self.bridge.probe(1.0) is None:
                QMessageBox.information(self, "Service", "Start the service first.")
                return
            self._login()

    def _toggle_control(self) -> None:
        c = self.bridge.client
        if not c.session:
            self._login()
            return
        try:
            if c.lease_holder:
                c.release_lease()
            else:
                c.acquire_lease()
        except ServiceError as exc:
            QMessageBox.warning(self, "Control of the setup", error_text(exc))
        self._poll()

    def _force_release(self) -> None:
        c = self.bridge.client
        if (c.user or {}).get("role") != "admin":
            QMessageBox.warning(self, "Force release", "Administrators only.")
            return
        if QMessageBox.question(self, "Force release",
                                "Stop the current owner's operations, run the cleanup and free the setup?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        call_async(lambda: c.call("lease.force_release", {}, 120.0),
                   lambda st: self._msg("info", f"Lease: {st['state']}"),
                   lambda e: self._msg("error", error_text(e)))

    # ------------------------------------------------------------------ polling / events
    def _poll(self) -> None:
        c = self.bridge.client
        if not c.session:
            return
        call_async(lambda: c.call("server.status", {}, 5.0), self._show_status, lambda e: None)

    def _show_status(self, st: dict) -> None:
        self.table.update_devices(st["devices"])
        lease = st["lease"]
        if lease["state"] == "HELD":
            h = lease["holder"]
            mine = self.bridge.client.lease_holder
            self.lease_lbl.setText(f"Setup controlled by {h['user']} ({h['client']})" + (" — you" if mine else ""))
        elif lease["state"] == "RECONCILING":
            self.lease_lbl.setText("Setup: stopping previous owner's work…")
        else:
            self.lease_lbl.setText("Setup: free")
        last = lease.get("last_reconcile")
        if last and not last.get("ok", True):
            self.lease_lbl.setText(self.lease_lbl.text() + "  ⚠ last cleanup not confirmed")
            self.lease_lbl.setToolTip("\n".join(last.get("problems", [])))
        self.control_btn.setText("Release control" if self.bridge.client.lease_holder else "Take control")
        run = st.get("run_info")
        if st.get("run_active") and run:
            self.status.setText(f"Measurement running: {run.get('workflow')} by {run.get('user')}")

    def _on_event(self, topic: str, body: dict) -> None:
        if topic.startswith("state."):
            self.table.set_state(body.get("device", ""), body.get("state", ""), body.get("status", ""))
            if body.get("state") == "FAULT":
                self._svc_line(f"[fault] {body.get('device')}: {body.get('status')}", "#f38ba8")
        elif topic == "lease":
            self._poll()

    def _on_connection(self, what: str, info: dict) -> None:
        if what == "disconnected" and not self.proc.running:
            self._set_badge("SERVICE STOPPED", "offline_badge")
        elif what == "lease_lost":
            self._msg("warning", "You lost control of the setup.")

    def _tab_changed(self, idx: int) -> None:
        w = self.bottom.widget(idx)
        if w is self.users:
            self.users.refresh()
        elif w is self.cfg:
            self.cfg.refresh()

    # ------------------------------------------------------------------ close
    def closeEvent(self, ev) -> None:
        QSettings("ETH-Intermag", "SambaControls").setValue("geometry", self.saveGeometry())
        if self._we_started and self.proc.running:
            ans = QMessageBox.question(
                self, "Close Samba_controls",
                "Stop the device service too?\n\nYes: stop it cleanly (Samba_mini loses access).\n"
                "No: leave it running in the background.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if ans == QMessageBox.StandardButton.Cancel:
                ev.ignore()
                return
            if ans == QMessageBox.StandardButton.Yes and not self.stop_service():
                ev.ignore()
                return
        c = self.bridge.client
        try:
            if c.session:
                if c.lease_holder:
                    c.release_lease()
                c.logout()
        except ServiceError:
            pass
        self.bridge.close()
        super().closeEvent(ev)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from core.gui import set_windows_app_id
    set_windows_app_id("ETH.Intermag.SambaControls")
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Samba_controls")
    if (REPO / "assets" / "samba_controls.ico").exists():
        app.setWindowIcon(QIcon(str(REPO / "assets" / "samba_controls.ico")))
    app.setOrganizationName("ETH-Intermag")
    w = ControlsWindow()
    w.show()
    return app.exec()
