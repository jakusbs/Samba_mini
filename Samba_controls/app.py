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
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                             QPushButton, QSplitter, QStatusBar, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from core.config import machine
from core.gui.bridge import ServiceBridge, call_async, error_text
from core.gui.login import LoginDialog
from core.gui.outputs import ask_keep_outputs
from core.protocol import ServiceError
from core.theme import app_style
from core.version import __version__
from Samba_controls.panels import admin as admin_panel
from Samba_controls.panels.admin import ConfigPanel, UsersPanel
from Samba_controls.panels.channel_editor import ChannelEditor
from Samba_controls.panels.device_editor import DeviceEditor
from Samba_controls.panels.devices import DevicePanel, DeviceTable
from Samba_controls.panels.lights import StartupLights
from Samba_controls.process import ServiceProcess

log = logging.getLogger("Samba_controls")
REPO = Path(__file__).resolve().parents[1]


class ControlsWindow(QMainWindow):
    def __init__(self, bridge: ServiceBridge | None = None, start_service: bool = True):
        super().__init__()
        self.bridge = bridge or ServiceBridge("Samba_controls")
        self.proc = ServiceProcess(self)
        self._we_started = False
        self._pending_presets: dict | None = None   # MiniMoke presets imported before anyone logged in
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
        # Start-up lights: the public device list, also before anyone logs in.
        self._dev_poll_busy = False
        self._dev_timer = QTimer(self)
        self._dev_timer.setInterval(1000)
        self._dev_timer.timeout.connect(self._poll_devices)
        self._dev_timer.start()
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
        self.start_btn = QPushButton(" Start service")
        self.start_btn.setToolTip("Start the device service on the real instruments")
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
        self.lights = StartupLights()
        root.addWidget(self.lights)

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
        self.devedit = DeviceEditor(lambda: self.bridge)
        self.bottom.addTab(self.devedit, "Device settings")
        self.chanedit = ChannelEditor(lambda: self.bridge)
        self.bottom.addTab(self.chanedit, "Channels")
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
        self.lights.device_clicked.connect(self.table.select_device)
        self.devedit.message.connect(self._msg)
        self.devedit.saved.connect(self._offer_restart)
        self.chanedit.message.connect(self._msg)
        self.chanedit.saved.connect(self._offer_restart)
        # both edit machine.toml: after one saves, the other re-reads (unless it has unsaved edits)
        self.devedit.saved.connect(lambda: self.chanedit.is_dirty() or self.chanedit.refresh())
        self.chanedit.saved.connect(lambda: self.devedit.is_dirty() or self.devedit.refresh())
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
        """Always the real instruments: simulation is only for development, from the command line
        (``scripts\\service_console.bat --mode simulation``), never from this window."""
        mode = "real"
        info = self.bridge.probe(1.0)
        if info is not None:
            if info["mode"] != mode:
                self._msg("warning", f"A {info['mode']} service (started from the command line) is running; "
                                     "stop it to start the real one.")
            self._after_ready()
            return
        cfg = machine.load()
        if cfg.status == "defaults":            # no machine.toml on this PC yet
            if not self._offer_minimoke_import():
                self._msg("info", "Service not started: the instruments need a machine configuration "
                                  "(their addresses). Import MiniMoke's settings to create it.")
                return
            cfg = machine.load()
        for p in cfg.problems:
            self._msg("warning", f"configuration: {p}")
        self._set_badge("STARTING…", "sim_badge")
        self.proc.start(mode)
        self.lights.set_service_starting()
        self._we_started = True

    def _offer_minimoke_import(self) -> bool:
        """Real mode chosen but no machine.toml: offer to create it from MiniMoke's settings.

        Like creating the first administrator, this bootstrap step needs no
        login, and it is only offered while no configuration file exists; later
        changes go through an administrator (Machine configuration tab)."""
        box = QMessageBox(QMessageBox.Icon.Question, "No machine configuration",
                          "There is no machine configuration on this PC yet, so the service does not know "
                          "the instrument addresses (lock-in, Hall probe driver, stage serial numbers).\n\n"
                          "Import them from MiniMoke? MiniMoke's files are only read. The new configuration "
                          "is set to real hardware.",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.button(QMessageBox.StandardButton.Yes).setText("Import from MiniMoke…")
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return False
        src = admin_panel.choose_minimoke_folder(self)
        if not src:
            return False
        res = admin_panel.confirm_and_apply(self, src, "real")
        if res is None:
            return False
        self._pending_presets = res["presets"] or None
        self._msg("info", f"Machine configuration created from {src}: {res['written'].get('config')}")
        self.cfg.refresh()
        return True

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
        self.lights.set_service_stopped(f"service stopped (exit {code})")
        self.bridge.client.session = None
        self.bridge.client.lease_holder = False
        self.user_lbl.setText("not logged in")
        self.login_btn.setText("Log in")
        self.panel.reset("Service stopped")
        meaning = {0: "", 3: " — another service instance is already running", 4: " — port in use"}.get(code, "")
        self._msg("warning" if code else "info", f"Service exited with code {code}{meaning}")

    # ------------------------------------------------------------------ login / lease
    def _login(self) -> None:
        dlg = LoginDialog(self.bridge, "Samba_controls", self)
        if dlg.exec() == LoginDialog.DialogCode.Accepted:
            self.panel.reset()                      # nothing shown belongs to the new session yet
            u = self.bridge.client.user
            self.user_lbl.setText(f"{u['username']} ({u['role']})")
            self.login_btn.setText("Log out")
            if self._pending_presets:
                presets, self._pending_presets = self._pending_presets, None
                problems = admin_panel.save_minimoke_presets(self.bridge.client, presets)
                for p in problems:
                    self._msg("warning", p)
                if not problems:
                    self._msg("info", f"MiniMoke procedure settings saved as presets for {u['username']}.")
            self.users.refresh()
            self._poll()

    def _login_logout(self) -> None:
        c = self.bridge.client
        if c.session:
            keep = ask_keep_outputs(self, c) if c.lease_holder else []
            if keep is None:
                return
            try:
                if c.lease_holder:
                    c.release_lease(keep_outputs=keep)
                c.logout()
            except ServiceError as exc:
                QMessageBox.warning(self, "Log out", error_text(exc))
                return
            self.user_lbl.setText("not logged in")
            self.login_btn.setText("Log in")
            self.panel.reset("Log in to see the devices")
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
                keep = ask_keep_outputs(self, c)
                if keep is None:
                    return
                c.release_lease(keep_outputs=keep)
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
                                "Stop the current owner's operations, reconnect faulted instruments, run the "
                                "cleanup and free the setup?\n\nIf an instrument cannot be reached its cleanup "
                                "(e.g. the coil or lock-in oscillator off) cannot be confirmed: check it by hand."
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

    def _poll_devices(self) -> None:
        """Feed the start-up lights from the public device list (no login needed)."""
        if self._dev_poll_busy or not self.isVisible():
            return
        c = self.bridge.client
        self._dev_poll_busy = True

        def ok(res):
            self._dev_poll_busy = False
            self.lights.update_devices(res)
            if self.svc_badge.text().startswith("SERVICE STOPPED"):      # e.g. started from the console
                sim = res.get("mode") == "simulation"
                self._set_badge("SERVICE: SIMULATION" if sim else "SERVICE: REAL HARDWARE",
                                "sim_badge" if sim else "real_badge")

        def err(exc):
            self._dev_poll_busy = False
            if not self.proc.running:
                self.lights.set_service_stopped("service not running")
        call_async(lambda: c.call("server.devices", {}, 2.0), ok, err)

    def _offer_restart(self) -> None:
        if QMessageBox.question(self, "Device settings saved",
                                "Restart the device service now so the new device settings apply?\n\n"
                                "The instruments are disconnected (with the usual cleanup) and connect again "
                                "one after another. Not possible while a measurement runs.") \
                == QMessageBox.StandardButton.Yes:
            self.restart_service()

    def restart_service(self) -> bool:
        """Stop the service cleanly, wait until it is gone, start it again (new settings)."""
        if not self.stop_service():
            return False
        t0 = time.time()
        while self.bridge.probe(0.5) is not None and time.time() - t0 < 60:
            QApplication.processEvents()
            time.sleep(0.2)
        if self.bridge.probe(0.5) is not None:
            self._msg("error", "The service did not stop within 60 s; not restarted.")
            return False
        self.start_service()
        return True

    def _on_event(self, topic: str, body: dict) -> None:
        if topic == "startup":
            self.lights.on_startup(body)
        elif topic.startswith("state."):
            self.lights.on_state(body.get("device", ""), body.get("state", ""), body.get("status", ""))
        if topic.startswith("state."):
            self.table.set_state(body.get("device", ""), body.get("state", ""), body.get("status", ""))
            self.panel.on_state(body.get("device", ""), body.get("state", ""), body.get("status", ""))
            if body.get("state") == "FAULT":
                self._svc_line(f"[fault] {body.get('device')}: {body.get('status')}", "#f38ba8")
        elif topic == "lease":
            self._poll()

    def _on_connection(self, what: str, info: dict) -> None:
        if what == "disconnected" and not self.proc.running:
            self._set_badge("SERVICE STOPPED", "offline_badge")
        elif what == "lease_lost":
            self._msg("warning", "You lost control of the setup.")
        elif what in ("session_lost", "restarted"):
            self.panel.reset("Log in again to see the devices")
            if what == "session_lost":
                self.user_lbl.setText("session expired")
                self.login_btn.setText("Log in")

    def _tab_changed(self, idx: int) -> None:
        w = self.bottom.widget(idx)
        if w is self.users:
            self.users.refresh()
        elif w is self.devedit and not self.devedit.is_dirty():
            self.devedit.refresh()
        elif w is self.chanedit and not self.chanedit.is_dirty():
            self.chanedit.refresh()
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
        keep = (ask_keep_outputs(self, c) if c.session and c.lease_holder and self.bridge.probe(0.5) is not None
                else [])
        if keep is None:
            ev.ignore()
            return
        try:
            if c.session:
                if c.lease_holder:
                    c.release_lease(keep_outputs=keep)
                c.logout()
        except ServiceError:
            pass
        self._timer.stop()                 # no polling into a closed window
        self.panel._timer.stop()
        self.bridge.close()
        super().closeEvent(ev)


def main(argv=None) -> int:
    from core.gui import install_crash_guard, set_windows_app_id, splash
    from Samba_controls import APP_ID
    install_crash_guard("samba_controls")          # log file + errors shown instead of closing the window
    set_windows_app_id(APP_ID)
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Samba_controls")
    if (REPO / "assets" / "samba_controls.ico").exists():
        app.setWindowIcon(QIcon(str(REPO / "assets" / "samba_controls.ico")))
    app.setOrganizationName("ETH-Intermag")
    splash.start("samba_controls", APP_ID)            # already up when started with python -m Samba_controls
    splash.status("Opening the main window…")
    splash.wait()
    w = ControlsWindow()
    splash.finish(w)
    return app.exec()
