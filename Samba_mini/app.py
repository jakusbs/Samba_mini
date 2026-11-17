"""Samba_mini main window.

Layout follows Samba's MainWindow (``Samba_main/samba.py``): no menu bar; an
action bar with setup pills, Start/Pause/Abort and the data folder; a
horizontal splitter with the named-configuration list, the live tabs (2D Map,
1D Plot, Calibration, Log) and the sensor/plot panel; bottom tabs (Trajectory,
Data Browser, Profile); and Samba's run-progress status bar.  Samba_mini adds
the control/mode/user section at the right of the action bar (shared-PC
accounts and the setup lease).

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
from core.gui.bridge import EngineBridge, ServiceBridge, error_text
from core.gui.login import LoginDialog
from core.plot_config import X_NATURAL, X_TIME
from core.plot_widgets import Live1DWidget, Live2DWidget
from core.kerr import KerrDisplayState
from core.polarity import flip_phase, phase_label
from core.protocol import ErrorCode, ServiceError
from core.scan import plans
from core.scan.channels import SENSORS, sensors_for
from core.scan.engine import RunRequest, ScanEngine
from core.scan.workflows import WORKFLOWS
from core.theme import app_style
from core.version import __version__, describe
from Samba_mini.panels.bd_calibration import BDCalibrationPanel, describe_record
from Samba_mini.panels.calibration import CalibrationPanel
from Samba_mini.panels.config_list import SETUPS, ConfigListPanel
from Samba_mini.panels.profile import ProfilePanel
from Samba_mini.panels.right_panel import RightPanel
from Samba_mini.panels.scanlist import ScanlistPanel
from Samba_mini.panels.trajectory import TrajectoryPanel
from Samba_mini.state import (EXPERIMENT_KEYS, LEGACY_IDENTITY_KEYS, OPTICS_KEYS, PRESET_SCHEMA, WORKING_VERSION,
                              PrefsStore, canonical, split_metadata, working_key)


RUN_META_KEYS = ("device_id", "incidence", "mirror_shift", "polarization", "lam2", "lam4", "noDC",
                 "r_4wire_ohm", "r_2wire_ohm", "fm_thickness_nm", "t_stack_nm")

log = logging.getLogger("Samba_mini")

MEAN_KEYS = ["dc_signal", "intensity", "hall_field", "lockin_x", "lockin_r"]
SETUP_LABEL = {"longitudinal": "Longitudinal", "polar": "Polar"}
SETUP_COLOR = {"longitudinal": "#00a86b", "polar": "#ab4b52"}       # Samba's Green / IR colours
REPO = Path(__file__).resolve().parents[1]


class MainWindow(QMainWindow):
    def __init__(self, bridge: ServiceBridge | None = None, auto_login: bool = True):
        super().__init__()
        self.bridge = bridge or ServiceBridge("Samba_mini")
        self.engine: ScanEngine | None = None
        self.engine_bridge = EngineBridge(self)
        self.setup = "longitudinal"
        # One store for the account's preferences and per-setup working state (autosaved);
        # see Samba_mini/state.py for what is remembered where.
        self.store = PrefsStore(lambda: self.bridge.client, parent=self)
        self.configs: dict[str, dict[str, dict]] = {s: {} for s in SETUPS}
        self._preset_name: dict[str, str | None] = {s: None for s in SETUPS}     # selected configuration
        self._preset_clean: dict[str, str | None] = {s: None for s in SETUPS}    # its saved form (canonical)
        self._restoring = False                                # filling widgets from stored state
        self._session_user: dict | None = None
        self._stash: dict | None = None                        # unsaved state of an expired session
        self._session_lost_pending = False
        self._run_cal: dict | None = None                      # calibration frozen for the current run
        self._ws_timer = QTimer(self)                          # working-state autosave (debounced)
        self._ws_timer.setSingleShot(True)
        self._ws_timer.setInterval(300)
        self._ws_timer.timeout.connect(self._save_working)
        self._run_t0: float | None = None
        self._run_state = "IDLE"
        self._progress = (0, 0)
        self._list = {"total": 1, "done": 0, "t0": None}     # scans in this run (scanlist: N)
        self._sl: dict | None = None                           # running scanlist
        self._sl_hall: list[float] = []                        # Hall readings of the current scan (mT)
        self._last_sample = ""                                 # for Samba's "new sample?" prompt
        self._calib_run = False                                # a Calibration-tab time scan is running
        self._scan_t0: float | None = None
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
        root.setContentsMargins(6, 6, 6, 6)          # Samba: 6 px frame, 4 px spacing
        root.setSpacing(4)
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
        self.calibration = CalibrationPanel(lambda: self.bridge, lambda: self.setup)
        self.live_tabs.addTab(self.calibration, "Calibration")
        log_w = QWidget()
        ll = QVBoxLayout(log_w)
        ll.setContentsMargins(2, 4, 2, 0)
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        hdr.addWidget(QLabel("Filter:"))
        self.log_filter = QComboBox()
        self.log_filter.addItems(["All", "Errors only", "Warnings + Errors"])
        self.log_filter.setFixedWidth(160)
        hdr.addWidget(self.log_filter)
        hdr.addStretch()
        clear = QLabel("<a style='color:#6c7086;font-size:10px;' href='#'>Clear</a>")
        clear.linkActivated.connect(lambda _l: (self._log_entries.clear(), self.log_view.clear()))
        hdr.addWidget(clear)
        ll.addLayout(hdr)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        ll.addWidget(self.log_view)
        self.live_tabs.addTab(log_w, "Log")
        self.live_tabs.setCurrentWidget(self.plot1d)
        cl.addWidget(self.live_tabs, 1)
        self.status_lbl = QLabel("Ready")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:#6c7086;font-size:11px;padding:2px 6px;")
        cl.addWidget(self.status_lbl)
        self.h_split.addWidget(center)
        self.right = RightPanel()
        right_scroll = QScrollArea()
        right_scroll.setWidget(self.right)
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setMinimumWidth(self.right.minimumSizeHint().width() + 18)   # rows never clipped
        self.h_split.addWidget(right_scroll)
        self.h_split.setSizes([215, 760, 360])
        self.v_split.addWidget(self.h_split)

        self.bottom_tabs = QTabWidget()
        self.trajectory = TrajectoryPanel(lambda: self.bridge)
        self.browser = DataBrowserPanel(self._data_dir)
        self.profile = ProfilePanel(lambda: self.bridge, self.store)
        self.scanlist = ScanlistPanel()
        self.bd_panel = BDCalibrationPanel(self._data_dir, self._cal_context)
        self.bottom_tabs.addTab(self.trajectory, "Trajectory")
        self.bottom_tabs.addTab(self.scanlist, "Scanlist")
        self.bottom_tabs.addTab(self.bd_panel, "BD Calibration")
        self.bottom_tabs.addTab(self.browser, "Data Browser")
        self.bottom_tabs.addTab(self.profile, "Profile")
        self.v_split.addWidget(self.bottom_tabs)
        self.v_split.setSizes([500, 400])
        root.addWidget(self.v_split, 1)
        self.setCentralWidget(central)
        self._status_bar()

    def _action_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("action_bar")
        bar.setStyleSheet("#action_bar{background:#12121f;border:1px solid #313244;border-radius:6px;}")
        bar.setFixedHeight(44)
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(6)
        # Samba's setup selector: exclusive segmented pills, coloured when selected
        self.setup_group = QButtonGroup(self)
        self.setup_btns = {}
        seg = QHBoxLayout()
        seg.setSpacing(0)
        for idx, s in enumerate(SETUPS):
            fg = SETUP_COLOR[s]
            b = QPushButton(SETUP_LABEL[s])
            b.setCheckable(True)
            b.setFixedHeight(30)
            b.setMinimumWidth(52)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            radius = ("border-top-left-radius:5px;border-bottom-left-radius:5px;"
                      "border-top-right-radius:0;border-bottom-right-radius:0;" if idx == 0 else
                      "border-top-left-radius:0;border-bottom-left-radius:0;"
                      "border-top-right-radius:5px;border-bottom-right-radius:5px;")
            b.setStyleSheet(
                f"QPushButton{{background:#252538;border:1px solid #45475a;color:#6c7086;font-size:11px;"
                f"font-weight:bold;padding:0 10px;{radius}}}"
                f"QPushButton:hover{{background:#313244;color:#cdd6f4;}}"
                f"QPushButton:checked{{background:{fg};color:#1e1e2e;border-color:{fg};}}")
            b.setToolTip("Longitudinal setup: Thorlabs stage (PMOKE or LMOKE)" if s == "longitudinal"
                         else "Polar setup: Trinamic stage (PMOKE)")
            self.setup_group.addButton(b)
            self.setup_btns[s] = b
            seg.addWidget(b)
        h.addLayout(seg)
        self.setup_btns["longitudinal"].setChecked(True)
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
        dir_lbl = QLabel("Dir:")
        dir_lbl.setStyleSheet("color:#89b4fa;font-size:11px;font-weight:bold;")
        h.addWidget(dir_lbl)
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
        """Samba's run-progress bar: Scan / Start / Elapsed / Run left / Scan left / Dead / Done."""
        sb = QStatusBar()
        self.setStatusBar(sb)
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(8, 0, 8, 0)
        row.setSpacing(0)
        self.sb = {}
        keys = ("Scan", "Start", "Elapsed", "Run left", "Scan left", "Dead", "Done")
        for i, key in enumerate(keys):
            cap = QLabel(f"{key}: ")
            cap.setStyleSheet("color:#a6adc8;font-size:12px;")
            val = QLabel("—")
            val.setStyleSheet("color:#cdd6f4;font-size:12px;")
            row.addWidget(cap)
            row.addWidget(val)
            self.sb[key] = val
            if i < len(keys) - 1:
                sep = QLabel("  │  ")
                sep.setStyleSheet("color:#45475a;font-size:12px;")
                row.addWidget(sep)
        row.addStretch(1)
        sb.addWidget(container, 1)
        for key in ("Saved", "Mode", "User"):
            lbl = QLabel(f"{key}: —")
            lbl.setStyleSheet("color:#a6adc8;font-size:12px;padding:0 6px;")
            sb.addPermanentWidget(lbl)
            self.sb[key] = lbl
        self._show_save_state(self.store.state, self.store.error)
        self._tint("IDLE")

    def _wire(self) -> None:
        self.start_btn.clicked.connect(self.start_run)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.abort_btn.clicked.connect(self.abort_run)
        self.login_btn.clicked.connect(self._login_logout)
        self.control_btn.clicked.connect(self._toggle_control)
        for s, b in self.setup_btns.items():
            b.clicked.connect(lambda _=False, s=s: self.set_setup(s))
        self.trajectory.workflow_changed.connect(self._on_workflow_changed)
        self.trajectory.changed.connect(self._show_estimate)
        # Kerr rotation (θ) display: one shared on/off state for the 1D plot, the 2D map and the
        # Data Browser (Samba); the λ/2 calibration of the BD tab converts volts to µrad.
        self.kerr_state = KerrDisplayState()
        for w in (self.plot1d, self.map2d, self.browser.plot):
            w.set_kerr_state(self.kerr_state)
        self.kerr_state.subscribe(self._on_kerr_toggled)
        self.bd_panel.active_changed.connect(self._on_active_calibration)
        self.bd_panel.message.connect(self._log)
        self.trajectory.meta.sample_edited.connect(self._on_sample_edited)
        self.trajectory.changed.connect(self._update_scanlist_info)
        self.trajectory.changed.connect(self._working_changed)
        self.calibration.timescan.changed.connect(self._working_changed)
        self.calibration.timescan.copy_requested.connect(self._copy_hardware_to_calibration_scan)
        self.scanlist.changed.connect(self._update_scanlist_info)
        self.config_list.config_selected.connect(lambda *_a: self._update_scanlist_info())
        self.right.plot_changed.connect(self._apply_plot_config)
        self.right.map_sensor_changed.connect(self._switch_map_sensor)
        self.right.colormap_changed.connect(self.map2d.set_colormap)
        self.config_list.config_selected.connect(self._load_config)
        self.config_list.new_requested.connect(lambda s, n: self._save_config(s, n, "create"))
        self.config_list.save_requested.connect(lambda s, n: self._save_config(s, n, "update"))
        self.config_list.copy_requested.connect(self._copy_config)
        self.config_list.delete_requested.connect(self._delete_config)
        self.config_list.rename_requested.connect(self._rename_config)
        self.calibration.message.connect(self._log)
        self.trajectory.hardware.message.connect(self._log)
        self.profile.message.connect(self._log)
        self.dir_edit.textChanged.connect(self._on_dir_edited)
        self.store.changed.connect(self._on_store_changed)
        self.store.status_changed.connect(self._show_save_state)
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
        self.trajectory.set_setup(self.setup)
        self._apply_session(None, self.setup)          # the logged-out state: explicit defaults
        self._show_estimate()
        self._update_scanlist_info()

    # ================================================================ helpers
    @property
    def prefs(self) -> dict:
        """The account's preferences as currently known (read-only view of the store)."""
        return self.store.values()

    def _data_dir(self) -> str:
        return self.dir_edit.text().strip() or str(paths.default_measurement_dir())

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Data folder", self._data_dir())
        if d:
            self.dir_edit.setText(d)

    def _on_dir_edited(self, text: str) -> None:
        """The action bar's Dir: and the Profile tab's data folder are one setting."""
        if not self._restoring:
            self.store.set("data_dir", text.strip())

    def _on_store_changed(self, key: str, value) -> None:
        if key == "data_dir" and self.dir_edit.text().strip() != str(value or "").strip():
            self.dir_edit.blockSignals(True)
            self.dir_edit.setText(str(value or "") or str(paths.default_measurement_dir()))
            self.dir_edit.blockSignals(False)

    def _show_save_state(self, state: str, text: str) -> None:
        shown = {"offline": ("—", "#6c7086", "Not logged in: nothing is saved"),
                 "saved": ("✓ saved", "#a6e3a1", "Preferences and working state are saved to your account"),
                 "pending": ("● unsaved", "#fab387", "Changes are about to be saved"),
                 "saving": ("● saving…", "#fab387", "Saving to your account"),
                 "error": ("⚠ NOT SAVED", "#f38ba8", text + "\nYour changes are kept; see the Profile tab (Retry)")}
        label, color, tip = shown.get(state, (state, "#a6adc8", ""))
        lbl = self.sb["Saved"]
        lbl.setText(f"Prefs: {label}")
        lbl.setToolTip(tip)
        lbl.setStyleSheet(f"color:{color};font-size:12px;padding:0 6px;")
        if state == "error":
            self._log("error", f"Preferences {text}")

    def _cal_context(self) -> dict:
        """What the BD Calibration tab checks files and records against."""
        c = self._client()
        return {"client": c if c.session else None, "setup": self.setup,
                "sample_id": self.trajectory.meta.meta_sample.text().strip(),
                "mode": (self.bridge.hello_info or {}).get("mode")}

    _LOG_COLORS = {"error": "#f38ba8", "warning": "#fab387", "info": "#a6e3a1", "debug": "#6c7086"}

    def _log_visible(self, level: str) -> bool:
        f = self.log_filter.currentIndex()
        return f == 0 or (f == 1 and level == "error") or (f == 2 and level in ("warning", "error"))

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
        self.statusBar().setStyleSheet(f"QStatusBar{{background:{bg};border-top:2px solid {border};}}")

    @property
    def running(self) -> bool:
        # The engine sets its result as the very last step of a run; a scanlist runs until its last scan.
        return (self.engine is not None and self.engine.result is None) or self._sl is not None

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

    # ---------------------------------------------------------------- session state
    # Login, logout, session expiry and account changes all end in _apply_session(), which sets
    # every user-owned widget from the store or to an explicit default -- nothing of a previous
    # user survives because a key happens to be missing.  It never touches an instrument: only
    # widgets are filled (stage positions are *read*).
    def _after_login(self) -> None:
        c = self._client()
        user = dict(c.user or {})
        err = ""
        try:
            prefs = c.call("prefs.get", {}, 10.0)
            ok = isinstance(prefs, dict)
        except ServiceError as exc:
            prefs, ok = {}, False
            err = (f"could not be loaded ({error_text(exc)}); nothing will be saved until you log in "
                   "again, so the stored values are not overwritten")
        stash, self._stash = self._stash, None
        self.store.reset(prefs if ok else {}, enabled=ok, error=err)
        restored = False
        if stash and ok and stash.get("user") == user.get("username"):
            self.store.update(stash.get("prefs") or {})          # the expired session's unsaved changes
            restored = True
        elif stash:
            self._log("warning", f"Unsaved changes of {stash.get('user')} from an expired session were "
                      "discarded (another account logged in).")
        self._session_user = user
        self.user_lbl.setText(f"{user.get('username')} ({user.get('role')})")
        self.login_btn.setText("Log out")
        setup = self.store.get("setup")
        self._apply_session(user, setup if setup in SETUPS else "longitudinal")
        if restored:
            if stash.get("bd") and stash["bd"].get("setup") == self.setup:
                self.bd_panel.restore_draft(stash["bd"])
            self._log("info", "Restored the unsaved changes of your expired session.")
        self._log("info", f"Logged in as {user.get('username')}")
        if err:
            self._log("error", f"Preferences {err}")

    def _apply_session(self, user: dict | None, setup: str) -> None:
        self._restoring = True
        try:
            self._ws_timer.stop()
            self.profile.load(user)
            self.dir_edit.setText(self.store.get("data_dir") or str(paths.default_measurement_dir()))
            self.kerr_state.set(bool(self.store.get("kerr_display", False)))
            self._reload_configs()
            self.trajectory.hardware.reset_session()
            self.calibration.reset_session()
            self.plot1d.clear()
            self.map2d.clear()
            self._map_data, self._map_axes = {}, None
            self._enter_setup(setup)
        finally:
            self._restoring = False
        try:
            self.browser.refresh()
        except Exception:  # noqa: BLE001 - the folder may not exist yet
            pass
        self._update_control_btn()
        self._update_scanlist_info()
        self._show_estimate()

    def _enter_setup(self, setup: str) -> None:
        """Show *setup* with this account's working state for it (or defaults).  No prompts."""
        self.setup = setup
        self.setup_btns[setup].setChecked(True)
        self.config_list.set_setup(setup)
        self.trajectory.set_setup(setup)
        self.calibration.update_setup()                # ignores position replies of the other stage
        self._apply_working(setup)
        self.calibration.refresh_positions()
        self.bd_panel.reload_from_service()            # the active calibration, read from the service

    def _default_operator(self) -> str:
        return str(self.store.get("default_operator") or (self._session_user or {}).get("full_name") or "")

    def _apply_working(self, setup: str) -> None:
        ws = self.store.get(working_key(setup))
        ws = ws if isinstance(ws, dict) else {}
        self.trajectory.apply_working_state(ws.get("trajectory"))
        meta = self.trajectory.meta
        meta.reset(operator=self._default_operator() if ws.get("experiment") is None else "")
        meta.load_values(ws.get("experiment") or {})
        meta.load_values(ws.get("optics") or {})
        self.calibration.timescan.reset()
        self.calibration.timescan.load(ws.get("calib_scan") or {})
        name = ws.get("preset")
        if name in self.configs.get(setup, {}):
            self._preset_name[setup] = name
            self._preset_clean[setup] = ws.get("preset_clean") or canonical(self._preset_value())
        else:
            self._preset_name[setup] = None
            self._preset_clean[setup] = None
        self.config_list.select(self._preset_name[setup], setup)
        self._last_sample = meta.meta_sample.text().strip()
        self._update_preset_dirty()

    def _capture_working(self) -> dict:
        exp, optics = split_metadata(self.trajectory.meta.get_values())
        return {"v": WORKING_VERSION, "trajectory": self.trajectory.working_state(), "experiment": exp,
                "optics": optics, "calib_scan": self.calibration.timescan.values(),
                "preset": self._preset_name[self.setup], "preset_clean": self._preset_clean[self.setup]}

    def _working_changed(self, *_a) -> None:
        if self._restoring:
            return
        self._update_preset_dirty()
        self._ws_timer.start()

    def _save_working(self) -> None:
        self._ws_timer.stop()
        if not self._restoring:
            self.store.set(working_key(self.setup), self._capture_working())

    def _login_logout(self) -> None:
        c = self._client()
        if not c.session:
            self.show_login()
            return
        if self.running:
            QMessageBox.warning(self, "Measurement running", "Stop the measurement before logging out.")
            return
        if not self.bd_panel.confirm_discard("logging out"):
            return
        self._save_working()
        err = self.store.flush_sync()
        if err and QMessageBox.question(
                self, "Log out", f"Your latest changes could not be saved:\n{err}\n\nLog out anyway and lose "
                "them?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            if c.lease_holder:
                c.release_lease()
            c.logout()
        except ServiceError as exc:
            QMessageBox.warning(self, "Log out", error_text(exc))
            return
        self._leave_session("Logged out")

    def _leave_session(self, message: str, expired: bool = False) -> None:
        """Back to the logged-out state (explicit defaults).  After an expiry the unsaved changes
        are kept in memory and given back if the same account logs in again."""
        user = self._session_user
        if expired and user:
            self._save_working()
            pending = self.store.pending()
            bd = self.bd_panel.draft()
            if pending or bd:
                self._stash = {"user": user.get("username"), "prefs": pending,
                               "bd": dict(bd, setup=self.setup) if bd else None}
        self._session_user = None
        self.store.reset({}, enabled=False)
        self.user_lbl.setText("session expired — log in" if expired else "not logged in")
        self.login_btn.setText("Log in")
        self._apply_session(None, "longitudinal")
        self._log("warning" if expired else "info", message)

    def _on_connection(self, what: str, info: dict) -> None:
        if what == "disconnected":
            self._set_mode_badge(None)
            self._log("warning", f"Device service not reachable ({info.get('reason', '')})")
        elif what == "restarted":
            self._log("warning", "The device service restarted; log in again.")
        elif what == "session_lost":
            if self._session_user is not None:
                if self.running:
                    self._session_lost_pending = True       # reset once the run has ended
                    self.user_lbl.setText("session expired")
                    self.login_btn.setText("Log in")
                else:
                    self._leave_session(f"Session expired ({info.get('message', '')}); log in again. "
                                        "Unsaved changes are given back if you log in with the same account.",
                                        expired=True)
        elif what == "lease_lost":
            self._log("error", "Control of the setup was lost (lease ended).")
            if self.engine is not None and self.running:
                self.engine.lease_lost()
        self._update_control_btn()

    def _on_event(self, topic: str, body: dict) -> None:
        if topic.startswith("readback."):
            dev, attr, q = body.get("device"), body.get("attr"), body.get("quality", "")
            hw = self.trajectory.hardware
            hw.set_readout(dev, attr, body.get("value"), q)
            if dev == "hall" and attr == "field_mT":
                self.trajectory.add_field_reading(body.get("value"))
            if dev == f"stage_{self.setup}" and attr in ("x_mm", "y_mm", "z_mm"):
                self.calibration.set_position(attr[0], body.get("value"), q)
            if dev == "lockin" and attr == "xy" and isinstance(body.get("value"), dict):
                v = body["value"]
                hw.set_readout("lockin", "x_V", v.get("x_V"), q)
                hw.set_readout("lockin", "r_V", v.get("r_V"), q)
        elif topic == "lease":
            self._update_control_btn(body)
        elif topic.startswith("state."):
            self.trajectory.hardware.set_device_state(body.get("device", ""), body.get("state", ""),
                                                      body.get("status", ""))
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
        """Switch setup: park this setup's working state, bring back the other's (or its
        defaults) -- never the other setup's draft under this setup's preset name."""
        if setup == self.setup or setup not in SETUPS:
            self.setup_btns[self.setup].setChecked(True)
            return
        if self.running or not self.bd_panel.confirm_discard(
                f"switching to the {SETUP_LABEL[setup]} setup"):
            self.setup_btns[self.setup].setChecked(True)
            return
        self._save_working()
        self._restoring = True
        try:
            self._enter_setup(setup)
        finally:
            self._restoring = False
        self.store.set("setup", setup)
        self._update_scanlist_info()
        self._show_estimate()

    def _on_workflow_changed(self, key: str) -> None:
        self.right.configure_for(key)
        # Restoring a saved state (login, setup switch) must not pull the user away from the
        # Calibration or Log tab; picking a measurement does show its plot.
        if not self._restoring or self.live_tabs.currentWidget() in (self.map2d, self.plot1d):
            self.live_tabs.setCurrentWidget(self.map2d if key == "xy_sweep" else self.plot1d)

    # ================================================================ named configurations
    def _preset_value(self) -> dict:
        """A configuration = the recipe: workflow, its parameters and the optics -- never the
        sample identity or the operator (those belong to the experiment)."""
        _exp, optics = split_metadata(self.trajectory.meta.get_values())
        return {"schema": PRESET_SCHEMA, "workflow": self.trajectory.current, "params": self.trajectory.params(),
                "metadata": optics}

    _config_value = _preset_value              # earlier name

    def _update_preset_dirty(self) -> bool:
        s = self.setup
        name = self._preset_name[s]
        dirty = bool(name and self._preset_clean[s] and canonical(self._preset_value()) != self._preset_clean[s])
        self.config_list.set_dirty(s, name if dirty else None)
        return dirty

    def _reload_configs(self) -> None:
        self.configs = {s: {} for s in SETUPS}
        c = self._client()
        if c.session:
            try:
                for p in c.call("presets.list", {}):
                    wf = p["workflow"]
                    if wf.startswith("samba_mini/"):
                        s = wf.split("/", 1)[1]
                        if s in self.configs:
                            self.configs[s][p["name"]] = p["value"]
            except ServiceError as exc:
                self._log("warning", f"configurations not loaded: {error_text(exc)}")
                self.config_list.set_status(f"Configurations could not be loaded: {error_text(exc)}", "error")
        for s in SETUPS:
            if self._preset_name[s] not in self.configs[s]:
                self._preset_name[s], self._preset_clean[s] = None, None
            self.config_list.set_items(s, sorted(self.configs[s]), self._preset_name[s])

    def _load_config(self, setup: str, name: str) -> None:
        """A configuration was clicked.  Unsaved changes to the selected one are never dropped
        silently: Save / Discard / Cancel."""
        prev = self._preset_name[setup]
        if self.running:
            self.config_list.select(prev, setup)
            return
        dirty = setup == self.setup and self._update_preset_dirty()
        if name == prev and not dirty:
            return
        if dirty:
            what = "reverting to the saved version" if name == prev else f"loading '{name}'"
            ans = QMessageBox.question(
                self, "Unsaved configuration changes",
                f"The current settings differ from the saved configuration '{prev}'.\n\nSave them into '{prev}' "
                f"before {what}?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if ans == QMessageBox.StandardButton.Cancel or (
                    ans == QMessageBox.StandardButton.Save and not self._save_config(setup, prev, "update")):
                self.config_list.select(prev, setup)
                return
        v = self.configs.get(setup, {}).get(name)
        if not v:
            self.config_list.select(prev, setup)
            return
        self._apply_preset(setup, name, v)

    def _apply_preset(self, setup: str, name: str, v: dict) -> None:
        """Fill the widgets from a configuration.  Never applies instrument settings, never
        changes the sample or operator (legacy configurations that stored them ask first)."""
        self._restoring = True
        try:
            wf = v.get("workflow", self.trajectory.current)
            self.trajectory.set_workflow(wf)
            self.trajectory.set_params(wf, v.get("params", {}))
            meta = dict(v.get("metadata", {}))
            meta.pop("export_csv", None)
            if v.get("geometry") and "incidence" not in meta:      # configurations saved before the Samba layout
                meta["incidence"] = v["geometry"]
            legacy = {k: meta.pop(k) for k in list(meta) if k in LEGACY_IDENTITY_KEYS}
            self.trajectory.set_metadata({k: val for k, val in meta.items() if k in OPTICS_KEYS})
        finally:
            self._restoring = False
        self._preset_name[setup] = name
        self._preset_clean[setup] = canonical(self._preset_value())
        self.config_list.select(name, setup)
        self.config_list.clear_error()
        self._update_preset_dirty()
        self._save_working()
        self._show_estimate()
        self._log("info", f"Loaded configuration '{name}' (settings only; no instrument was changed)")
        if legacy:
            self._offer_legacy_identity(name, legacy)

    def _offer_legacy_identity(self, name: str, legacy: dict) -> None:
        """Configurations saved before this version may hold a sample / operator.  Applying them
        is an explicit choice, and then it goes through the normal sample-change handling."""
        if "sample_id" not in legacy and legacy.get("sample") not in (None, ""):
            legacy["sample_id"] = legacy["sample"]
        legacy.pop("sample", None)
        current, _ = split_metadata(self.trajectory.meta.get_values())
        diff = {k: val for k, val in legacy.items()
                if k in EXPERIMENT_KEYS and val not in (None, "") and current.get(k) != val}
        if not diff:
            return
        lines = "\n".join(f"  {k}: {val!r}  (now {current.get(k)!r})" for k, val in diff.items())
        ans = QMessageBox.question(
            self, "Sample information in this configuration",
            f"'{name}' was saved by an earlier Samba_mini together with experiment information:\n{lines}\n\n"
            "Configurations no longer change the current experiment. Copy this information into the current "
            "experiment?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            self._log("info", f"The sample information stored in '{name}' was not applied.")
            return
        self.trajectory.set_metadata(diff)
        self._log("info", f"Experiment information copied from '{name}': {', '.join(diff)}")
        self._on_sample_edited(self.trajectory.meta.meta_sample.text().strip())

    def _presets_ok(self, feature: str = "presets-atomic-1") -> bool:
        c = self._client()
        if not c.session:
            self.show_login()
            return False
        if not c.has_feature(feature):
            self.config_list.set_status("The running device service is older than this Samba_mini: restart it "
                                        "(Samba_controls) when no measurement runs.", "error")
            return False
        return True

    def _save_config(self, setup: str, name: str, mode: str | None = None) -> bool:
        """+ (mode "create": refuses an existing name) or S (mode "update").  A failed save
        leaves the settings untouched and the error visible under the list."""
        c = self._client()
        if not c.session:
            self.show_login()
            return False
        if mode is None:
            mode = "update" if name in self.configs.get(setup, {}) else "create"
        if not c.has_feature("presets-atomic-1"):
            mode = "overwrite"                    # older service: plain save (no create/update modes)
        value = self._preset_value()
        try:
            c.call("presets.save", {"workflow": f"samba_mini/{setup}", "name": name, "value": value,
                                    "mode": mode}, 10.0)
        except ServiceError as exc:
            self.config_list.set_status(f"'{name}' not saved: {exc.message} — your settings are unchanged.",
                                        "error")
            self._log("error", f"configuration '{name}' not saved: {error_text(exc)}")
            return False
        self.configs[setup][name] = value
        self._preset_name[setup] = name
        self._preset_clean[setup] = canonical(value)
        self.config_list.set_items(setup, sorted(self.configs[setup]), name)
        self.config_list.clear_error()
        self._update_preset_dirty()
        self._save_working()
        self._log("info", f"Saved configuration '{name}'")
        return True

    def _copy_config(self, setup: str, src: str, new: str) -> None:
        """Copy the *saved* configuration (not the current, possibly edited, settings)."""
        if not self._presets_ok():
            return
        try:
            res = self._client().call("presets.copy", {"workflow": f"samba_mini/{setup}", "name": src,
                                                       "new_name": new}, 10.0)
        except ServiceError as exc:
            self.config_list.set_status(f"Not copied: {exc.message}", "error")
            self._log("error", f"configuration not copied: {error_text(exc)}")
            return
        self.configs[setup][new] = res["value"]
        self.config_list.set_items(setup, sorted(self.configs[setup]), self._preset_name[setup])
        self.config_list.clear_error()
        self._update_preset_dirty()
        self._log("info", f"Copied configuration '{src}' to '{new}'")

    def _delete_config(self, setup: str, name: str) -> None:
        try:
            self._client().call("presets.delete", {"workflow": f"samba_mini/{setup}", "name": name})
        except ServiceError as exc:
            self.config_list.set_status(f"Not deleted: {exc.message}", "error")
            self._log("error", error_text(exc))
            return
        self.configs[setup].pop(name, None)
        if self._preset_name[setup] == name:
            self._preset_name[setup], self._preset_clean[setup] = None, None
        self.config_list.set_items(setup, sorted(self.configs[setup]), self._preset_name[setup])
        self._update_preset_dirty()
        self._save_working()

    def _rename_config(self, setup: str, old: str, new: str) -> None:
        """Atomic rename in the service; refused (nothing changes) when *new* exists."""
        if not self._presets_ok():
            return
        try:
            res = self._client().call("presets.rename", {"workflow": f"samba_mini/{setup}", "name": old,
                                                         "new_name": new}, 10.0)
        except ServiceError as exc:
            self.config_list.set_status(f"Not renamed: {exc.message}", "error")
            self._log("error", f"configuration not renamed: {error_text(exc)}")
            return
        self.configs[setup].pop(old, None)
        self.configs[setup][new] = res["value"]
        if self._preset_name[setup] == old:
            self._preset_name[setup] = new
        self.config_list.set_items(setup, sorted(self.configs[setup]), self._preset_name[setup])
        self.config_list.clear_error()
        self._update_preset_dirty()
        self._save_working()
        self._log("info", f"Renamed configuration '{old}' to '{new}'")

    # ================================================================ run control
    def _check_ready(self):
        """Checks shared by single runs and scanlists; returns (workflow, params, points) or None."""
        if self.running:
            return None
        c = self._client()
        if self.bridge.probe(1.5) is None:
            QMessageBox.warning(self, "Device service", "The device service is not running. "
                                "Start Samba_controls first.")
            return None
        if not c.session and not self.show_login():
            return None
        wf = self.trajectory.workflow()
        try:
            params = wf.coerce(self.trajectory.params())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid parameters", str(exc))
            return None
        n, _secs, msg = self.trajectory.estimate()
        if msg:
            QMessageBox.warning(self, "Cannot start", msg)
            return None
        return wf, params, n

    def _confirm_real(self, wf, params: dict, what: str) -> bool:
        if (self.bridge.hello_info or {}).get("mode") != "real":
            return True
        moves = wf.uses_stage == "required" or params.get("position_stage")
        text = (f"Start {what} on the REAL setup?\n\nThe coil command will be set"
                + (", the lock-in oscillator switched on" if wf.uses_lockin else "")
                + (" and the stage moved" if moves else "") + ".")
        return QMessageBox.question(self, "Real hardware", text) == QMessageBox.StandardButton.Yes

    def _take_control(self) -> bool:
        c = self._client()
        try:
            if not c.lease_holder:
                c.acquire_lease()
        except ServiceError as exc:
            QMessageBox.warning(self, "Control of the setup", error_text(exc))
            return False
        self._update_control_btn()
        return True

    def start_run(self) -> None:
        """Start, as in Samba: the Calibration tab's time scan while that tab is open, a scanlist
        while the Scanlist tab is open, else the Trajectory measurement."""
        if self.live_tabs.currentWidget() is self.calibration:
            self.start_calibration_scan()
            return
        if self.bottom_tabs.currentWidget() is self.scanlist:
            self.start_scanlist()
            return
        ready = self._check_ready()
        if not ready:
            return
        wf, params, n = ready
        if not self._confirm_real(wf, params, wf.title) or not self._take_control():
            return
        if not self._freeze_calibration():
            return
        self._sl = None
        self._run_t0 = time.time()
        self._list = {"total": 1, "done": 0}
        self._set_running_ui(True)
        self._launch(wf, params, n)

    def _freeze_calibration(self) -> bool:
        """Read the active λ/2 calibration from the service (not a cache) and freeze it for the
        run: the file and the live θ display use this one snapshot until the run ends."""
        c = self._client()
        if not c.has_feature("calibration-records-1"):
            self._run_cal = None
            self._log("warning", "The running device service is older and has no calibration records: this "
                      "run records no λ/2 calibration.")
            return True
        act = self.bd_panel.refresh_active()
        if self.bd_panel.active_error:
            QMessageBox.warning(self, "Calibration", f"The active λ/2 calibration {self.bd_panel.active_error}."
                                "\nThe run was not started.")
            return False
        from Samba_mini.panels.bd_calibration import snapshot
        self._run_cal = snapshot(act)
        sample = self.trajectory.meta.meta_sample.text().strip()
        if self._run_cal and (self._run_cal.get("activated_for_sample") or "") != sample:
            self._log("warning", f"The active λ/2 calibration was activated for sample "
                      f"{self._run_cal.get('activated_for_sample')!r}; the current sample is {sample!r}.")
        if self.bd_panel.is_dirty():
            self._log("warning", "The BD Calibration editor has unsaved values; they are not used by this run "
                      "(only the saved, active calibration is).")
        return True

    def _launch(self, wf, params: dict, n: int, extra_meta: dict | None = None, label: str = "",
                config_name: str | None = None) -> None:
        meta = self.trajectory.metadata()
        data_dir = self._data_dir()
        notebook = self.store.get("lab_notebook") or os.path.join(data_dir, "lab_notebook_SambaMini.csv")
        em = {k: meta[k] for k in RUN_META_KEYS if k in meta}
        em.update(extra_meta or {})
        snap = self._run_cal
        req = RunRequest(wf.key, params, setup=self.setup, geometry=meta["incidence"], sample=meta["sample"],
                         operator=meta["operator"], notes=meta["notes"],
                         config_name=(config_name if config_name is not None
                                      else self.config_list.current_name() or ""), save_dir=data_dir,
                         export_csv=bool(self.store.get("export_csv", False)), lab_notebook=notebook,
                         extra_metadata=em, bd_calibration=list(snap["values"]) if snap else None,
                         bd_calibration_record=dict(snap) if snap else None)
        self._push_kerr_calibration(snap["values"] if snap else [0.0] * 6)   # display = file
        self.bd_panel.set_running(True, snap)
        self._prepare_plots(wf.key, params, n)
        self.engine = ScanEngine(self._client(), req, self.engine_bridge.callbacks())
        self._scan_t0 = time.time()
        self._progress = (0, n)
        self._sl_hall = []
        # The service locks only the devices a run declares: a run that does not use the stage
        # (calibration and time scans without stage positioning) leaves it free to move, e.g. to
        # focus while the signal is recorded.
        stage_free = f"stage_{self.setup}" not in wf.required_devices(params, self.setup)
        self.calibration.set_stage_enabled(stage_free, during_run=True)
        self.engine.start()
        self._log("info", f"Started {wf.title} ({n} points){label}"
                  + (" — the stage can be moved meanwhile" if stage_free else ""))

    # ---------------------------------------------------------------- calibration time scan
    def calibration_scan_params(self) -> dict:
        """Exactly what the calibration time scan applies: the Calibration tab's own settings
        (samples, interval, Int, coil, lock-in) on top of the time scan's defaults; no stage move.
        Nothing comes from the Trajectory tab."""
        ts = self.calibration.timescan.values()
        params = WORKFLOWS["time"].defaults()
        params.update({k: ts[k] for k in ("b", "volt", "sensi", "lockin_freq", "time_const", "phase")})
        params.update(duration=round(ts["n"] * ts["interval"], 9), interval=ts["interval"],
                      acq_time=ts["acq_time"], position_stage=False)
        return params

    def _copy_hardware_to_calibration_scan(self) -> None:
        hw = self.trajectory.hardware
        v = dict(hw.lockin_values(), b=hw.coil.value())
        self.calibration.timescan.load(v)
        self._log("info", "Calibration scan: coil and lock-in values copied from the Hardware box.")

    def start_calibration_scan(self) -> None:
        """Samba's calibration time scan with the Calibration tab's own settings.  The file is
        named ..._calibration.h5; when it completes it is fitted in the BD Calibration tab."""
        if self.running:
            return
        c = self._client()
        if self.bridge.probe(1.5) is None:
            QMessageBox.warning(self, "Device service", "The device service is not running. "
                                "Start Samba_controls first.")
            return
        if not c.session and not self.show_login():
            return
        wf = WORKFLOWS["time"]
        try:
            params = wf.coerce(self.calibration_scan_params())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid parameters", f"{exc}\n\n(The settings are in the Calibration "
                                "tab's calibration-scan box.)")
            return
        n = plans.time_points(params["duration"], params["interval"])
        if not self._confirm_real(wf, params, "a calibration time scan") or not self._take_control():
            return
        if not self._freeze_calibration():
            return
        self._save_working()
        self._sl = None
        self._calib_run = True
        self._run_t0 = time.time()
        self._list = {"total": 1, "done": 0}
        self._set_running_ui(True)
        self.calibration.begin_scan(params["duration"], f"Calibration time scan: {n} samples, "
                                    f"{params['interval']:g} s apart, Int {params['acq_time']:g} s")
        self._launch(wf, params, n, {"calibration_scan": True}, " — calibration time scan",
                     config_name="calibration")

    # ---------------------------------------------------------------- scanlist
    def start_scanlist(self) -> None:
        ready = self._check_ready()
        if not ready:
            return
        wf, params, n = ready
        sv = self.scanlist.values()
        if sv["field_flip"] and abs(float(params.get("b", 0.0))) == 0.0:
            QMessageBox.warning(self, "Field flip", "The coil command is 0 V, so reversing it does nothing. "
                                "Set the coil command in the Hardware box, or switch the field flip off.")
            return
        what = f"a scanlist of {sv['n_scans']} × {wf.title}"
        if not self._confirm_real(wf, params, what) or not self._take_control():
            return
        if not self._freeze_calibration():          # one calibration snapshot for the whole scanlist
            return
        self._sl = {"wf": wf, "params": params, "n": n, "i": 0, "total": sv["n_scans"], "flip": sv["field_flip"],
                    "order": sv["order"], "name": sv["name"], "results": [], "stop": False,
                    "dir": os.path.join(self._data_dir(), f"ScanLists_{SETUP_LABEL[self.setup]}")}
        self._run_t0 = time.time()
        self._list = {"total": sv["n_scans"], "done": 0}
        self._set_running_ui(True)
        self._log("info", f"Scanlist '{sv['name']}': {sv['n_scans']} × {wf.title}"
                  + (f", field flip {sv['order']}" if sv["field_flip"] else ""))
        self._next_scan()

    def _next_scan(self) -> None:
        sl = self._sl
        if sl is None:
            return
        if sl["stop"] or sl["i"] >= sl["total"]:
            self._finish_scanlist()
            return
        i = sl["i"]
        params = dict(sl["params"])
        phase = flip_phase(i, sl["order"]) if sl["flip"] else 0
        if phase:
            params["b"] = -float(params["b"])
        self._list["done"] = i
        self.scanlist.mark(i, "running")
        label = f" — scan {i + 1}/{sl['total']}"
        if sl["flip"]:
            label += f", field {phase_label(phase)} (coil {params['b']:+.4g} V)"
        self._launch(sl["wf"], params, sl["n"], {"scanlist_name": sl["name"], "scanlist_index": i + 1,
                                                 "scanlist_phase": phase_label(phase)}, label)

    def _scanlist_scan_done(self, result: dict) -> bool:
        """Record the finished scan; True when the next scan should start."""
        sl = self._sl
        status = result.get("status")
        self.scanlist.mark(sl["i"], status or "?", result.get("path") or "")
        if status == "completed" and result.get("path"):
            hall = [h for h in self._sl_hall if h == h]
            field_T = float(np.median(hall)) / 1000.0 if hall else float("nan")
            sl["results"].append((result["path"], +1, field_T))
        elif result.get("path"):
            self._log("warning", f"{status} scan not recorded in the scanlist: {result['path']}")
        sl["i"] += 1
        return (status == "completed" and not sl["stop"] and sl["i"] < sl["total"]
                and not result.get("cleanup_problems"))

    def _finish_scanlist(self) -> None:
        sl, self._sl = self._sl, None
        if sl is None:
            return
        if not sl["results"]:
            self._log("warning", f"Scanlist '{sl['name']}' ended without a completed scan; no list written.")
            return
        try:
            path = write_scanlist(sl["dir"], sl["name"], sl["results"], sl["flip"], sl["order"])
        except OSError as exc:
            self._log("error", f"Scanlist file not written: {exc}")
            QMessageBox.warning(self, "Scanlist", f"The scanlist file could not be written:\n{exc}")
            return
        self._log("info", f"✓ Scanlist complete — {len(sl['results'])}/{sl['total']} scans — {path}")

    def toggle_pause(self) -> None:
        if not self.running or self.engine is None or self.engine.result is not None:
            return
        if self._run_state == "PAUSED":
            self.engine.resume()
        else:
            self.engine.pause()

    def abort_run(self) -> None:
        if self._sl is not None:
            self._sl["stop"] = True            # Samba: Abort ends the whole scanlist
        if self.engine is not None and self.engine.result is None:
            self.engine.abort()
            self.abort_btn.setEnabled(False)

    def _set_running_ui(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.abort_btn.setEnabled(running)
        self.trajectory.set_editable(not running)
        self.scanlist.set_editable(not running)
        self.config_list.setEnabled(not running)
        for b in self.setup_btns.values():
            b.setEnabled(not running)
        self.login_btn.setEnabled(not running)
        self.calibration.set_stage_enabled(not running)
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
        if self._sl is not None:
            if self._scanlist_scan_done(result):
                QTimer.singleShot(0, self._next_scan)
                return
            self._finish_scanlist()
        was_calib = self._calib_run
        self._calib_run = False
        self._set_running_ui(False)
        self._run_state = "IDLE"
        self._tint("IDLE")
        self._run_cal = None
        self.bd_panel.set_running(False)
        self._on_active_calibration(self.bd_panel.active())      # the display follows the active one again
        self._update_status_bar()
        self._show_estimate()
        if self._session_lost_pending:
            self._session_lost_pending = False
            self._leave_session("Session expired during the measurement; log in again. Unsaved changes are "
                                "given back if you log in with the same account.", expired=True)
            return
        if was_calib:
            if status == "completed" and result.get("path"):
                QTimer.singleShot(0, lambda p=result["path"]: self._fit_acquired_calibration(p))
            else:
                self._log("warning", f"Calibration scan {status}: not fitted. Use Fit & Import… in the BD "
                          "Calibration tab to choose a file explicitly.")

    def _fit_acquired_calibration(self, path: str) -> None:
        """② of the calibration workflow: fit the scan just acquired, for review."""
        if self.bd_panel.import_file(path, auto=True):
            self.bottom_tabs.setCurrentWidget(self.bd_panel)
            self._log("info", f"Calibration scan fitted: review the values in the BD Calibration tab, then Save "
                      f"and Activate ({os.path.basename(path)}).")
        else:
            self._log("warning", f"Calibration scan not fitted: {self.bd_panel.status_text()}")

    # ---------------------------------------------------------------- BD calibration / θ display
    def _push_kerr_calibration(self, vals: list) -> None:
        self.plot1d.set_kerr_calibration(vals)
        self.map2d.set_kerr_calibration(vals)

    def _on_kerr_toggled(self, enabled: bool) -> None:
        if not self._restoring:
            self.store.set("kerr_display", bool(enabled))

    def _on_active_calibration(self, active) -> None:
        """The active calibration changed (or was read).  While a run is going its display stays
        on the snapshot the run started with -- the same values its file records."""
        if self.running:
            if not self._restoring:
                self._log("info", "Active calibration changed: it applies from the next run; the running "
                          "measurement keeps the one it started with.")
            return
        rec = (active or {}).get("record")
        self._push_kerr_calibration(rec["values"] if rec else [0.0] * 6)

    def _on_sample_edited(self, name: str) -> None:
        """Samba: a new sample asks for a fresh λ/2 calibration.  Here: the active calibration
        (activated for another sample) can be deactivated -- persistently, in the service, so a
        setup switch or a restart does not bring it back."""
        if not name or name == self._last_sample:
            self.bd_panel.context_changed()
            return
        self._last_sample = name
        self._working_changed()
        self.bd_panel.context_changed()
        act = self.bd_panel.active()
        if act is None or (act.get("sample_id") or "") == name:
            return
        ans = QMessageBox.question(
            self, "New sample",
            f"Sample changed to '{name}'. The active λ/2 calibration "
            f"({describe_record(act.get('record'), short=True)}) was activated for "
            f"{repr(act.get('sample_id')) if act.get('sample_id') else 'no particular sample'}.\n\n"
            f"Start a new calibration? The active one is deactivated, so measurements of '{name}' record "
            "no calibration until you acquire, save and activate a new one.")
        if ans != QMessageBox.StandardButton.Yes:
            self._log("info", f"Sample '{name}': kept the active calibration {describe_record(act.get('record'), short=True)}.")
            return
        if not self.bd_panel.confirm_discard("starting a new calibration"):
            return
        if self.bd_panel.deactivate():
            self.bd_panel.new_editor()
            self.bd_panel.set_status(f"New sample '{name}': acquire (Calibration tab) or enter the λ/2 "
                                     "calibration, then Save and Activate.")
            self.bottom_tabs.setCurrentWidget(self.bd_panel)

    def _update_scanlist_info(self) -> None:
        wf = self.trajectory.workflow()
        n, secs, msg = self.trajectory.estimate()
        cfg = self.config_list.current_name() or ""
        k = self.scanlist.values()["n_scans"]
        if msg:
            plan = f"<span style='color:#f38ba8'>{msg}</span>"
        else:
            each = _hms(secs) if secs is not None else "—"
            total = _hms(secs * k) if secs is not None else "—"
            plan = (f"Each scan: <b>{n}</b> points, ≈ {each}<br>{k} scans: ≈ <b>{total}</b>"
                    + (" + moves" if wf.key in ("x_sweep", "y_sweep", "xy_sweep") else ""))
        p = self.trajectory.params()
        sv = self.scanlist.values()
        if sv["field_flip"] and "b" in p and float(p["b"]) == 0.0:
            plan += ("<br><span style='color:#fab387'>Coil command is 0 V: the field flip would do nothing "
                     "(set it in the Hardware box).</span>")
        self.scanlist.set_measurement(f"{wf.title}" + (f"  ·  {cfg}" if cfg else ""),
                                      flip_allowed="b" in {p.name for p in wf.params}, plan_html=plan)
        if self._sl is None:
            self.scanlist.preview(float(p["b"]) if "b" in p else None)
        self.scanlist.set_auto_name(self.trajectory.meta.build_scan_name(
            volt=float(p.get("volt", 0.0)), freq_Hz=float(p.get("lockin_freq", 0.0)), config_name=cfg or wf.short))

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
        if self._calib_run:
            self.calibration.add_point(float(ev.get("x", math.nan)), rec)
        if self._sl is not None:
            h = rec.get("hall_field_mT")
            if isinstance(h, (int, float)):
                self._sl_hall.append(float(h))
        self.status_lbl.setText(
            f"Point {self._progress[0]}/{self._progress[1]} — coil {rec.get('coil_V', math.nan):+.4g} V, "
            f"Hall {rec.get('hall_field_mT', math.nan):.4g} mT, DC {rec.get('balanced_V', math.nan):.5g} V")

    # ================================================================ status bar
    def _update_status_bar(self) -> None:
        """Samba's seven fields; 'Dead' is the share of time not spent integrating."""
        c = self._client()
        mode = (self.bridge.hello_info or {}).get("mode", "offline") if c.connected else "offline"
        self.sb["Mode"].setText(f"Mode: {mode}")
        self.sb["User"].setText(f"User: {(c.user or {}).get('username', '—')}")
        if not (self.running and self._run_t0):
            for key in ("Scan", "Start", "Elapsed", "Run left", "Scan left", "Dead", "Done"):
                self.sb[key].setText("—")
            return
        now = time.time()
        lst = self._list
        done, total = self._progress
        frac_scan = done / total if total else 0.0
        frac_run = (lst["done"] + frac_scan) / max(lst["total"], 1)
        run_el = now - self._run_t0
        scan_el = now - (self._scan_t0 or self._run_t0)
        self.sb["Scan"].setText(f"{lst['done'] + 1}/{lst['total']}")
        self.sb["Start"].setText(time.strftime("%H:%M:%S", time.localtime(self._run_t0)))
        self.sb["Elapsed"].setText(_hms(run_el))
        self.sb["Run left"].setText(_hms(run_el * (1 - frac_run) / frac_run) if frac_run > 0 else "—")
        self.sb["Scan left"].setText(_hms(scan_el / done * (total - done)) if done > 0 else "—")
        acq = (self.engine.params or {}).get("acq_time") if self.engine else None
        if acq and done and scan_el > 0:
            self.sb["Dead"].setText(f"{max(0.0, (scan_el - done * acq) / scan_el) * 100:.0f}%")
        else:
            self.sb["Dead"].setText("—")
        self.sb["Done"].setText(f"{frac_run * 100:.0f}%")

    def _show_estimate(self) -> None:
        """Samba shows the pre-scan estimate in the status line under the plot."""
        if self.running:
            return
        text = self.trajectory.summary_text()
        bad = ":" in text and "points" not in text
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(f"color:{'#f38ba8' if bad else '#6c7086'};font-size:11px;padding:2px 6px;")

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
        if self._client().session and not self.bd_panel.confirm_discard("closing Samba_mini"):
            ev.ignore()
            return
        if self.running:
            ans = QMessageBox.question(self, "Measurement running",
                                       "A measurement is running. Abort it and close Samba_mini?\n"
                                       "(The data measured so far are kept.)")
            if ans != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self.abort_run()
            loop = QEventLoop()
            t0 = time.time()
            while self.running and time.time() - t0 < 120:
                loop.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
                time.sleep(0.05)
        c = self._client()
        if c.session:
            self._save_working()
            err = self.store.flush_sync()
            if err and QMessageBox.question(
                    self, "Close Samba_mini", f"Your latest changes could not be saved:\n{err}\n\nClose anyway "
                    "and lose them?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        QSettings("ETH-Intermag", "SambaMini").setValue("geometry", self.saveGeometry())
        try:
            if c.session:
                if c.lease_holder:
                    c.release_lease()
                c.logout()
        except ServiceError:
            pass
        self.store.reset({}, enabled=False)
        self.store.shutdown()
        self.bridge.close()
        super().closeEvent(ev)


def write_scanlist(directory: str, name: str, results: list, field_flip: bool, order: str) -> str:
    """Samba's scanlist .txt: header comments, then path / relay_sign / field_T per scan.

    The polarity comment records the field flip and order; there is no relay
    on the MiniMoke setups, so relay_sign is always +1.  field_T is the median
    Hall reading of the scan in tesla (NaN without Hall readings).
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{name}.txt")
    k = 2
    while os.path.exists(path):                      # same name again: _2, _3 … (Samba)
        path = os.path.join(directory, f"{name}_{k}.txt")
        k += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Scanlist: {name}  {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        f.write(f"# polarity: relay_flip=0 field_flip={int(bool(field_flip))} order={order}\n")
        f.write("# path\trelay_sign\tfield_T\n")
        for fn, rs, ft in results:
            f.write(f"{fn}\t{rs:+d}\t{ft:.6f}\n")
    return path


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
    from core.gui import install_crash_guard, set_windows_app_id, splash
    from Samba_mini import APP_ID
    install_crash_guard("samba_mini")          # log file + errors shown instead of closing the window
    set_windows_app_id(APP_ID)
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Samba_mini")
    if (REPO / "assets" / "samba_mini.ico").exists():
        app.setWindowIcon(QIcon(str(REPO / "assets" / "samba_mini.ico")))
    app.setOrganizationName("ETH-Intermag")
    splash.start("samba_mini", APP_ID)            # already up when started with python -m Samba_mini
    splash.status("Opening the main window…")
    splash.wait()
    w = MainWindow()
    splash.finish(w)
    return app.exec()
