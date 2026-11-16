"""Samba_mini main window.

Layout follows Samba's MainWindow (``Samba_main/samba.py``): no menu bar; an
action bar with setup pills and Start/Pause/Abort; a horizontal splitter with
the named-configuration list, the live tabs (2D Map, 1D Plot, Log) and the
sensor/plot panel; bottom tabs (Trajectory, Instruments, Data Browser,
Profile); and an eight-field status bar.

All hardware access goes through the device service (``core.client``); the
scan itself runs in the Qt-free ``core.scan.engine.ScanEngine`` thread.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QKeySequence, QTextCharFormat
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                             QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSplitter,
                             QStatusBar, QStyle, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from core import paths
from core.data_browser import DataBrowserPanel
from core.gui.bridge import EngineBridge, ServiceBridge, call_async, error_text
from core.gui.login import LoginDialog
from core.plot_config import X_NATURAL, X_TIME
from core.plot_widgets import Live1DWidget, Live2DWidget
from core.protocol import ErrorCode, ServiceError
from core.scan import plans
from core.scan.channels import SENSORS, sensors_for
from core.scan.engine import RunRequest, ScanEngine
from core.scan.workflows import WORKFLOWS
from core.theme import app_style
from core.version import __version__, describe
from Samba_mini.panels.config_list import SETUPS, ConfigListPanel
from Samba_mini.panels.instruments import InstrumentsPanel
from Samba_mini.panels.profile import ProfilePanel
from Samba_mini.panels.right_panel import RightPanel
from Samba_mini.panels.trajectory import TrajectoryPanel
from Samba_mini.panels.widgets import pill

log = logging.getLogger("Samba_mini")

MEAN_KEYS = ["dc_signal", "intensity", "hall_field", "lockin_x", "lockin_r"]
SETUP_LABEL = {"longitudinal": "Longitudinal", "polar": "Polar"}
REPO = Path(__file__).resolve().parents[1]


class MainWindow(QMainWindow):
    def __init__(self, bridge: ServiceBridge | None = None, auto_login: bool = True):
        super().__init__()
        self.bridge = bridge or ServiceBridge("Samba_mini")
        self.engine: ScanEngine | None = None
        self.engine_bridge = EngineBridge(self)
        self.setup = "longitudinal"
        self.geometry_mode = "LMOKE"
        self.prefs: dict = {}
        self.configs: dict[str, dict[str, dict]] = {s: {} for s in SETUPS}
        self._run_t0: float | None = None
        self._run_state = "IDLE"
        self._progress = (0, 0)
        self._map_data: dict[str, np.ndarray] = {}
        self._map_axes = None
        self._auto_login = auto_login
        self._login_shown = False
        self._log_entries: list[tuple[str, str]] = []
        self.setWindowTitle(f"Samba_mini v{__version__} — MiniMOKE · ETH Zürich")
        icon = REPO / "assets" / "samba_mini.ico"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.setStyleSheet(app_style())
        self._build()
        self._wire()
        self._restore_geometry()
        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(self._check_service)
        self._poll.start()
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._update_status_bar)
        self._clock.start()
        QTimer.singleShot(200, self._check_service)

    # ================================================================ build
    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._action_bar())

        self.v_split = QSplitter(Qt.Orientation.Vertical)
        self.h_split = QSplitter(Qt.Orientation.Horizontal)
        self.config_list = ConfigListPanel()
        self.h_split.addWidget(self.config_list)

        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        self.live_tabs = QTabWidget()
        self.map2d = Live2DWidget()
        self.plot1d = Live1DWidget()
        self.live_tabs.addTab(self.map2d, "2D Map")
        self.live_tabs.addTab(self.plot1d, "1D Plot")
        log_w = QWidget()
        ll = QVBoxLayout(log_w)
        ll.setContentsMargins(2, 2, 2, 2)
        self.log_filter = QComboBox()
        self.log_filter.addItems(["All", "Warnings and errors", "Errors"])
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        ll.addWidget(self.log_filter)
        ll.addWidget(self.log_view)
        self.live_tabs.addTab(log_w, "Log")
        self.live_tabs.setCurrentWidget(self.plot1d)
        cl.addWidget(self.live_tabs, 1)
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color:#a6adc8;padding:2px 6px;")
        cl.addWidget(self.status_lbl)
        self.h_split.addWidget(center)
        self.right = RightPanel()
        right_scroll = QScrollArea()
        right_scroll.setWidget(self.right)
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.h_split.addWidget(right_scroll)
        self.h_split.setSizes([215, 760, 360])
        self.v_split.addWidget(self.h_split)

        self.bottom_tabs = QTabWidget()
        self.trajectory = TrajectoryPanel()
        self.instruments = InstrumentsPanel(lambda: self.bridge, lambda: self.setup)
        self.browser = DataBrowserPanel(self._data_dir)
        self.profile = ProfilePanel(lambda: self.bridge)
        self.bottom_tabs.addTab(self.trajectory, "Trajectory")
        self.bottom_tabs.addTab(self.instruments, "Instruments")
        self.bottom_tabs.addTab(self.browser, "Data Browser")
        self.bottom_tabs.addTab(self.profile, "Profile")
        self.v_split.addWidget(self.bottom_tabs)
        self.v_split.setSizes([520, 380])
        root.addWidget(self.v_split, 1)
        self.setCentralWidget(central)
        self._status_bar()

    def _action_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("action_bar")
        bar.setFixedHeight(44)
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(6)
        self.setup_group = QButtonGroup(self)
        self.setup_btns = {}
        for s in SETUPS:
            b = pill(SETUP_LABEL[s], f"setup_{s}")
            b.setToolTip("Longitudinal setup: Thorlabs stage (PMOKE or LMOKE)" if s == "longitudinal"
                         else "Polar setup: Trinamic stage (PMOKE)")
            self.setup_group.addButton(b)
            self.setup_btns[s] = b
            h.addWidget(b)
        self.setup_btns["longitudinal"].setChecked(True)
        self.geo_label = QLabel("Geometry:")
        h.addWidget(self.geo_label)
        self.geo_group = QButtonGroup(self)
        self.geo_btns = {}
        for g in ("PMOKE", "LMOKE"):
            b = pill(g)
            self.geo_group.addButton(b)
            self.geo_btns[g] = b
            h.addWidget(b)
        self.geo_btns["LMOKE"].setChecked(True)
        h.addWidget(_vsep())
        st = self.style()
        self.start_btn = QPushButton(" Start")
        self.start_btn.setObjectName("start_btn")
        self.start_btn.setIcon(st.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.pause_btn = QPushButton(" Pause")
        self.pause_btn.setObjectName("pause_btn")
        self.pause_btn.setIcon(st.standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        self.abort_btn = QPushButton(" Abort")
        self.abort_btn.setObjectName("abort_btn")
        self.abort_btn.setIcon(st.standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        for b in (self.start_btn, self.pause_btn, self.abort_btn):
            b.setFixedHeight(30)
            h.addWidget(b)
        self.pause_btn.setEnabled(False)
        self.abort_btn.setEnabled(False)
        h.addWidget(_vsep())
        h.addWidget(QLabel("Dir:"))
        self.dir_edit = QLineEdit(str(paths.default_measurement_dir()))
        self.dir_edit.setMinimumWidth(200)
        h.addWidget(self.dir_edit, 1)
        browse = QPushButton("...")
        browse.setFixedWidth(34)
        browse.clicked.connect(self._browse_dir)
        h.addWidget(browse)
        h.addWidget(_vsep())
        self.control_btn = QPushButton("Take control")
        self.control_btn.setToolTip("Control of the setup (lease): needed for runs and manual controls")
        h.addWidget(self.control_btn)
        self.mode_badge = QLabel("OFFLINE")
        self.mode_badge.setObjectName("offline_badge")
        h.addWidget(self.mode_badge)
        self.user_lbl = QLabel("not logged in")
        self.user_lbl.setStyleSheet("color:#a6adc8;")
        h.addWidget(self.user_lbl)
        self.login_btn = QPushButton("Log in")
        h.addWidget(self.login_btn)
        return bar

    def _status_bar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.sb = {}
        for key in ("State", "Scan", "Start", "Elapsed", "Left", "Points", "Mode", "User"):
            lbl = QLabel(f"{key}: —")
            sb.addPermanentWidget(lbl)
            self.sb[key] = lbl
        self._tint("IDLE")

    def _wire(self) -> None:
        self.start_btn.clicked.connect(self.start_run)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.abort_btn.clicked.connect(self.abort_run)
        self.login_btn.clicked.connect(self._login_logout)
        self.control_btn.clicked.connect(self._toggle_control)
        for s, b in self.setup_btns.items():
            b.clicked.connect(lambda _=False, s=s: self.set_setup(s))
        for g, b in self.geo_btns.items():
            b.clicked.connect(lambda _=False, g=g: setattr(self, "geometry_mode", g))
        self.trajectory.workflow_changed.connect(self._on_workflow_changed)
        self.right.plot_changed.connect(self._apply_plot_config)
        self.right.map_sensor_changed.connect(self._switch_map_sensor)
        self.right.colormap_changed.connect(self.map2d.set_colormap)
        self.config_list.config_selected.connect(self._load_config)
        self.config_list.save_requested.connect(self._save_config)
        self.config_list.copy_requested.connect(self._copy_config)
        self.config_list.delete_requested.connect(self._delete_config)
        self.config_list.rename_requested.connect(self._rename_config)
        self.instruments.message.connect(self._log)
        self.profile.message.connect(self._log)
        self.profile.saved.connect(self._apply_prefs)
        self.log_filter.currentIndexChanged.connect(lambda _i: self._render_log())
        eb = self.engine_bridge
        eb.state.connect(self._on_engine_state)
        eb.point.connect(self._on_point)
        eb.progress.connect(self._on_progress)
        eb.log.connect(self._log)
        eb.finished.connect(self._on_finished)
        self.bridge.connection.connect(self._on_connection)
        self.bridge.event.connect(self._on_event)
        sc = QAction(self)
        sc.setShortcut(QKeySequence("F5"))
        sc.triggered.connect(self.start_run)
        self.addAction(sc)
        self._on_workflow_changed(self.trajectory.current)

    # ================================================================ helpers
    def _data_dir(self) -> str:
        return self.dir_edit.text().strip() or str(paths.default_measurement_dir())

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Data folder", self._data_dir())
        if d:
            self.dir_edit.setText(d)

    _LOG_COLORS = {"error": "#f38ba8", "warning": "#fab387", "info": "#a6e3a1", "debug": "#6c7086"}

    def _log_visible(self, level: str) -> bool:
        f = self.log_filter.currentIndex()
        return f == 0 or (f == 1 and level in ("warning", "error")) or (f == 2 and level == "error")

    def _render_log(self) -> None:
        self.log_view.clear()
        for level, line in self._log_entries:
            if self._log_visible(level):
                self.log_view.setTextColor(QColor(self._LOG_COLORS.get(level, "#cdd6f4")))
                self.log_view.append(line)

    def _log(self, level: str, msg: str) -> None:
        colors = self._LOG_COLORS
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self._log_entries.append((level, line))
        del self._log_entries[:-5000]
        if self._log_visible(level):
            self.log_view.setTextColor(QColor(colors.get(level, "#cdd6f4")))
            self.log_view.append(line)
        if level in ("warning", "error"):
            self.status_lbl.setText(msg)
            self.status_lbl.setStyleSheet(f"color:{colors[level]};padding:2px 6px;")
        getattr(log, level if level in ("info", "warning", "error") else "info")(msg)

    def _tint(self, state: str) -> None:
        bg, border = {"RUNNING": ("#16241b", "#a6e3a1"), "PREPARING": ("#16241b", "#a6e3a1"),
                      "PAUSED": ("#2b2015", "#fab387"), "STOPPING": ("#2b2015", "#fab387")}.get(
            state, ("#181825", "#313244"))
        self.statusBar().setStyleSheet(f"QStatusBar{{background:{bg};border-top:1px solid {border};}}"
                                       "QStatusBar QLabel{color:#cdd6f4;padding:0 6px;}")

    @property
    def running(self) -> bool:
        # The engine sets its result as the very last step of a run.
        return self.engine is not None and self.engine.result is None

    def _client(self):
        return self.bridge.client

    # ================================================================ service / login
    def _check_service(self) -> None:
        c = self._client()
        if c.connected and c.instance_id:
            self._set_mode_badge((self.bridge.hello_info or {}).get("mode"))
            return
        info = self.bridge.probe(1.0)
        if info is None:
            self._set_mode_badge(None)
            return
        self._set_mode_badge(info["mode"])
        for p in info.get("config_problems") or []:
            self._log("warning", f"service configuration: {p}")
        if self._auto_login and not c.session and not self._login_shown:
            self._login_shown = True
            QTimer.singleShot(0, self.show_login)

    def _set_mode_badge(self, mode: str | None) -> None:
        if mode is None:
            self.mode_badge.setText("SERVICE OFFLINE")
            self.mode_badge.setObjectName("offline_badge")
            self.mode_badge.setToolTip("Start Samba_controls to run the device service")
        elif mode == "simulation":
            self.mode_badge.setText("SIMULATION")
            self.mode_badge.setObjectName("sim_badge")
            self.mode_badge.setToolTip("Simulated instruments: data are labelled SIMULATED")
        else:
            self.mode_badge.setText("REAL HARDWARE")
            self.mode_badge.setObjectName("real_badge")
            self.mode_badge.setToolTip("Measurements drive the real instruments")
        self.mode_badge.style().unpolish(self.mode_badge)
        self.mode_badge.style().polish(self.mode_badge)

    def show_login(self) -> bool:
        if self.bridge.probe(1.5) is None:
            ans = QMessageBox.question(self, "Device service not running",
                                       "The device service is not running.\nStart Samba_controls now?")
            if ans == QMessageBox.StandardButton.Yes:
                launch_controls()
            return False
        dlg = LoginDialog(self.bridge, "Samba_mini", self)
        if dlg.exec() != LoginDialog.DialogCode.Accepted:
            return False
        self._after_login()
        return True

    def _after_login(self) -> None:
        c = self._client()
        user = c.user or {}
        self.user_lbl.setText(f"{user.get('username')} ({user.get('role')})")
        self.login_btn.setText("Log out")
        try:
            self.prefs = c.call("prefs.get")
        except ServiceError:
            self.prefs = {}
        self.profile.load(user, self.prefs)
        self._apply_prefs(self.prefs)
        self._reload_configs()
        self._log("info", f"Logged in as {user.get('username')}")
        self.instruments.refresh_positions()
        self._update_control_btn()

    def _apply_prefs(self, prefs: dict) -> None:
        self.prefs = prefs or {}
        if self.prefs.get("data_dir"):
            self.dir_edit.setText(self.prefs["data_dir"])
        meta = {}
        if self.prefs.get("default_operator"):
            meta["operator"] = self.prefs["default_operator"]
        if self.prefs.get("default_sample"):
            meta["sample"] = self.prefs["default_sample"]
        meta["export_csv"] = bool(self.prefs.get("export_csv", False))
        self.trajectory.set_metadata(meta)
        if self.prefs.get("setup") in SETUPS:
            self.set_setup(self.prefs["setup"])

    def _login_logout(self) -> None:
        c = self._client()
        if not c.session:
            self.show_login()
            return
        if self.running:
            QMessageBox.warning(self, "Measurement running", "Stop the measurement before logging out.")
            return
        try:
            if c.lease_holder:
                c.release_lease()
            c.logout()
        except ServiceError as exc:
            QMessageBox.warning(self, "Log out", error_text(exc))
            return
        self.user_lbl.setText("not logged in")
        self.login_btn.setText("Log in")
        self.configs = {s: {} for s in SETUPS}
        for s in SETUPS:
            self.config_list.set_items(s, [])
        self.profile.load(None, {})
        self._update_control_btn()
        self._log("info", "Logged out")

    def _on_connection(self, what: str, info: dict) -> None:
        if what == "disconnected":
            self._set_mode_badge(None)
            self._log("warning", f"Device service not reachable ({info.get('reason', '')})")
        elif what == "restarted":
            self._log("warning", "The device service restarted; log in again.")
        elif what == "session_lost":
            self.user_lbl.setText("session expired")
            self.login_btn.setText("Log in")
        elif what == "lease_lost":
            self._log("error", "Control of the setup was lost (lease ended).")
            if self.engine is not None and self.running:
                self.engine.lease_lost()
        self._update_control_btn()

    def _on_event(self, topic: str, body: dict) -> None:
        if topic.startswith("readback."):
            dev, attr = body.get("device"), body.get("attr")
            self.right.set_readout(dev, attr, body.get("value"), body.get("unit", ""), body.get("quality", ""))
            if dev == f"stage_{self.setup}" and attr in ("x_mm", "y_mm", "z_mm"):
                self.instruments.set_position(attr[0], body.get("value"), body.get("quality", ""))
            if dev == "lockin" and attr == "xy" and isinstance(body.get("value"), dict):
                v = body["value"]
                self.right.set_readout("lockin", "x_V", v.get("x_V"), "V", body.get("quality", ""))
                self.right.set_readout("lockin", "r_V", v.get("r_V"), "V", body.get("quality", ""))
        elif topic == "lease":
            self._update_control_btn(body)
        elif topic.startswith("state."):
            if body.get("state") == "FAULT":
                self._log("error", f"{body.get('device')}: {body.get('status')}")

    # ================================================================ control
    def _toggle_control(self) -> None:
        c = self._client()
        if not c.session:
            self.show_login()
            return
        try:
            if c.lease_holder:
                if self.running:
                    QMessageBox.warning(self, "Measurement running", "Stop the measurement first.")
                    return
                c.release_lease()
                self._log("info", "Released control of the setup.")
            else:
                c.acquire_lease()
                self._log("info", "You control the setup.")
        except ServiceError as exc:
            QMessageBox.warning(self, "Control of the setup", error_text(exc))
        self._update_control_btn()

    def _update_control_btn(self, lease: dict | None = None) -> None:
        c = self._client()
        if c.lease_holder:
            self.control_btn.setText("Release control")
            self.control_btn.setStyleSheet("background:#a6e3a1;color:#1e1e2e;font-weight:bold;")
        else:
            held = lease and lease.get("state") == "HELD" and lease.get("holder")
            self.control_btn.setText("Take control" if not held else
                                     f"Controlled by {lease['holder'].get('user')}")
            self.control_btn.setStyleSheet("" if not held else "color:#fab387;")

    # ================================================================ setup / workflow
    def set_setup(self, setup: str) -> None:
        if self.running:
            self.setup_btns[self.setup].setChecked(True)
            return
        self.setup = setup
        self.setup_btns[setup].setChecked(True)
        polar = setup == "polar"
        for w in (self.geo_label, *self.geo_btns.values()):
            w.setVisible(not polar)
        self.config_list.set_setup(setup)
        self.instruments.update_setup()
        self.instruments.refresh_positions()

    def _on_workflow_changed(self, key: str) -> None:
        self.right.configure_for(key)
        self.live_tabs.setCurrentWidget(self.map2d if key == "xy_sweep" else self.plot1d)

    # ================================================================ named configurations
    def _config_value(self) -> dict:
        return {"workflow": self.trajectory.current, "params": self.trajectory.params(),
                "geometry": self.geometry_mode, "metadata": self.trajectory.metadata()}

    def _reload_configs(self) -> None:
        self.configs = {s: {} for s in SETUPS}
        try:
            for p in self._client().call("presets.list", {}):
                wf = p["workflow"]
                if wf.startswith("samba_mini/"):
                    s = wf.split("/", 1)[1]
                    if s in self.configs:
                        self.configs[s][p["name"]] = p["value"]
        except ServiceError as exc:
            self._log("warning", f"configurations not loaded: {error_text(exc)}")
        for s in SETUPS:
            self.config_list.set_items(s, sorted(self.configs[s]))
        self.config_list.hint.setText("Configurations are saved to your account.")

    def _load_config(self, setup: str, name: str) -> None:
        if self.running:
            return
        v = self.configs.get(setup, {}).get(name)
        if not v:
            return
        self.trajectory.set_workflow(v.get("workflow", self.trajectory.current))
        self.trajectory.set_params(v.get("workflow"), v.get("params", {}))
        if v.get("geometry") in self.geo_btns:
            self.geometry_mode = v["geometry"]
            self.geo_btns[v["geometry"]].setChecked(True)
        meta = dict(v.get("metadata", {}))
        meta.pop("export_csv", None)
        self.trajectory.set_metadata(meta)
        self._log("info", f"Loaded configuration '{name}'")

    def _save_config(self, setup: str, name: str) -> None:
        c = self._client()
        if not c.session:
            self.show_login()
            return
        value = self._config_value()
        try:
            c.call("presets.save", {"workflow": f"samba_mini/{setup}", "name": name, "value": value})
        except ServiceError as exc:
            self._log("error", f"configuration not saved: {error_text(exc)}")
            return
        self.configs[setup][name] = value
        self.config_list.set_items(setup, sorted(self.configs[setup]), name)
        self._log("info", f"Saved configuration '{name}'")

    def _copy_config(self, setup: str, new: str) -> None:
        self._save_config(setup, new)

    def _delete_config(self, setup: str, name: str) -> None:
        try:
            self._client().call("presets.delete", {"workflow": f"samba_mini/{setup}", "name": name})
        except ServiceError as exc:
            self._log("error", error_text(exc))
            return
        self.configs[setup].pop(name, None)
        self.config_list.set_items(setup, sorted(self.configs[setup]))

    def _rename_config(self, setup: str, old: str, new: str) -> None:
        v = self.configs[setup].get(old)
        if v is None:
            return
        try:
            self._client().call("presets.save", {"workflow": f"samba_mini/{setup}", "name": new, "value": v})
            self._client().call("presets.delete", {"workflow": f"samba_mini/{setup}", "name": old})
        except ServiceError as exc:
            self._log("error", error_text(exc))
            return
        self.configs[setup][new] = self.configs[setup].pop(old)
        self.config_list.set_items(setup, sorted(self.configs[setup]), new)

    # ================================================================ run control
    def start_run(self) -> None:
        if self.running:
            return
        c = self._client()
        if self.bridge.probe(1.5) is None:
            QMessageBox.warning(self, "Device service", "The device service is not running. "
                                "Start Samba_controls first.")
            return
        if not c.session and not self.show_login():
            return
        wf = self.trajectory.workflow()
        try:
            params = wf.coerce(self.trajectory.params())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid parameters", str(exc))
            return
        n, secs, msg = self.trajectory.estimate()
        if msg:
            QMessageBox.warning(self, "Cannot start", msg)
            return
        mode = (self.bridge.hello_info or {}).get("mode")
        if mode == "real":
            moves = wf.uses_stage == "required" or params.get("position_stage")
            text = (f"Start {wf.title} on the REAL setup?\n\nThe coil command will be set"
                    + (", the lock-in oscillator switched on" if wf.uses_lockin else "")
                    + (" and the stage moved" if moves else "") + ".")
            if QMessageBox.question(self, "Real hardware", text) != QMessageBox.StandardButton.Yes:
                return
        try:
            if not c.lease_holder:
                c.acquire_lease()
        except ServiceError as exc:
            QMessageBox.warning(self, "Control of the setup", error_text(exc))
            return
        self._update_control_btn()
        meta = self.trajectory.metadata()
        data_dir = self._data_dir()
        notebook = self.prefs.get("lab_notebook") or os.path.join(data_dir, "lab_notebook_SambaMini.csv")
        req = RunRequest(wf.key, params, setup=self.setup, geometry=self.geometry_mode, sample=meta["sample"],
                         operator=meta["operator"], notes=meta["notes"],
                         config_name=self.config_list.current_name() or "", save_dir=data_dir,
                         export_csv=meta["export_csv"], lab_notebook=notebook)
        self._prepare_plots(wf.key, params, n)
        self.engine = ScanEngine(c, req, self.engine_bridge.callbacks())
        self._run_t0 = time.time()
        self._progress = (0, n)
        self._set_running_ui(True)
        self.engine.start()
        self._log("info", f"Started {wf.title} ({n} points)")

    def toggle_pause(self) -> None:
        if not self.running:
            return
        if self._run_state == "PAUSED":
            self.engine.resume()
        else:
            self.engine.pause()

    def abort_run(self) -> None:
        if self.running:
            self.engine.abort()
            self.abort_btn.setEnabled(False)

    def _set_running_ui(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.abort_btn.setEnabled(running)
        self.trajectory.set_editable(not running)
        self.config_list.setEnabled(not running)
        for b in list(self.setup_btns.values()) + list(self.geo_btns.values()):
            b.setEnabled(not running)
        self.login_btn.setEnabled(not running)
        self.instruments.setEnabled(not running)
        if not running:
            self.pause_btn.setText(" Pause")

    def _on_engine_state(self, state: str, message: str) -> None:
        self._run_state = state
        self._tint(state)
        if state == "PAUSED":
            self.pause_btn.setText(" Resume")
            if message:
                self.status_lbl.setText(f"PAUSED — {message}")
                self.status_lbl.setStyleSheet("color:#fab387;padding:2px 6px;")
                if "error" in message.lower() or "cannot write" in message.lower():
                    box = QMessageBox(QMessageBox.Icon.Warning, "Measurement auto-paused", message,
                                      QMessageBox.StandardButton.Ok, self)
                    box.setModal(False)
                    box.open()
        elif state == "RUNNING":
            self.pause_btn.setText(" Pause")
            self.status_lbl.setText("Running")
            self.status_lbl.setStyleSheet("color:#a6e3a1;padding:2px 6px;")
        self._update_status_bar()

    def _on_progress(self, done: int, total: int) -> None:
        self._progress = (done, total)

    def _on_finished(self, result: dict) -> None:
        self._set_running_ui(False)
        self._run_state = "IDLE"
        self._tint("IDLE")
        status = result.get("status")
        text = f"Run {status}: {result.get('points')}/{result.get('points_planned')} points"
        if result.get("path"):
            text += f"\n{result['path']}"
        if result.get("message"):
            text += f"\n{result['message']}"
        level = "info" if status == "completed" else "warning"
        self._log(level, text.replace("\n", " — "))
        problems = result.get("cleanup_problems") or []
        if problems:
            QMessageBox.warning(self, "Check the setup",
                                "The outputs could not be confirmed safe:\n\n" + "\n".join(problems)
                                + "\n\nCheck the coil current, lock-in output and stages before continuing.")
        elif status not in ("completed", "aborted"):
            QMessageBox.warning(self, f"Run {status}", text)
        try:
            self.browser.refresh()
        except Exception:  # noqa: BLE001
            pass
        self._update_status_bar()

    # ================================================================ plots
    def _prepare_plots(self, key: str, params: dict, n: int) -> None:
        wf = WORKFLOWS[key]
        chans = sensors_for(wf.uses_lockin)
        all_sensors = [{"label": ch.label, "unit": ch.unit} for ch in chans]
        extra = 0
        if key in ("b_sweep_dc", "b_sweep_lockin"):
            all_sensors += [{"label": f"{ch.label} (mean)", "unit": ch.unit} for ch in chans
                            if ch.name in MEAN_KEYS]
        xl, xu = {"b_sweep_dc": ("Coil command", "V"), "b_sweep_lockin": ("Coil command", "V"),
                  "x_sweep": ("X position", "um"), "y_sweep": ("Y position", "um"),
                  "xy_sweep": ("X position", "um"), "time": ("Time", "s")}[key]
        self.plot1d.alloc(max(n + extra, 1), xl, xu, all_sensors)
        self._apply_plot_config()
        self._map_data = {}
        self._map_axes = None
        if key == "xy_sweep":
            xs = plans.axis_values(params["x_min"], params["x_max"], params["x_step"])
            ys = plans.axis_values(params["y_min"], params["y_max"], params["y_step"])
            self._map_axes = (xs, ys)
            for ch in chans:
                self._map_data[ch.name] = np.full((len(ys), len(xs)), np.nan)
            name = self.right.map_combo.currentData() or "dc_signal"
            ch = next((c for c in chans if c.name == name), chans[0])
            self.map2d.setup(xs, ys, "X (um)", "Y (um)", f"{ch.label} ({ch.unit})", self.right.cmap.currentText())
        else:
            self.map2d.clear()

    def _apply_plot_config(self) -> None:
        meta = [m for m in self.right.sensors_meta()]
        if self.trajectory.current in ("b_sweep_dc", "b_sweep_lockin"):
            for m in list(meta):
                if m["name"] in MEAN_KEYS and m["axis"] != "—":
                    meta.append({"label": f"{m['label']} (mean)", "unit": m["unit"], "axis": m["axis"]})
        try:
            self.plot1d.apply_config(meta, self.right.x_key())
        except Exception as exc:  # noqa: BLE001 - before alloc the widget has no buffers
            log.debug("plot config: %s", exc)

    def _switch_map_sensor(self, name: str) -> None:
        if name in self._map_data:
            ch = next(c for c in SENSORS if c.name == name)
            self.map2d.switch_sensor(self._map_data[name], f"{ch.label} ({ch.unit})")

    def _on_point(self, ev: dict) -> None:
        kind = ev.get("kind")
        vals = ev.get("values", {})
        if kind == "average":
            xs = ev["x"]
            for k in MEAN_KEYS:
                ch = next((c for c in SENSORS if c.name == k), None)
                ys = ev["values"].get(k)
                if ch is None or ys is None:
                    continue
                for i, (x, y) in enumerate(zip(xs, ys)):
                    self.plot1d.update_point(i, x, {f"{ch.label} (mean)": y})
            return
        labelled = {ch.label: vals.get(ch.name, math.nan) for ch in SENSORS}
        labelled[X_TIME] = vals.get("time", math.nan)
        wf = ev.get("workflow")
        if wf in ("b_sweep_dc", "b_sweep_lockin"):
            ix = ev["flat_index"]
        else:
            ix = ev["index"]
        try:
            self.plot1d.update_point(ix, ev.get("x", math.nan), labelled)
        except IndexError:
            pass
        if wf == "xy_sweep" and self._map_axes is not None and ev.get("rep", 0) == 0:
            for name, arr in self._map_data.items():
                arr[ev["iy"], ev["ix"]] = vals.get(name, math.nan)
            shown = self.right.map_combo.currentData() or "dc_signal"
            self.map2d.update_point(ev["ix"], ev["iy"], vals.get(shown, math.nan))
        rec = ev.get("record", {})
        self.status_lbl.setText(
            f"Point {self._progress[0]}/{self._progress[1]} — coil {rec.get('coil_V', math.nan):+.4g} V, "
            f"Hall {rec.get('hall_field_mT', math.nan):.4g} mT, DC {rec.get('balanced_V', math.nan):.5g} V")

    # ================================================================ status bar
    def _update_status_bar(self) -> None:
        c = self._client()
        st = self._run_state if self.engine else "IDLE"
        self.sb["State"].setText(f"State: {st}")
        self.sb["Scan"].setText(f"Scan: {WORKFLOWS[self.trajectory.current].short}")
        if self._run_t0 and self.engine:
            self.sb["Start"].setText("Start: " + time.strftime("%H:%M:%S", time.localtime(self._run_t0)))
            el = time.time() - self._run_t0 if self.running else (self.engine.result or {}).get("elapsed", 0)
            if self.running:
                self.sb["Elapsed"].setText(f"Elapsed: {_hms(el)}")
                done, total = self._progress
                if done > 0 and total > done:
                    self.sb["Left"].setText(f"Left: ~{_hms(el / done * (total - done))}")
            else:
                self.sb["Left"].setText("Left: —")
        done, total = self._progress
        self.sb["Points"].setText(f"Points: {done}/{total}")
        mode = (self.bridge.hello_info or {}).get("mode", "offline") if c.connected else "offline"
        self.sb["Mode"].setText(f"Mode: {mode}")
        self.sb["User"].setText(f"User: {(c.user or {}).get('username', '—')}")

    # ================================================================ window
    def _restore_geometry(self) -> None:
        s = QSettings("ETH-Intermag", "SambaMini")
        g = s.value("geometry")
        if g is not None:
            self.restoreGeometry(g)
        else:
            self.resize(1360, 920)
        self.setMinimumSize(1180, 640)

    def closeEvent(self, ev) -> None:
        if self.running:
            ans = QMessageBox.question(self, "Measurement running",
                                       "A measurement is running. Abort it and close Samba_mini?\n"
                                       "(The data measured so far are kept.)")
            if ans != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self.engine.abort()
            loop = QEventLoop()
            t0 = time.time()
            while self.running and time.time() - t0 < 120:
                loop.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
                time.sleep(0.05)
        QSettings("ETH-Intermag", "SambaMini").setValue("geometry", self.saveGeometry())
        c = self._client()
        try:
            if c.session:
                if c.lease_holder:
                    c.release_lease()
                c.call("prefs.set", {"values": {"setup": self.setup}})
                c.logout()
        except ServiceError:
            pass
        self.bridge.close()
        super().closeEvent(ev)


def _vsep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    f.setStyleSheet("color:#313244;")
    return f


def _hms(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def launch_controls() -> None:
    """Start Samba_controls (which owns the device service)."""
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable, "-m", "Samba_controls"], cwd=str(REPO), creationflags=flags)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from core.gui import set_windows_app_id
    set_windows_app_id("ETH.Intermag.SambaMini")
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Samba_mini")
    if (REPO / "assets" / "samba_mini.ico").exists():
        app.setWindowIcon(QIcon(str(REPO / "assets" / "samba_mini.ico")))
    app.setOrganizationName("ETH-Intermag")
    w = MainWindow()
    w.show()
    return app.exec()
