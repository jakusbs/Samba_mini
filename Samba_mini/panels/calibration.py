"""Calibration tab (next to 2D Map / 1D Plot, as in Samba): its own plot on the
left, stage positioning and the calibration time scan's settings on the right.

As in Samba, pressing Start while this tab is open runs a time scan with the
tab's own settings and plots it here; nothing is measured otherwise (Samba
has autofocus here too, which MiniMoke does not have).  Stage: Samba's digit jog (a ▲
and ▼ for every digit) in µm like the scan ranges (10 mm … 10 nm), go-to, STOP and
"set zero here"; "Alignment…" opens the same stage in mm (10 mm … 1 µm) for coarse
moves.

Every action goes through the device service and needs control of the setup
(the lease); the service rejects it otherwise.  Slow calls run off the GUI
thread.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QVBoxLayout, QWidget)

from core.gui.bridge import call_guarded, error_text
from core.scan.channels import SENSORS
from Samba_mini.panels.widgets import NoScrollComboBox, NoScrollDoubleSpinBox, NoScrollSpinBox
STAGE_TITLES = {"longitudinal": "Longitudinal stage · Thorlabs Kinesis",
                "polar": "Polar stage · Trinamic TMCM-3212"}



class ScanPlot(QWidget):
    """The Calibration tab's plot: the channels of the calibration time scan
    (Y1 left, Y2 right) against the time since it started.  It only shows
    data from a scan started with Start while this tab is open.  The Y1 / Y2
    choice is remembered per account and setup (Samba_mini's working state)."""
    selection_changed = pyqtSignal()
    DEFAULT_SELECTION = {"y1": "dc_signal", "y2": "intensity"}

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
            combo.currentIndexChanged.connect(lambda _i: self.selection_changed.emit())
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

    def set_channels(self, table: list[dict]) -> None:
        """Show the channel names of the running service in the Y1/Y2 lists."""
        names = {r["id"]: r for r in table}
        for combo in (self.y1, self.y2):
            cur = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("—", "")
            for ch in SENSORS:
                r = names.get(ch.name, {})
                combo.addItem(f"{r.get('name', ch.label)} ({ch.unit})", ch.name)
            combo.setCurrentIndex(max(combo.findData(cur), 0))
            combo.blockSignals(False)
        self.units = {ch.name: (names.get(ch.name, {}).get("name", ch.label), ch.unit) for ch in SENSORS}
        self._redraw(force=True)

    def selection(self) -> dict:
        """The channel ids shown on Y1 and Y2 ("" = none)."""
        return {"y1": self.y1.currentData() or "", "y2": self.y2.currentData() or ""}

    def set_selection(self, sel: dict | None) -> None:
        """Show a remembered choice; anything missing or unknown gets the default (BD / Intensity)."""
        sel = sel if isinstance(sel, dict) else {}
        for key, combo in (("y1", self.y1), ("y2", self.y2)):
            want = sel.get(key, self.DEFAULT_SELECTION[key])
            i = combo.findData(want if isinstance(want, str) else None)
            if i < 0:
                i = max(combo.findData(self.DEFAULT_SELECTION[key]), 0)
            combo.blockSignals(True)
            combo.setCurrentIndex(i)
            combo.blockSignals(False)
        self._redraw(force=True)

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
        t_max = 0.0
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
                t_max = max(t_max, pts[-1][0])
        # The time axis follows the recorded data (it grows during the scan), not the planned
        # duration: a long scan would otherwise squeeze the first minutes into a sliver.
        self.ax.set_xlim(0, t_max * 1.03 if t_max > 0 else 1.0)
        self.canvas.draw_idle()

    def x_limits(self) -> tuple[float, float]:
        return tuple(self.ax.get_xlim())


class TimeScanBox(QGroupBox):
    """The calibration time scan's own settings (Samba: "Time scan (this tab's own config)"):
    Points, Int and Settle.  The scan takes its samples back to back, as in Samba, and uses the
    coil and the lock-in as they are set (Hardware box: Set / Apply now); it changes neither."""
    changed = pyqtSignal()

    DEFAULTS = {"n": 300, "acq_time": 0.1, "settle_time": 1.0}

    def __init__(self, parent=None):
        super().__init__("Calibration scan — press Start in this tab", parent)
        g = QGridLayout(self)
        g.setSpacing(4)
        g.setContentsMargins(6, 4, 6, 4)
        self.n = NoScrollSpinBox()
        self.n.setRange(1, 1_000_000)
        self.acq = NoScrollDoubleSpinBox()
        self.acq.setRange(1e-4, 600.0)
        self.acq.setDecimals(4)
        self.acq.setSuffix(" s")
        self.settle = NoScrollDoubleSpinBox()
        self.settle.setRange(0.0, 60.0)
        self.settle.setDecimals(2)
        self.settle.setSuffix(" s")
        col = 0
        for label, w, tip in (("Points:", self.n, "Number of samples, taken one after another"),
                              ("Int:", self.acq, "Averaging time of each sample"),
                              ("Settle:", self.settle, "Wait before the first sample, e.g. after moving the stage")):
            w.setToolTip(tip)
            w.setKeyboardTracking(False)
            w.setMinimumWidth(78)
            g.addWidget(QLabel(label), 0, col)
            g.addWidget(w, 0, col + 1)
            col += 2
        self.total = QLabel("")
        self.total.setStyleSheet("color:#6c7086;font-size:10px;")
        self.total.setToolTip("Settle + Points × Int (each sample adds a few ms on top)")
        g.addWidget(self.total, 1, 0, 1, 6)
        self.applied = QLabel("Uses the coil and the lock-in as they are set (Hardware box: Set / Apply now) "
                              "and leaves them so; the stage does not move.")
        self.applied.setWordWrap(True)
        self.applied.setStyleSheet("color:#6c7086;font-size:10px;")
        g.addWidget(self.applied, 2, 0, 1, 6)
        self.setToolTip("Press Start while this tab is open to run this time scan with exactly these settings.\n"
                        "Saved as ..._calibration.h5; when it completes it is fitted in the BD Calibration tab "
                        "for review.")
        for w in (self.n, self.acq, self.settle):
            w.valueChanged.connect(self._update_total)
            w.valueChanged.connect(self.changed.emit)
        self.reset()

    def _update_total(self, *_):
        secs = self.settle.value() + self.n.value() * self.acq.value()
        self.total.setText(f"≈ {secs:.1f} s" if secs < 120 else f"≈ {secs / 60:.1f} min")

    def values(self) -> dict:
        return {"n": self.n.value(), "acq_time": self.acq.value(), "settle_time": self.settle.value()}

    def load(self, v: dict) -> None:
        """Earlier versions also kept an interval, the coil and lock-in settings here: ignored."""
        for key, w in (("n", self.n), ("acq_time", self.acq), ("settle_time", self.settle)):
            if key in (v or {}):
                w.blockSignals(True)
                try:
                    w.setValue(type(w.value())(v[key]))
                except (TypeError, ValueError):
                    pass
                w.blockSignals(False)
        self._update_total()
        self.changed.emit()

    def reset(self) -> None:
        self.load(self.DEFAULTS)


class DigitJog(QWidget):
    """One axis of Samba's digit jog (``core/calibration.py`` DigitJogWidget).

    A ▲ above and a ▼ below every digit of the position: a click jogs the axis by one unit of
    that digit.  The Calibration tab shows µm (10 mm down to 10 nm); the Alignment window shows
    mm (10 mm down to 1 µm) for coarse moves.  Leading zeros are dimmed so the number stands
    out.  As in MiniMoke the digits are the live position and a click is a relative jog, so it
    can never send a stale absolute target; the stage worker runs repeated clicks one after
    another.  As in Samba nothing can be clicked until a position has been read.  Typing a value
    (in the shown unit) and pressing Enter moves the axis there.  Positions are kept in mm (the
    service's unit) whatever is shown.
    """
    jog = pyqtSignal(str, float)          # axis, delta in mm
    move = pyqtSignal(str, float)         # axis, absolute target in mm

    _BTN_UP = ("QPushButton{background:#313244;color:#89b4fa;border:1px solid #45475a;border-radius:2px;"
               "font-size:10px;font-weight:bold;padding:0;}QPushButton:hover{background:#45475a;}"
               "QPushButton:pressed{background:#585b70;}QPushButton:disabled{color:#45475a;}")
    _BTN_DOWN = _BTN_UP.replace("#89b4fa", "#f38ba8")
    _DIGIT = ("QLabel{color:%s;font-family:'Courier New',monospace;font-size:%dpx;font-weight:bold;"
              "background:#181825;border:1px solid #313244;border-radius:3px;padding:1px 1px;}")
    _EDIT = ("QLineEdit{background:#181825;border:1px solid #45475a;border-radius:4px;color:#cdd6f4;"
             "font-family:'Courier New',monospace;font-size:13px;font-weight:bold;padding:3px 6px;}"
             "QLineEdit:focus{border:1px solid #89b4fa;}QLineEdit:disabled{color:#6c7086;}")
    _DIM = "#585b70"

    def __init__(self, axis: str, unit: str = "µm", n_int: int = 5, n_dec: int = 2, size: int = 18,
                 parent=None):
        super().__init__(parent)
        self.axis = axis
        self.unit = unit
        self.per_mm = 1000.0 if unit == "µm" else 1.0      # shown units per mm
        self.N_INT, self.N_DEC = n_int, n_dec
        self._font = size
        self._color = "#cdd6f4"
        self._pos: float | None = None                    # mm
        w, h = size + 4, size + 10                        # digit box
        outer = QHBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(10)
        name = QLabel(axis.upper())
        name.setStyleSheet(f"color:#89b4fa;font-size:{size}px;font-weight:bold;")
        name.setFixedWidth(size)
        outer.addWidget(name)
        grid = QGridLayout()
        grid.setHorizontalSpacing(2)
        grid.setVerticalSpacing(2)
        grid.setContentsMargins(0, 0, 0, 0)
        self._sign = QLabel("?")
        self._sign.setFixedSize(w, h)
        self._sign.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self._sign, 1, 0)
        self._digits: list[QLabel] = []
        self._buttons: list[QPushButton] = []
        col = 1
        for i in range(n_int + n_dec):
            if i == n_int:
                dot = QLabel(".")
                dot.setFixedWidth(8)
                dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
                dot.setStyleSheet(f"color:#6c7086;font-size:{size}px;font-weight:bold;")
                grid.addWidget(dot, 1, col)
                col += 1
            step = 10.0 ** (n_int - 1 - i)                # in the shown unit
            for row, sign, style, text in ((0, +1, self._BTN_UP, "▲"), (2, -1, self._BTN_DOWN, "▼")):
                b = QPushButton(text)
                b.setFixedSize(w, max(13, size - 4))
                b.setStyleSheet(style)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setToolTip(f"{axis.upper()} {'+' if sign > 0 else '−'}{step:g} {unit}")
                b.clicked.connect(lambda _=False, d=sign * step / self.per_mm: self._click(d))
                grid.addWidget(b, row, col, Qt.AlignmentFlag.AlignCenter)
                self._buttons.append(b)
            d = QLabel("-")
            d.setFixedSize(w, h)
            d.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(d, 1, col)
            self._digits.append(d)
            col += 1
        u = QLabel(unit)
        u.setStyleSheet("color:#a6adc8;font-size:12px;")
        grid.addWidget(u, 1, col)
        outer.addLayout(grid)
        right = QVBoxLayout()
        right.setSpacing(2)
        row = QHBoxLayout()
        lbl = QLabel("Go to:")
        lbl.setStyleSheet("color:#6c7086;font-size:10px;")
        self.edit = QLineEdit("")
        self.edit.setStyleSheet(self._EDIT)
        self.edit.setFixedWidth(110)
        self.edit.setToolTip(f"Type a position in {unit} and press Enter to move {axis.upper()} there")
        self.edit.returnPressed.connect(self._enter)
        row.addWidget(lbl)
        row.addWidget(self.edit)
        row.addWidget(QLabel(unit))
        right.addLayout(row)
        self.hint = QLabel("")
        self.hint.setStyleSheet("color:#6c7086;font-size:10px;")
        right.addWidget(self.hint)
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
        self.move.emit(self.axis, target / self.per_mm)

    def set_unknown(self) -> None:
        self._pos = None
        self._apply()

    def set_position(self, value_mm, simulated: bool = False) -> None:
        try:
            v = float(value_mm)
        except (TypeError, ValueError):
            v = math.nan
        self._pos = v if math.isfinite(v) else None
        self._color = "#fab387" if simulated else "#cdd6f4"
        self.setToolTip("simulated position" if simulated else "")
        self._apply()

    def _style(self, lbl: QLabel, color: str) -> None:
        lbl.setStyleSheet(self._DIGIT % (color, self._font))

    def _apply(self) -> None:
        known = self._pos is not None
        for b in self._buttons:
            b.setEnabled(known)
        self.edit.setEnabled(known)
        if not known:
            self._sign.setText("?")
            self._style(self._sign, self._color)
            for d in self._digits:
                d.setText("-")
                self._style(d, self._color)
            self.hint.setText("position unknown — Read all")
            return
        v = round(self._pos * self.per_mm, self.N_DEC)
        n = self.N_INT + self.N_DEC
        text = f"{abs(v):0{n + 1}.{self.N_DEC}f}".replace(".", "")
        if len(text) > n:                                  # beyond the shown digits: no digit display
            text = "~" * n
        self._sign.setText("−" if v < 0 else "+")
        self._style(self._sign, self._color)
        leading = True
        for i, (d, ch) in enumerate(zip(self._digits, text)):
            leading = leading and ch == "0" and i < self.N_INT - 1
            d.setText(ch)
            self._style(d, self._DIM if leading else self._color)
        self.hint.setText(f"= {self._pos:+.5f} mm" if self.unit == "µm" else f"= {self._pos * 1000:+.2f} µm")
        if not self.edit.hasFocus():
            self.edit.setText(f"{v:.{self.N_DEC}f}")


class AlignmentDialog(QDialog):
    """Coarse alignment of the selected stage in mm (10 mm … 1 µm), next to the Calibration
    tab's fine µm jog.  It does exactly what the tab does (the same stage, the same rules: moves
    need control of the setup) and stays open while you work."""

    def __init__(self, panel: "CalibrationPanel"):
        super().__init__(panel)
        self.setModal(False)
        self.setMinimumWidth(560)
        self._panel = panel
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        self.body = QWidget()
        b = QVBoxLayout(self.body)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(12)
        note = QLabel("Coarse moves in mm. Fine positioning in µm is in the Calibration tab.")
        note.setStyleSheet("color:#a6adc8;")
        b.addWidget(note)
        self.jogs: dict[str, DigitJog] = {}
        for ax in ("x", "y", "z"):
            j = DigitJog(ax, unit="mm", n_int=2, n_dec=3, size=24)
            j.jog.connect(panel._jog)
            j.move.connect(lambda a, mm: panel._goto({a: mm}))
            b.addWidget(j)
            self.jogs[ax] = j
        btns = QHBoxLayout()
        b_read = QPushButton("⟳ Read all")
        b_read.clicked.connect(panel.refresh_positions)
        b_zero = QPushButton("Set zero here")
        b_zero.clicked.connect(panel._set_zero)
        self.stop_btn = QPushButton("■ STOP")
        self.stop_btn.setObjectName("abort_btn")
        self.stop_btn.clicked.connect(panel._stop)
        for w in (b_read, b_zero, self.stop_btn):
            btns.addWidget(w)
        b.addLayout(btns)
        lay.addWidget(self.body)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        lay.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

    def set_title(self, title: str) -> None:
        self.setWindowTitle(f"Alignment · {title}")

    def set_enabled(self, enabled: bool) -> None:
        self.body.setEnabled(enabled)


class CalibrationPanel(QWidget):
    message = pyqtSignal(str, str)          # level, text

    def __init__(self, bridge_getter, setup_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        self._setup = setup_getter
        self._gen = 0                           # selection generation (bumped on setup/session change)
        self.align: AlignmentDialog | None = None
        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        self.plot = ScanPlot()
        outer.addWidget(self.plot, 1)
        box = QGroupBox("Stage positioning")
        self.stage_box = box
        v = QVBoxLayout(box)
        v.setSpacing(8)
        v.setContentsMargins(8, 8, 8, 8)
        self.stage_title = QLabel("")
        self.stage_title.setStyleSheet("color:#a6adc8;")
        self.stage_title.setVisible(False)          # the stage name is in the box title
        self.jogs: dict[str, DigitJog] = {}
        for ax in ("x", "y", "z"):
            j = DigitJog(ax, unit="µm", n_int=5, n_dec=2, size=18)
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
        self.align_btn = QPushButton("Alignment…")
        self.align_btn.setToolTip("Open a window for coarse moves in mm")
        self.align_btn.clicked.connect(self.open_alignment)
        b_stop = QPushButton("■ STOP")
        b_stop.setObjectName("abort_btn")
        b_stop.clicked.connect(self._stop)
        for b in (b_read, b_zero, self.align_btn, b_stop):
            btns.addWidget(b)
        v.addLayout(btns)
        box.setToolTip("▲/▼ jog by one unit of that digit (10 mm … 0.01 µm = 10 nm). Moves need control "
                       "of the setup.\nPositions are relative to the software zero (MiniMoke's offsets), "
                       "in µm like the scans. Alignment… opens coarse moves in mm.")
        box.setMinimumWidth(470)
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

    def _title(self) -> str:
        return STAGE_TITLES.get(self._setup(), self._setup())

    def set_stage_enabled(self, enabled: bool, during_run: bool = False) -> None:
        """A run that uses the stage owns it (the controls are greyed, also in the Alignment
        window); one that does not, such as the calibration time scan, leaves it free so it can
        be moved to focus meanwhile."""
        self.stage_box.setEnabled(enabled)
        title = f"Stage positioning · {self._title()}"
        if during_run:
            title += " — free during this scan" if enabled else " — in use by the measurement"
        self.stage_box.setTitle(title)
        if self.align is not None:
            self.align.set_enabled(enabled)

    def open_alignment(self) -> None:
        if self.align is None:
            self.align = AlignmentDialog(self)
            self.align.set_title(self._title())
            for ax, j in self.jogs.items():                 # show what is known right away
                if j._pos is None:
                    self.align.jogs[ax].set_unknown()
                else:
                    self.align.jogs[ax].set_position(j._pos, j._color == "#fab387")
        self.align.set_enabled(self.stage_box.isEnabled())
        self.align.show()
        self.align.raise_()
        self.align.activateWindow()
        self.refresh_positions()

    def _all_jogs(self, axis: str) -> list[DigitJog]:
        jogs = [self.jogs[axis]] if axis in self.jogs else []
        if self.align is not None and axis in self.align.jogs:
            jogs.append(self.align.jogs[axis])
        return jogs

    # ------------------------------------------------------------------
    def _stage(self) -> str:
        return f"stage_{self._setup()}"

    def update_setup(self) -> None:
        """The selected setup (stage) changed: forget the shown positions and ignore every
        position reply still on its way for the previous stage."""
        self._gen += 1
        self.stage_title.setText(self._title())
        self.stage_box.setTitle(f"Stage positioning · {self._title()}")
        if self.align is not None:
            self.align.set_title(self._title())
        for ax in ("x", "y", "z"):
            for j in self._all_jogs(ax):
                j.set_unknown()                  # the other stage's position must not be jogged from

    def reset_session(self) -> None:
        """Login, logout or session expiry: nothing shown belongs to the new session yet."""
        self.update_setup()
        self.timescan.reset()
        self.plot.set_selection(None)
        self.plot.begin(0.0, "Press Start while this tab is open to run a calibration time scan")

    def _client(self):
        b = self._bridge()
        return b.client if b else None

    def _current(self, gen: int, stage: str):
        """Still the same selection (setup) as when a request was sent?"""
        return lambda: gen == self._gen and stage == self._stage()

    def _cmd(self, device: str, command: str, quiet: bool = False, **args) -> None:
        c = self._client()
        if c is None:
            return

        def ok(_res):
            if not quiet:
                self.message.emit("info", f"{device}.{command}: done")
            self.refresh_positions()

        # Errors name the device, so they stay meaningful after a setup switch; a reply from an
        # earlier login session is dropped (call_guarded).
        call_guarded(c, lambda: c.device(device).command(command, **args), ok,
                     lambda e: self.message.emit("error", f"{device}.{command}: {error_text(e)}"))

    def _stop(self) -> None:
        self._cmd(self._stage(), "stop", quiet=True)

    def _jog(self, axis: str, delta_mm: float) -> None:
        self._cmd(self._stage(), "jog", quiet=True, axis=axis, delta_mm=delta_mm)

    def _goto(self, targets: dict) -> None:
        c = self._client()
        if c is None:
            return
        st = self._stage()

        def go():
            return {a: c.device(st).command("move_to", axis=a, position_mm=v) for a, v in targets.items()}
        call_guarded(c, go, lambda r: self.refresh_positions(),
                     lambda e: self.message.emit("error", f"{st} move: {error_text(e)}"))

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
        valid = self._current(self._gen, st)

        def failed(_e):
            for ax in ("x", "y", "z"):
                for j in self._all_jogs(ax):
                    j.set_unknown()
        call_guarded(c, lambda: c.device(st).read("x_mm", "y_mm", "z_mm"), self._show_positions,
                     failed, still_valid=valid)

    def _show_positions(self, readings) -> None:
        for ax in ("x", "y", "z"):
            r = readings.get(f"{ax}_mm")
            self.set_position(ax, r.value if r is not None else None, r.quality.value if r else "")

    def set_position(self, axis: str, value, quality: str = "") -> None:
        for j in self._all_jogs(axis):
            if quality == "INVALID":
                j.set_unknown()                  # no reading: nothing to jog from
            else:
                j.set_position(value, quality == "SIMULATED")
