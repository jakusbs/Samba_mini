"""Calibration tab (next to 2D Map / 1D Plot, as in Samba): its own plot on the
left, stage positioning and the calibration time scan's settings on the right.

As in Samba, pressing Start while this tab is open runs a time scan with the
tab's own settings and plots it here; nothing is measured otherwise (Samba
has autofocus here too, which MiniMoke does not have).  Stage: Samba's digit jog (a ▲
and ▼ for every digit, MiniMoke's odometer steps 10 mm … 0.00001 mm), go-to,
STOP and "set zero here".  Stage positions are in mm like MiniMoke's motor
tabs; scan ranges in the Trajectory tab are in µm like MiniMoke's procedures.

Every action goes through the device service and needs control of the setup
(the lease); the service rejects it otherwise.  Slow calls run off the GUI
thread.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QVBoxLayout, QWidget)

from core.gui.bridge import call_async, error_text
from core.scan.channels import SENSORS
from Samba_mini.panels.widgets import NoScrollComboBox, NoScrollDoubleSpinBox, NoScrollSpinBox
STAGE_TITLES = {"longitudinal": "Longitudinal stage · Thorlabs Kinesis",
                "polar": "Polar stage · Trinamic TMCM-3212"}



class ScanPlot(QWidget):
    """The Calibration tab's plot: the channels of the calibration time scan
    (Y1 left, Y2 right) against the time since it started.  It only shows
    data from a scan started with Start while this tab is open."""

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
        for w, label, color in ((self.y1, "Y1:", "#89b4fa"), (self.y2, "Y2:", "#f38ba8")):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{color};")
            row.addWidget(lbl)
            row.addWidget(w, 1)
        lay.addLayout(row)
        self.fig = Figure(facecolor="#1e1e2e")
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.ax2 = self.ax.twinx()
        self.fig.subplots_adjust(left=0.15, right=0.85, top=0.93, bottom=0.17)
        for ax in (self.ax, self.ax2):
            ax.set_facecolor("#12121f")
            for sp in ax.spines.values():
                sp.set_color("#45475a")
            ax.tick_params(colors="#a6adc8", labelsize=8)
            try:
                eng_axis(ax.yaxis)
            except Exception:  # noqa: BLE001 - cosmetic
                pass
        self.ax.set_xlabel("Time (s)", color="#a6adc8", fontsize=8)
        self.ax.set_title("Press Start while this tab is open to run a calibration time scan",
                          color="#6c7086", fontsize=8)
        self.l1, = self.ax.plot([], [], color="#89b4fa", lw=1.3, marker=".", ms=3)
        self.l2, = self.ax2.plot([], [], color="#f38ba8", lw=1.3, marker=".", ms=3)
        self.canvas.setMinimumWidth(220)
        lay.addWidget(self.canvas, 1)
        self.data: dict[str, list] = {ch.name: [] for ch in SENSORS}
        self.units = {ch.name: (ch.label, ch.unit) for ch in SENSORS}
        self._span = 0.0
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._redraw)
        self._timer.start()

    def begin(self, duration_s: float, title: str = "") -> None:
        for d in self.data.values():
            d.clear()
        self._span = float(duration_s)
        self.ax.set_title(title, color="#a6adc8", fontsize=8)
        self._redraw(force=True)

    def add(self, t: float, rec: dict) -> None:
        """One point of the calibration scan (backend record keys, see core.scan.channels)."""
        for ch in SENSORS:
            try:
                v = float(rec.get(ch.key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(v) and math.isfinite(t):
                self.data[ch.name].append((t, v))
                self._dirty = True

    def _redraw(self, force: bool = False) -> None:
        if not (self._dirty or force) or not self.isVisible():
            return
        self._dirty = False
        for combo, line, ax, color in ((self.y1, self.l1, self.ax, "#89b4fa"),
                                       (self.y2, self.l2, self.ax2, "#f38ba8")):
            name = combo.currentData()
            pts = self.data.get(name, []) if name else []
            line.set_data([q[0] for q in pts], [q[1] for q in pts])
            label, unit = self.units.get(name, ("", ""))
            ax.set_ylabel(f"{label} ({unit})" if name else "", color=color, fontsize=8)
            if pts:
                lo, hi = min(q[1] for q in pts), max(q[1] for q in pts)
                pad = (hi - lo) * 0.08 or max(abs(hi) * 0.05, 1e-12)
                ax.set_ylim(lo - pad, hi + pad)
        self.ax.set_xlim(0, max(self._span, 1e-3))
        self.canvas.draw_idle()


class TimeScanBox(QGroupBox):
    """The calibration time scan's own settings (Samba: "Time scan (this tab's own config)")."""

    def __init__(self, parent=None):
        super().__init__("Time scan — press Start in this tab", parent)
        g = QGridLayout(self)
        g.setSpacing(4)
        self.n = NoScrollSpinBox()
        self.n.setRange(1, 1_000_000)
        self.n.setValue(300)
        self.interval = NoScrollDoubleSpinBox()
        self.interval.setRange(1e-3, 3600.0)
        self.interval.setDecimals(3)
        self.interval.setValue(0.2)
        self.interval.setSuffix(" s")
        self.acq = NoScrollDoubleSpinBox()
        self.acq.setRange(1e-4, 600.0)
        self.acq.setDecimals(4)
        self.acq.setValue(0.1)
        self.acq.setSuffix(" s")
        g.setContentsMargins(6, 4, 6, 4)
        col = 0
        for label, w, tip in (("N:", self.n, "Number of samples"),
                              ("Interval:", self.interval, "Time from one sample to the next"),
                              ("Int:", self.acq, "Averaging time of each sample")):
            w.setToolTip(tip)
            w.setKeyboardTracking(False)
            w.setMinimumWidth(70)
            g.addWidget(QLabel(label), 0, col)
            g.addWidget(w, 0, col + 1)
            col += 2
        self.total = QLabel("")
        self.total.setStyleSheet("color:#6c7086;font-size:10px;")
        g.addWidget(self.total, 0, col)
        self.setToolTip("Press Start while this tab is open to run this time scan.\n"
                        "Lock-in and coil settings come from the Trajectory tab's time scan; the stage does "
                        "not move.\nSaved as ..._calibration.h5, which the BD Calibration tab's Fit & Import "
                        "picks up.")
        for w in (self.n, self.interval):
            w.valueChanged.connect(self._update_total)
        self._update_total()

    def _update_total(self, *_):
        self.total.setText(f"= {self.n.value() * self.interval.value():.1f} s")

    def values(self) -> dict:
        return {"n": self.n.value(), "interval": self.interval.value(), "acq_time": self.acq.value()}

    def load(self, v: dict) -> None:
        for key, w in (("n", self.n), ("interval", self.interval), ("acq_time", self.acq)):
            if key in (v or {}):
                try:
                    w.setValue(type(w.value())(v[key]))
                except (TypeError, ValueError):
                    pass


class DigitJog(QWidget):
    """One axis of Samba's digit jog (``core/calibration.py`` DigitJogWidget), in MiniMoke's units.

    A ▲ above and a ▼ below every digit of the position: a click jogs the axis
    by one unit of that digit, from 10 mm down to 0.00001 mm (MiniMoke's
    odometer steps).  As in MiniMoke the digits are the live position, and a
    click is a relative jog, so it can never send a stale absolute target; the
    stage worker runs repeated clicks one after another.  As in Samba nothing
    can be clicked until a position has been read.  Typing a value and pressing
    Enter moves the axis there.
    """
    jog = pyqtSignal(str, float)          # axis, delta in mm
    move = pyqtSignal(str, float)         # axis, absolute target in mm

    N_INT, N_DEC = 2, 5

    _BTN_UP = ("QPushButton{background:#313244;color:#89b4fa;border:1px solid #45475a;border-radius:2px;"
               "font-size:10px;font-weight:bold;padding:0;}QPushButton:hover{background:#45475a;}"
               "QPushButton:pressed{background:#585b70;}QPushButton:disabled{color:#45475a;}")
    _BTN_DOWN = _BTN_UP.replace("#89b4fa", "#f38ba8")
    _DIGIT = ("QLabel{color:#cdd6f4;font-family:'Courier New',monospace;font-size:16px;font-weight:bold;"
              "background:#181825;border:1px solid #313244;border-radius:3px;padding:2px 2px;}")
    _EDIT = ("QLineEdit{background:#181825;border:1px solid #45475a;border-radius:4px;color:#cdd6f4;"
             "font-family:'Courier New',monospace;font-size:13px;font-weight:bold;padding:3px 6px;}"
             "QLineEdit:focus{border:1px solid #89b4fa;}QLineEdit:disabled{color:#6c7086;}")

    def __init__(self, axis: str, parent=None):
        super().__init__(parent)
        self.axis = axis
        self._pos: float | None = None
        outer = QHBoxLayout(self)
        outer.setContentsMargins(2, 0, 2, 0)
        outer.setSpacing(8)
        name = QLabel(axis.upper())
        name.setStyleSheet("color:#89b4fa;font-size:16px;font-weight:bold;")
        name.setFixedWidth(16)
        outer.addWidget(name)
        grid = QGridLayout()
        grid.setSpacing(1)
        grid.setContentsMargins(0, 0, 0, 0)
        self._sign = QLabel("?")
        self._sign.setFixedSize(20, 22)
        self._sign.setStyleSheet(self._DIGIT)
        self._sign.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self._sign, 1, 0)
        self._digits: list[QLabel] = []
        self._buttons: list[QPushButton] = []
        col = 1
        for i in range(self.N_INT + self.N_DEC):
            if i == self.N_INT:
                dot = QLabel(".")
                dot.setFixedWidth(8)
                dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
                dot.setStyleSheet("color:#6c7086;font-size:16px;font-weight:bold;")
                grid.addWidget(dot, 1, col)
                col += 1
            step = 10.0 ** (self.N_INT - 1 - i)
            for row, sign, style, text in ((0, +1, self._BTN_UP, "▲"), (2, -1, self._BTN_DOWN, "▼")):
                b = QPushButton(text)
                b.setFixedSize(20, 13)
                b.setStyleSheet(style)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setToolTip(f"{axis.upper()} {'+' if sign > 0 else '−'}{step:g} mm")
                b.clicked.connect(lambda _=False, d=sign * step: self._click(d))
                grid.addWidget(b, row, col, Qt.AlignmentFlag.AlignCenter)
                self._buttons.append(b)
            d = QLabel("-")
            d.setFixedSize(20, 22)
            d.setAlignment(Qt.AlignmentFlag.AlignCenter)
            d.setStyleSheet(self._DIGIT)
            grid.addWidget(d, 1, col)
            self._digits.append(d)
            col += 1
        unit = QLabel("mm")
        unit.setStyleSheet("color:#6c7086;font-size:11px;")
        grid.addWidget(unit, 1, col)
        outer.addLayout(grid)
        right = QVBoxLayout()
        right.setSpacing(2)
        row = QHBoxLayout()
        lbl = QLabel("Go to:")
        lbl.setStyleSheet("color:#6c7086;font-size:10px;")
        self.edit = QLineEdit("")
        self.edit.setStyleSheet(self._EDIT)
        self.edit.setFixedWidth(100)
        self.edit.setToolTip(f"Type a position in mm and press Enter to move {axis.upper()} there")
        self.edit.returnPressed.connect(self._enter)
        row.addWidget(lbl)
        row.addWidget(self.edit)
        row.addWidget(QLabel("mm"))
        right.addLayout(row)
        self.um = QLabel("")
        self.um.setStyleSheet("color:#6c7086;font-size:10px;")
        right.addWidget(self.um)
        outer.addLayout(right)
        outer.addStretch()
        self._apply()

    def _click(self, delta_mm: float) -> None:
        if self._pos is not None:
            self.jog.emit(self.axis, delta_mm)

    def _enter(self) -> None:
        if self._pos is None:
            return
        try:
            target = float(self.edit.text().replace(",", ".").strip())
        except ValueError:
            return
        self.move.emit(self.axis, target)

    def set_unknown(self) -> None:
        self._pos = None
        self._apply()

    def set_position(self, value_mm, simulated: bool = False) -> None:
        try:
            v = float(value_mm)
        except (TypeError, ValueError):
            v = math.nan
        self._pos = v if math.isfinite(v) else None
        color = "#fab387" if simulated else "#cdd6f4"
        for lbl in self._digits + [self._sign]:
            lbl.setStyleSheet(self._DIGIT.replace("#cdd6f4", color))
        self.setToolTip("simulated position" if simulated else "")
        self._apply()

    def _apply(self) -> None:
        known = self._pos is not None
        for b in self._buttons:
            b.setEnabled(known)
        self.edit.setEnabled(known)
        if not known:
            self._sign.setText("?")
            for d in self._digits:
                d.setText("-")
            self.um.setText("position unknown — Read all")
            return
        v = round(self._pos, self.N_DEC)
        text = f"{abs(v):0{self.N_INT + self.N_DEC + 1}.{self.N_DEC}f}".replace(".", "")
        if len(text) > self.N_INT + self.N_DEC:              # beyond ±99.99999 mm: no digit display
            text = "~" * (self.N_INT + self.N_DEC)
        self._sign.setText("−" if v < 0 else "+")
        for d, ch in zip(self._digits, text):
            d.setText(ch)
        self.um.setText(f"= {v * 1000:+.2f} µm")
        if not self.edit.hasFocus():
            self.edit.setText(f"{v:.{self.N_DEC}f}")


class CalibrationPanel(QWidget):
    message = pyqtSignal(str, str)          # level, text

    def __init__(self, bridge_getter, setup_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        self._setup = setup_getter
        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        self.plot = ScanPlot()
        outer.addWidget(self.plot, 1)
        box = QGroupBox("Stage positioning")
        self.stage_box = box
        v = QVBoxLayout(box)
        v.setSpacing(1)
        self.stage_title = QLabel("")
        self.stage_title.setStyleSheet("color:#a6adc8;")
        self.stage_title.setVisible(False)          # the stage name is in the box title
        self.jogs: dict[str, DigitJog] = {}
        for ax in ("x", "y", "z"):
            j = DigitJog(ax)
            j.jog.connect(self._jog)
            j.move.connect(lambda a, mm: self._goto({a: mm}))
            v.addWidget(j)
            self.jogs[ax] = j
        btns = QHBoxLayout()
        b_read = QPushButton("⟳ Read all")
        b_read.clicked.connect(self.refresh_positions)
        b_zero = QPushButton("Set zero here")
        b_zero.setToolTip("MiniMoke 'home': the current position becomes the software zero")
        b_zero.clicked.connect(self._set_zero)
        b_stop = QPushButton("■ STOP")
        b_stop.setObjectName("abort_btn")
        b_stop.clicked.connect(lambda: self._cmd(self._stage(), "stop", quiet=True))
        for b in (b_read, b_zero, b_stop):
            btns.addWidget(b)
        v.addLayout(btns)
        box.setToolTip("▲/▼ jog by one unit of that digit (10 mm … 0.00001 mm = 10 nm). Moves need control "
                       "of the setup.\nPositions are relative to the software zero (MiniMoke's offsets); "
                       "scans use µm.")
        right = QVBoxLayout()
        right.addWidget(box)
        self.timescan = TimeScanBox()
        right.addWidget(self.timescan)
        right.addStretch(1)
        outer.addLayout(right, 0)
        self.update_setup()

    # ------------------------------------------------------------------ calibration time scan
    def begin_scan(self, duration_s: float, title: str) -> None:
        self.plot.begin(duration_s, title)

    def add_point(self, t: float, rec: dict) -> None:
        self.plot.add(t, rec)

    def set_stage_enabled(self, enabled: bool) -> None:
        """During a measurement the stage belongs to the run; the plot stays live."""
        self.stage_box.setEnabled(enabled)

    # ------------------------------------------------------------------
    def _stage(self) -> str:
        return f"stage_{self._setup()}"

    def update_setup(self) -> None:
        self.stage_title.setText(STAGE_TITLES.get(self._setup(), self._setup()))
        self.stage_box.setTitle(f"Stage positioning · {STAGE_TITLES.get(self._setup(), self._setup())}")
        for j in self.jogs.values():
            j.set_unknown()                      # the other stage's position must not be jogged from

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

    def _jog(self, axis: str, delta_mm: float) -> None:
        self._cmd(self._stage(), "jog", quiet=True, axis=axis, delta_mm=delta_mm)

    def _goto(self, targets: dict) -> None:
        c = self._client()
        if c is None:
            return
        st = self._stage()

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
        call_async(lambda: c.device(st).read("x_mm", "y_mm", "z_mm"), self._show_positions,
                   lambda e: [j.set_unknown() for j in self.jogs.values()])

    def _show_positions(self, readings) -> None:
        for ax in ("x", "y", "z"):
            r = readings.get(f"{ax}_mm")
            self.set_position(ax, r.value if r is not None else None, r.quality.value if r else "")

    def set_position(self, axis: str, value, quality: str = "") -> None:
        j = self.jogs.get(axis)
        if j is not None:
            j.set_position(value, quality == "SIMULATED")
