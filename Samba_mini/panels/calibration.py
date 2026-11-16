"""Calibration tab (next to 2D Map / 1D Plot, as in Samba): a live signal plot
on the left and stage positioning on the right.

Samba's Calibration tab holds its own plot and the stage positioning group
(plus autofocus, which MiniMoke does not have).  The plot here is a rolling
signal monitor so the signal stays visible while the stage is positioned: it
shows the service's live readbacks (about once a second while no measurement
runs) and every measured point during a run.  Stage: readback, jog with
MiniMoke's odometer steps, go-to, STOP and "set zero here".

Every action goes through the device service and needs control of the setup
(the lease); the service rejects it otherwise.  Slow calls run off the GUI
thread.
"""

from __future__ import annotations

import collections
import math
import time

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
                             QWidget)

from core.gui.bridge import call_async, error_text
from core.scan.channels import SENSORS
from Samba_mini.panels.widgets import NoScrollComboBox, NoScrollDoubleSpinBox

JOG_STEPS_MM = [10.0, 1.0, 0.1, 0.01, 0.001, 0.0001, 0.00001]   # MiniMoke odometer steps
STAGE_TITLES = {"longitudinal": "Longitudinal stage · Thorlabs Kinesis",
                "polar": "Polar stage · Trinamic TMCM-3212"}

_AXIS = "color:#89b4fa;font-size:16px;font-weight:bold;"
_RB = "font-family:Consolas,'Courier New';font-size:14px;color:#a6e3a1;min-width:120px;"


MONITOR_WINDOWS = [("1 min", 60), ("5 min", 300), ("15 min", 900), ("1 h", 3600)]
#: service readbacks -> channel names
READBACK_CHANNELS = {("daq", "balanced_V"): "dc_signal", ("daq", "intensity_V"): "intensity",
                     ("hall", "field_mT"): "hall_field"}
LOCKIN_CHANNELS = {"x_V": "lockin_x", "y_V": "lockin_y", "r_V": "lockin_r", "theta_deg": "lockin_theta"}


class SignalMonitor(QWidget):
    """Rolling plot of up to two channels (Y1 left, Y2 right) against time."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        from core.plot_interact import eng_axis
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.y1 = NoScrollComboBox()
        self.y2 = NoScrollComboBox()
        for combo, default in ((self.y1, "dc_signal"), (self.y2, "intensity")):
            combo.addItem("—", "")
            for ch in SENSORS:
                combo.addItem(f"{ch.label} ({ch.unit})", ch.name)
            combo.setCurrentIndex(max(combo.findData(default), 0))
            combo.setSizeAdjustPolicy(NoScrollComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
            combo.currentIndexChanged.connect(lambda _i: self._redraw(force=True))
        self.window = NoScrollComboBox()
        for label, secs in MONITOR_WINDOWS:
            self.window.addItem(label, secs)
        self.window.currentIndexChanged.connect(lambda _i: self._redraw(force=True))
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        for w, label, color in ((self.y1, "Y1:", "#89b4fa"), (self.y2, "Y2:", "#f38ba8"),
                                (self.window, "Window:", "#a6adc8")):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{color};")
            row.addWidget(lbl)
            row.addWidget(w, 0 if w is self.window else 1)
        row.addWidget(clear)
        lay.addLayout(row)
        self.fig = Figure(facecolor="#1e1e2e")
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.ax2 = self.ax.twinx()
        self.fig.subplots_adjust(left=0.15, right=0.85, top=0.95, bottom=0.17)
        for ax in (self.ax, self.ax2):
            ax.set_facecolor("#12121f")
            for sp in ax.spines.values():
                sp.set_color("#45475a")
            ax.tick_params(colors="#a6adc8", labelsize=8)
            try:
                eng_axis(ax.yaxis)
            except Exception:  # noqa: BLE001 - cosmetic
                pass
        self.ax.set_xlabel("Time (s, 0 = now)", color="#a6adc8", fontsize=8)
        self.l1, = self.ax.plot([], [], color="#89b4fa", lw=1.3, marker=".", ms=3)
        self.l2, = self.ax2.plot([], [], color="#f38ba8", lw=1.3, marker=".", ms=3)
        self.canvas.setMinimumWidth(220)
        lay.addWidget(self.canvas, 1)
        hint = QLabel("Live: readbacks about once a second while idle, every point during a measurement.")
        hint.setStyleSheet("color:#6c7086;font-size:10px;")
        lay.addWidget(hint)
        self.data: dict[str, collections.deque] = {ch.name: collections.deque(maxlen=20000) for ch in SENSORS}
        self.units = {ch.name: (ch.label, ch.unit) for ch in SENSORS}
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._redraw)
        self._timer.start()

    def add(self, channel: str, value, t: float | None = None) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if channel in self.data and math.isfinite(v):
            self.data[channel].append((t or time.time(), v))
            self._dirty = True

    def clear(self) -> None:
        for d in self.data.values():
            d.clear()
        self._redraw(force=True)

    def _redraw(self, force: bool = False) -> None:
        if not (self._dirty or force) or not self.isVisible():
            return
        self._dirty = False
        now = time.time()
        span = float(self.window.currentData() or 60)
        for combo, line, ax, color in ((self.y1, self.l1, self.ax, "#89b4fa"),
                                       (self.y2, self.l2, self.ax2, "#f38ba8")):
            name = combo.currentData()
            pts = [(t - now, v) for t, v in self.data.get(name, ()) if now - t <= span] if name else []
            line.set_data([q[0] for q in pts], [q[1] for q in pts])
            label, unit = self.units.get(name, ("", ""))
            ax.set_ylabel(f"{label} ({unit})" if name else "", color=color, fontsize=8)
            if pts:
                lo, hi = min(q[1] for q in pts), max(q[1] for q in pts)
                pad = (hi - lo) * 0.08 or max(abs(hi) * 0.05, 1e-12)
                ax.set_ylim(lo - pad, hi + pad)
        self.ax.set_xlim(-span, 0)
        self.canvas.draw_idle()


class CalibrationPanel(QWidget):
    message = pyqtSignal(str, str)          # level, text

    def __init__(self, bridge_getter, setup_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        self._setup = setup_getter
        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        self.monitor = SignalMonitor()
        outer.addWidget(self.monitor, 1)
        box = QGroupBox("Stage positioning")
        self.stage_box = box
        g = QGridLayout(box)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(8)
        self.stage_title = QLabel("")
        self.stage_title.setStyleSheet("color:#a6adc8;")
        g.addWidget(self.stage_title, 0, 0, 1, 6)
        hdr = ["", "Readback", "Jog", "", "Go to", ""]
        for col, text in enumerate(hdr):
            if text:
                lbl = QLabel(text)
                lbl.setStyleSheet("color:#6c7086;font-size:10px;")
                g.addWidget(lbl, 1, col)
        self.pos_lbl: dict[str, QLabel] = {}
        self.goto: dict[str, NoScrollDoubleSpinBox] = {}
        for row, ax in enumerate(("x", "y", "z"), start=2):
            a = QLabel(ax.upper())
            a.setStyleSheet(_AXIS)
            g.addWidget(a, row, 0)
            lbl = QLabel("—")
            lbl.setStyleSheet(_RB)
            self.pos_lbl[ax] = lbl
            g.addWidget(lbl, row, 1)
            minus = QPushButton("◀")
            plus = QPushButton("▶")
            for b, sign in ((minus, -1), (plus, +1)):
                b.setFixedWidth(34)
                b.setToolTip(f"Jog {ax.upper()} by {'−' if sign < 0 else '+'}step")
                b.clicked.connect(lambda _=False, a=ax, s=sign: self._jog(a, s))
            g.addWidget(minus, row, 2)
            g.addWidget(plus, row, 3)
            sp = NoScrollDoubleSpinBox()
            sp.setDecimals(5)
            sp.setRange(-100, 100)
            sp.setSuffix(" mm")
            sp.setMinimumWidth(100)
            self.goto[ax] = sp
            g.addWidget(sp, row, 4)
            b_go = QPushButton("Go")
            b_go.setFixedWidth(44)
            b_go.clicked.connect(lambda _=False, a=ax: self._goto([a]))
            g.addWidget(b_go, row, 5)
        g.addWidget(QLabel("Jog step:"), 5, 0)
        self.step = NoScrollComboBox()
        for s in JOG_STEPS_MM:
            self.step.addItem(f"{s:g} mm", s)
        self.step.setCurrentIndex(3)
        g.addWidget(self.step, 5, 1)
        btns = QHBoxLayout()
        b_read = QPushButton("⟳ Read all")
        b_read.clicked.connect(self.refresh_positions)
        b_goall = QPushButton("Go to all")
        b_goall.clicked.connect(lambda: self._goto(["x", "y", "z"]))
        b_zero = QPushButton("Set zero here")
        b_zero.setToolTip("MiniMoke 'home': the current position becomes the software zero")
        b_zero.clicked.connect(self._set_zero)
        b_stop = QPushButton("■ STOP")
        b_stop.setObjectName("abort_btn")
        b_stop.clicked.connect(lambda: self._cmd(self._stage(), "stop", quiet=True))
        for b in (b_read, b_goall, b_zero, b_stop):
            btns.addWidget(b)
        g.addLayout(btns, 6, 0, 1, 6)
        note = QLabel("Moves need control of the setup. Positions are relative to the software zero "
                      "(MiniMoke's offsets).")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6c7086;font-size:10px;")
        g.addWidget(note, 7, 0, 1, 6)
        g.setRowStretch(8, 1)
        right = QVBoxLayout()
        right.addWidget(box)
        right.addStretch(1)
        outer.addLayout(right, 0)
        self.update_setup()

    # ------------------------------------------------------------------ live signal
    def add_readback(self, dev: str, attr: str, value) -> None:
        if (dev, attr) in READBACK_CHANNELS:
            self.monitor.add(READBACK_CHANNELS[(dev, attr)], value)
        elif dev == "lockin" and attr == "xy" and isinstance(value, dict):
            for k, name in LOCKIN_CHANNELS.items():
                self.monitor.add(name, value.get(k))

    def add_record(self, rec: dict) -> None:
        """One measured point (backend record keys, see core.scan.channels)."""
        t = rec.get("t_trigger") or None
        for ch in SENSORS:
            if ch.key in rec:
                self.monitor.add(ch.name, rec[ch.key], t)

    def set_stage_enabled(self, enabled: bool) -> None:
        """During a measurement the stage belongs to the run; the plot stays live."""
        self.stage_box.setEnabled(enabled)

    # ------------------------------------------------------------------
    def _stage(self) -> str:
        return f"stage_{self._setup()}"

    def update_setup(self) -> None:
        self.stage_title.setText(STAGE_TITLES.get(self._setup(), self._setup()))
        for lbl in self.pos_lbl.values():
            lbl.setText("—")

    def _client(self):
        b = self._bridge()
        return b.client if b else None

    def _cmd(self, device: str, command: str, quiet: bool = False, **args) -> None:
        c = self._client()
        if c is None:
            return

        def ok(_res):
            if not quiet:
                self.message.emit("info", f"{device}.{command}: done")
            self.refresh_positions()

        call_async(lambda: c.device(device).command(command, **args), ok,
                   lambda e: self.message.emit("error", f"{device}.{command}: {error_text(e)}"))

    def _jog(self, axis: str, sign: int) -> None:
        self._cmd(self._stage(), "jog", quiet=True, axis=axis, delta_mm=sign * float(self.step.currentData()))

    def _goto(self, axes: list[str]) -> None:
        c = self._client()
        if c is None:
            return
        st = self._stage()
        targets = {a: self.goto[a].value() for a in axes}

        def go():
            return {a: c.device(st).command("move_to", axis=a, position_mm=v) for a, v in targets.items()}
        call_async(go, lambda r: self.refresh_positions(),
                   lambda e: self.message.emit("error", f"move: {error_text(e)}"))

    def _set_zero(self) -> None:
        if QMessageBox.question(self, "Set zero", "Make the current position the software zero of all axes?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        self._cmd(self._stage(), "set_zero", axis="all")

    def refresh_positions(self) -> None:
        c = self._client()
        if c is None or not c.session:
            return
        st = self._stage()
        call_async(lambda: c.device(st).read("x_mm", "y_mm", "z_mm"), self._show_positions, lambda e: None)

    def _show_positions(self, readings) -> None:
        for ax in ("x", "y", "z"):
            r = readings.get(f"{ax}_mm")
            self.set_position(ax, r.value if r is not None else None, r.quality.value if r else "")

    def set_position(self, axis: str, value, quality: str = "") -> None:
        lbl = self.pos_lbl.get(axis)
        if lbl is None:
            return
        if value is None or value != value:
            lbl.setText("—")
            return
        lbl.setText(f"{value:+.5f} mm")
        sim = quality == "SIMULATED"
        lbl.setStyleSheet(_RB.replace("#a6e3a1", "#fab387") if sim else _RB)
        lbl.setToolTip("simulated value" if sim else "")
