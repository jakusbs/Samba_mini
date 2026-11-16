"""Trajectory tab, arranged like Samba's (``Samba_main/panels/trajectory.py``).

* Scan-type pills **Spatial | Field**.
* Spatial: Samba's *X axis* / *Y axis* boxes with "Scan enabled".  X only =
  MiniMoke X-sweep, Y only = Y-sweep, both = XY map, **neither = time scan**
  (as in Samba, where a spatial scan without an axis is a TIME scan; the X box
  then asks for the number of samples).  An axis that is not scanned is held
  at its fixed position.
* Field: Samba's two sub-modes side by side, the inactive one greyed out:
  Samba's "AC Field Sweep" is MiniMoke's Lock-in B-sweep, "DC Hysteresis" is
  the DC B-sweep; Samba's field monitor (Hall probe, last 60 s) sits between.
* Bottom row: Timing | Metadata | Hardware.

The six MiniMoke workflows and their parameters are unchanged
(``core.scan.workflows``): this panel only arranges the widgets.  ``params()``
returns exactly the parameters of the selected workflow and ``set_params``
takes them back, so saved configurations load unchanged.
"""

from __future__ import annotations

import collections
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QRadioButton, QStackedWidget, QVBoxLayout, QWidget)

from core.scan import plans
from core.scan.workflows import WORKFLOWS, Workflow
from Samba_mini.panels.hardware import HardwareGroup
from Samba_mini.panels.metadata import MetadataGroup
from Samba_mini.panels.widgets import (RO_STYLE, NoScrollDoubleSpinBox, NoScrollSpinBox, StepNPair,
                                       segmented_pills)

FIELD_KEYS = ("b_sweep_lockin", "b_sweep_dc")
SPATIAL_KEYS = ("x_sweep", "y_sweep", "xy_sweep", "time")
STAGE_NAMES = {"longitudinal": "Thorlabs Kinesis · stage_longitudinal",
               "polar": "Trinamic TMCM-3212 · stage_polar"}

_ACT = ("QGroupBox{border:1px solid #89b4fa;border-radius:6px;margin-top:9px;padding-top:9px;"
        "font-weight:bold;color:#89b4fa;}QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}")
_IDLE = ("QGroupBox{border:1px solid #45475a;border-radius:6px;margin-top:9px;padding-top:9px;"
         "font-weight:bold;color:#45475a;}QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}")


def _dspin(lo, hi, dec, suffix="", width=None) -> NoScrollDoubleSpinBox:
    sp = NoScrollDoubleSpinBox()
    sp.setRange(lo, hi)
    sp.setDecimals(dec)
    sp.setKeyboardTracking(False)
    if suffix:
        sp.setSuffix(f" {suffix}")
    if width:
        sp.setMinimumWidth(width)
    return sp


def _ispin(lo, hi, width=None) -> NoScrollSpinBox:
    sp = NoScrollSpinBox()
    sp.setRange(lo, hi)
    sp.setKeyboardTracking(False)
    if width:
        sp.setMinimumWidth(width)
    return sp


def _ro(text: str, small: bool = False) -> QLineEdit:
    le = QLineEdit(text)
    le.setReadOnly(True)
    le.setStyleSheet(RO_STYLE + ("font-size:10px;" if small else ""))
    return le


# ============================================================================ spatial
class AxisGroup(QGroupBox):
    """Samba's ActuatorGroup for one stage axis (positions in µm, MiniMoke's unit)."""
    changed = pyqtSignal()
    scan_toggled = pyqtSignal(bool)

    def __init__(self, title: str, axis: str, parent=None):
        super().__init__(title, parent)
        self.axis = axis
        g = QGridLayout(self)
        g.setSpacing(4)
        g.setContentsMargins(8, 8, 8, 8)
        self.scan_cb = QCheckBox("Scan enabled")
        self.scan_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scan_cb.toggled.connect(self._on_toggled)
        row0 = QHBoxLayout()
        row0.setContentsMargins(0, 0, 0, 0)
        row0.addWidget(self.scan_cb)
        row0.addStretch()
        self.slot = row0                      # extra controls on the right of "Scan enabled"
        g.addLayout(row0, 0, 0, 1, 6)
        g.addWidget(QLabel("Device:"), 1, 0)
        self.dev_lbl = _ro("", small=True)
        g.addWidget(self.dev_lbl, 1, 1, 1, 4)
        self.attr_lbl = _ro(f"{axis}", small=True)
        self.attr_lbl.setMinimumWidth(40)
        g.addWidget(self.attr_lbl, 1, 5)
        g.addWidget(QLabel("Label:"), 2, 0)
        g.addWidget(_ro(axis.upper()), 2, 1, 1, 2)
        g.addWidget(QLabel("Unit:"), 2, 3)
        g.addWidget(_ro("µm"), 2, 4, 1, 2)
        g.setColumnStretch(1, 2)
        g.setColumnStretch(4, 2)
        g.addWidget(QLabel("Start:"), 3, 0)
        self.start = _dspin(-1e6, 1e6, 3, "µm")
        g.addWidget(self.start, 3, 1, 1, 2)
        g.addWidget(QLabel("Stop:"), 3, 3)
        self.stop = _dspin(-1e6, 1e6, 3, "µm")
        g.addWidget(self.stop, 3, 4, 1, 2)

        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        self.n_lbl = QLabel("N:")
        self.npts = _ispin(1, 1_000_000)
        self.d_lbl = QLabel(f"Δ{axis}:")
        self.step = _dspin(1e-3, 1e6, 3, "µm")
        self.samples_lbl = QLabel("Samples N:")
        self.samples = _ispin(1, 10_000_000, 80)
        self.samples.setToolTip("Time scan: number of samples (MiniMoke's duration = N × interval)")
        for w in (self.n_lbl, self.npts, self.d_lbl, self.step, self.samples_lbl, self.samples):
            rl.addWidget(w)
        rl.addStretch()
        self.fixed_lbl = QLabel("Fixed at:")
        self.fixed = _dspin(-1e6, 1e6, 3, "µm", 90)
        self.fixed.setToolTip(f"Position of {axis.upper()} while it is not scanned")
        rl.addWidget(self.fixed_lbl)
        rl.addWidget(self.fixed)
        g.addWidget(row, 4, 0, 1, 6)
        self.pair = StepNPair(self.npts, self.step, lambda: self.stop.value() - self.start.value())
        for w in (self.start, self.stop):
            w.valueChanged.connect(self.pair.span_changed)
        for w in (self.start, self.stop, self.step, self.npts, self.fixed, self.samples):
            w.valueChanged.connect(self.changed.emit)
        self.set_time_mode(False)

    def _on_toggled(self, on: bool) -> None:
        self._apply_enabled()
        self.scan_toggled.emit(on)
        self.changed.emit()

    def _apply_enabled(self) -> None:
        on = self.scan_cb.isChecked()
        for w in (self.start, self.stop, self.npts, self.step):
            w.setEnabled(on)
        self.fixed.setEnabled(not on)

    def set_time_mode(self, time_mode: bool) -> None:
        """X box in a time scan: the N row asks for the number of samples."""
        for w in (self.n_lbl, self.npts, self.d_lbl, self.step):
            w.setVisible(not time_mode)
        for w in (self.samples_lbl, self.samples):
            w.setVisible(time_mode)
        self._apply_enabled()

    def set_device(self, text: str) -> None:
        self.dev_lbl.setText(text)
        self.dev_lbl.setToolTip(text)

    def range_values(self) -> tuple[float, float, float]:
        return self.start.value(), self.stop.value(), self.step.value()

    def set_range(self, lo=None, hi=None, step=None) -> None:
        for sp, v in ((self.start, lo), (self.stop, hi), (self.step, step)):
            if v is not None:
                sp.blockSignals(True)
                sp.setValue(float(v))
                sp.blockSignals(False)
        self.pair.span_changed()


# ============================================================================ field
class FieldSweepBox(QGroupBox):
    """One field-sweep sub-mode box (Samba's AC Field Sweep / DC Hysteresis boxes)."""
    changed = pyqtSignal()

    def __init__(self, key: str, title: str, parent=None):
        super().__init__(title, parent)
        self.key = key
        wf = WORKFLOWS[key]
        g = QGridLayout(self)
        g.setSpacing(4)
        g.setContentsMargins(8, 8, 8, 8)
        coil = wf.param("b_min")
        self.b_min = _dspin(coil.min, coil.max, coil.decimals, "V", 96)
        self.b_max = _dspin(coil.min, coil.max, coil.decimals, "V", 96)
        st = wf.param("b_step")
        self.b_step = _dspin(st.min, st.max, st.decimals, "V", 90)
        self.npts = _ispin(1, 10_000_000, 60)
        for w in (self.b_min, self.b_max):
            w.setToolTip("Coil command in V on the DAQ output (MiniMoke labelled it 'A')")
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self.b_min)
        row.addWidget(QLabel("→"))
        row.addWidget(self.b_max)
        row.addWidget(QLabel("N:"))
        row.addWidget(self.npts)
        row.addWidget(QLabel("Δ:"))
        row.addWidget(self.b_step)
        row.addStretch()
        g.addLayout(row, 0, 0, 1, 4)
        sf = wf.param("sweep_freq")
        self.sweep_freq = _dspin(sf.min, sf.max, sf.decimals, "Hz", 90)
        self.sweep_freq.setToolTip(sf.help or "full loops per second")
        g.addWidget(QLabel("Max sweep freq:" if key == "b_sweep_lockin" else "Sweep freq:"), 1, 0)
        g.addWidget(self.sweep_freq, 1, 1)
        ns = wf.param("num_sweeps")
        self.num_sweeps = _ispin(int(ns.min), int(ns.max), 70)
        g.addWidget(QLabel("Sweeps:"), 1, 2)
        g.addWidget(self.num_sweeps, 1, 3)
        self.num_sweeps.setToolTip("Number of sweeps to average")
        self.info = QLabel("")
        self.info.setStyleSheet("color:#6c7086;font-size:10px;")
        g.addWidget(self.info, 2, 0, 1, 4)
        self.pair = StepNPair(self.npts, self.b_step, lambda: self.b_max.value() - self.b_min.value())
        for w in (self.b_min, self.b_max):
            w.valueChanged.connect(self.pair.span_changed)
        for w in (self.b_min, self.b_max, self.b_step, self.npts, self.sweep_freq, self.num_sweeps):
            w.valueChanged.connect(self.changed.emit)

    def values(self) -> dict:
        return {"b_min": self.b_min.value(), "b_max": self.b_max.value(), "b_step": self.b_step.value(),
                "sweep_freq": self.sweep_freq.value(), "num_sweeps": self.num_sweeps.value()}

    def set_values(self, v: dict) -> None:
        for name in ("b_min", "b_max", "b_step", "sweep_freq", "num_sweeps"):
            if name in v:
                sp = getattr(self, name)
                sp.blockSignals(True)
                try:
                    sp.setValue(int(round(float(v[name]))) if name == "num_sweeps" else float(v[name]))
                except (TypeError, ValueError):
                    pass
                sp.blockSignals(False)
        self.pair.span_changed()

    def set_active(self, active: bool) -> None:
        self.setStyleSheet(_ACT if active else _IDLE)
        self.setEnabled(active)


class FieldMonitor(QWidget):
    """Samba's 'Field monitor (last 60 s)' strip, fed by Hall readbacks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.fig = Figure(figsize=(2.6, 1.3), facecolor="#1e1e2e")
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.22, right=0.97, top=0.84, bottom=0.30)
        self._style()
        lay.addWidget(self.canvas)
        self.setMinimumWidth(220)
        self.hist: collections.deque = collections.deque(maxlen=240)
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._redraw)
        self._timer.start()
        self._line, = self.ax.plot([], [], color="#89b4fa", lw=1.2)

    def _style(self) -> None:
        ax = self.ax
        ax.set_facecolor("#12121f")
        for s in ax.spines.values():
            s.set_color("#45475a")
        ax.tick_params(colors="#a6adc8", labelsize=7)
        ax.set_title("Field monitor (last 60 s)", color="#6c7086", fontsize=8)
        ax.set_ylabel("Hall (mT)", color="#a6adc8", fontsize=7)
        ax.set_xlabel("Time (s)", color="#a6adc8", fontsize=7)

    def add(self, value) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if v == v:
            self.hist.append((time.time(), v))
            self._dirty = True

    def _redraw(self) -> None:
        if not self._dirty or not self.isVisible():
            return
        self._dirty = False
        now = time.time()
        pts = [(t - now, v) for t, v in self.hist if now - t <= 60]
        if not pts:
            return
        xs, ys = zip(*pts)
        self._line.set_data(xs, ys)
        self.ax.set_xlim(-60, 0)
        lo, hi = min(ys), max(ys)
        pad = max(0.05 * (hi - lo), 0.05)
        self.ax.set_ylim(lo - pad, hi + pad)
        self.canvas.draw_idle()


# ============================================================================ timing
class TimingGroup(QGroupBox):
    """Samba's Timing box (Int / Settle / T.out) with MiniMoke's timing parameters."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Timing", parent)
        g = QGridLayout(self)
        g.setSpacing(2)
        g.setContentsMargins(6, 4, 6, 4)
        self.w = {
            "acq_time": _dspin(1e-5, 600.0, 5, "s", 90),
            "settle_time": _dspin(0.0, 60.0, 3, "s", 90),
            "field_settle_s": _dspin(0.0, 60.0, 3, "s", 90),
            "interval": _dspin(1e-6, 86400.0, 4, "s", 90),
            "record_hall": QCheckBox("Record Hall"),
        }
        tips = {"acq_time": "Int: how long each point is averaged (DAQ window at 50 kHz; the Hall probe "
                            "integrates min(Int, 0.54 s))",
                "settle_time": "Spatial scans: wait after each stage move before measuring",
                "field_settle_s": "Spatial scans: wait after the coil command changes (+B → −B)",
                "interval": "Time scan only: time from the start of one sample to the start of the next.\n"
                            "Samples N × interval = duration (MiniMoke's 'Sampling interval').\n"
                            "Each sample is averaged for Int; if Int is longer than the interval, the samples\n"
                            "simply follow one another.",
                "record_hall": "Read the Hall probe at every point (MiniMoke always did)"}
        # No "Repeats" field: as in Samba, a measurement is repeated with the Scanlist tab (one file
        # per scan).  MiniMoke's repeat_num is always 1: an X/Y-sweep measures its line once at +B
        # and once at −B, an XY map is measured once.
        rows = [("Int (s):", "acq_time"), ("Settle (s):", "settle_time"), ("B settle (s):", "field_settle_s"),
                ("Interval (s):", "interval")]
        self.labels = {}
        for r, (label, key) in enumerate(rows):
            lbl = QLabel(label)
            self.labels[key] = lbl
            g.addWidget(lbl, r, 0)
            g.addWidget(self.w[key], r, 1)
        g.addWidget(self.w["record_hall"], len(rows), 0, 1, 2)
        for key, w in self.w.items():
            w.setToolTip(tips[key])
            (w.toggled if isinstance(w, QCheckBox) else w.valueChanged).connect(self.changed.emit)

    def values(self) -> dict:
        return {k: (w.isChecked() if isinstance(w, QCheckBox) else w.value()) for k, w in self.w.items()}

    def set_values(self, v: dict) -> None:
        for k, w in self.w.items():
            if k not in v:
                continue
            w.blockSignals(True)
            try:
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(v[k]))
                elif isinstance(w, NoScrollSpinBox):
                    w.setValue(int(round(float(v[k]))))
                else:
                    w.setValue(float(v[k]))
            except (TypeError, ValueError):
                pass
            w.blockSignals(False)

    def set_used(self, names: set[str]) -> None:
        """Grey the fields the selected measurement does not use (Samba greys unused boxes)."""
        for k, w in self.w.items():
            w.setEnabled(k in names)
            if k in self.labels:
                self.labels[k].setEnabled(k in names)


# ============================================================================ panel
class TrajectoryPanel(QWidget):
    workflow_changed = pyqtSignal(str)
    changed = pyqtSignal()

    def __init__(self, bridge_getter=lambda: None, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)
        type_row, self.scan_bg, self.type_btns = segmented_pills(["Spatial", "Field"], self)
        type_row.addStretch()
        root.addLayout(type_row)

        # ---------------------------------------------------------- spatial page
        self.stack = QStackedWidget()
        sp = QWidget()
        spl = QVBoxLayout(sp)
        spl.setContentsMargins(0, 0, 0, 0)
        axes = QHBoxLayout()
        self.x_grp = AxisGroup("X axis", "x")
        self.y_grp = AxisGroup("Y axis", "y")
        axes.addWidget(self.x_grp)
        axes.addWidget(self.y_grp)
        spl.addLayout(axes)
        self.time_move = QCheckBox("Time scan: go to the fixed X / Y first")
        self.time_move.setToolTip("No axis ticked = time scan at the fixed X / Y position.\n"
                                  "Ticked: move the stage there first (MiniMoke did before time traces).")
        self.x_grp.slot.addWidget(self.time_move)
        spl.addStretch()
        self.stack.addWidget(sp)

        # ---------------------------------------------------------- field page
        fp = QWidget()
        fpl = QVBoxLayout(fp)
        fpl.setContentsMargins(0, 0, 0, 0)
        sub = QHBoxLayout()
        self.fsub_bg = QButtonGroup(self)
        self.rb_li = QRadioButton("▸ Lock-in B-sweep")
        self.rb_dc = QRadioButton("▸ DC B-sweep")
        self.rb_li.setChecked(True)
        self.rb_li.setToolTip(WORKFLOWS["b_sweep_lockin"].description)
        self.rb_dc.setToolTip(WORKFLOWS["b_sweep_dc"].description)
        self.fsub_bg.addButton(self.rb_li, 0)
        self.fsub_bg.addButton(self.rb_dc, 1)
        sub.addWidget(self.rb_li)
        sub.addWidget(self.rb_dc)
        sub.addStretch()
        fpl.addLayout(sub)
        boxes = QHBoxLayout()
        self.li_box = FieldSweepBox("b_sweep_lockin", "Lock-in B-sweep")
        self.dc_box = FieldSweepBox("b_sweep_dc", "DC B-sweep")
        self.monitor = FieldMonitor()
        boxes.addWidget(self.li_box, 3)
        boxes.addWidget(self.monitor, 2)
        boxes.addWidget(self.dc_box, 3)
        fpl.addLayout(boxes)
        prow = QHBoxLayout()
        self.f_move = QCheckBox("Move the stage first to")
        self.f_move.setToolTip("MiniMoke moved the stage to (x, y) before field sweeps")
        self.f_x = _dspin(-1e6, 1e6, 3, "µm", 100)
        self.f_y = _dspin(-1e6, 1e6, 3, "µm", 100)
        prow.addWidget(self.f_move)
        prow.addWidget(QLabel("x:"))
        prow.addWidget(self.f_x)
        prow.addWidget(QLabel("y:"))
        prow.addWidget(self.f_y)
        prow.addStretch()
        fpl.addLayout(prow)
        fpl.addStretch()
        self.stack.addWidget(fp)
        root.addWidget(self.stack, 1)

        # ---------------------------------------------------------- bottom row
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.timing = TimingGroup()
        self.meta = MetadataGroup()
        self.hardware = HardwareGroup(bridge_getter)
        bottom.addWidget(self.timing, 0)
        bottom.addWidget(self.meta, 3)
        bottom.addWidget(self.hardware, 4)
        root.addLayout(bottom)

        # ---------------------------------------------------------- defaults + wiring
        self.current = "x_sweep"
        self._shared: dict[str, dict] = {k: {} for k in WORKFLOWS}
        for key in ("time", "b_sweep_dc", "b_sweep_lockin", "y_sweep", "x_sweep"):
            self.set_params(key, WORKFLOWS[key].defaults())
        self.x_grp.scan_cb.setChecked(True)
        self.scan_bg.idClicked.connect(lambda _i: self._mode_changed())
        self.fsub_bg.idClicked.connect(lambda _i: self._mode_changed())
        for g in (self.x_grp, self.y_grp):
            g.scan_toggled.connect(lambda _on: self._mode_changed())
        for w in (self.x_grp, self.y_grp, self.li_box, self.dc_box, self.timing, self.meta, self.hardware):
            w.changed.connect(self._on_changed)
        for w in (self.time_move, self.f_move):
            w.toggled.connect(lambda _on: self._on_changed())
        for w in (self.f_x, self.f_y):
            w.valueChanged.connect(lambda _v: self._on_changed())
        self._apply_mode()

    # ------------------------------------------------------------------ mode
    def _derive_key(self) -> str:
        if self.scan_bg.checkedId() == 1:
            return "b_sweep_dc" if self.rb_dc.isChecked() else "b_sweep_lockin"
        x, y = self.x_grp.scan_cb.isChecked(), self.y_grp.scan_cb.isChecked()
        return "xy_sweep" if x and y else "x_sweep" if x else "y_sweep" if y else "time"

    def _switch(self, key: str) -> None:
        """Lock-in, coil and timing values belong to the measurement (MiniMoke kept them per
        procedure): park the old measurement's values, bring back the new one's."""
        if key == self.current:
            return
        self._shared[self.current] = self._shared_from_widgets()
        self.current = key
        self._shared_to_widgets(self._shared.get(key, {}))

    def _shared_from_widgets(self) -> dict:
        v = dict(self.hardware.lockin_values())
        v["b"] = self.hardware.coil.value()
        v.update(self.timing.values())
        return v

    def _shared_to_widgets(self, v: dict) -> None:
        for w in (self.hardware, self.timing):
            w.blockSignals(True)
        try:
            self.hardware.set_lockin_values(v)
            if "b" in v:
                self.hardware.coil.setValue(float(v["b"]))
            self.timing.set_values(v)
        finally:
            for w in (self.hardware, self.timing):
                w.blockSignals(False)

    def _mode_changed(self) -> None:
        key = self._derive_key()
        changed = key != self.current
        self._switch(key)
        self._apply_mode()
        if changed:
            self.workflow_changed.emit(key)
        self.changed.emit()

    def _apply_mode(self) -> None:
        key = self.current
        field = key in FIELD_KEYS
        self.stack.setCurrentIndex(1 if field else 0)
        self.type_btns[1 if field else 0].setChecked(True)
        time_mode = key == "time"
        self.x_grp.set_time_mode(time_mode)
        self.time_move.setVisible(time_mode)
        self.li_box.set_active(key == "b_sweep_lockin")
        self.dc_box.set_active(key == "b_sweep_dc")
        names = {p.name for p in WORKFLOWS[key].params}
        self.timing.set_used(names)
        self.hardware.set_lockin_used(WORKFLOWS[key].uses_lockin)
        self.hardware.set_coil_used("b" in names)
        self._update_info()

    def _on_changed(self) -> None:
        self._update_info()
        self.changed.emit()

    # ------------------------------------------------------------------ public API
    def set_workflow(self, key: str) -> None:
        if key not in WORKFLOWS:
            return
        for w in (self.x_grp.scan_cb, self.y_grp.scan_cb, self.rb_li, self.rb_dc):
            w.blockSignals(True)
        if key in FIELD_KEYS:
            self.type_btns[1].setChecked(True)
            (self.rb_dc if key == "b_sweep_dc" else self.rb_li).setChecked(True)
        else:
            self.type_btns[0].setChecked(True)
            self.x_grp.scan_cb.setChecked(key in ("x_sweep", "xy_sweep"))
            self.y_grp.scan_cb.setChecked(key in ("y_sweep", "xy_sweep"))
        for w in (self.x_grp.scan_cb, self.y_grp.scan_cb, self.rb_li, self.rb_dc):
            w.blockSignals(False)
        for g in (self.x_grp, self.y_grp):
            g._apply_enabled()
        self._switch(key)
        self._apply_mode()
        self.workflow_changed.emit(key)
        self.changed.emit()

    def workflow(self) -> Workflow:
        return WORKFLOWS[self.current]

    def _all_values(self, key: str) -> dict:
        shared = self._shared_from_widgets() if key == self.current else self._shared.get(key, {})
        v = dict(shared)
        v["repeat_num"] = 1                   # repeats are done with the Scanlist tab (see TimingGroup)
        if key in FIELD_KEYS:
            v.update((self.li_box if key == "b_sweep_lockin" else self.dc_box).values())
            v.update(x=self.f_x.value(), y=self.f_y.value(), position_stage=self.f_move.isChecked())
        else:
            x0, x1, dx = self.x_grp.range_values()
            y0, y1, dy = self.y_grp.range_values()
            v.update(x_min=x0, x_max=x1, x_step=dx, y_min=y0, y_max=y1, y_step=dy,
                     x=self.x_grp.fixed.value(), y=self.y_grp.fixed.value())
            if key == "time":
                v["duration"] = round(self.x_grp.samples.value() * float(v.get("interval", 0.5)), 9)
                v["position_stage"] = self.time_move.isChecked()
        return v

    def params(self, key: str | None = None) -> dict:
        """The selected workflow's parameters (MiniMoke names), exactly."""
        key = key or self.current
        v = self._all_values(key)
        return {p.name: v[p.name] for p in WORKFLOWS[key].params if p.name in v}

    def set_params(self, key: str, values: dict) -> None:
        if key not in WORKFLOWS:
            return
        v = dict(values or {})
        widgets = [self.x_grp, self.y_grp, self.li_box, self.dc_box, self.timing, self.hardware,
                   self.time_move, self.f_move, self.f_x, self.f_y]
        for w in widgets:
            w.blockSignals(True)
        try:
            shared_keys = set(self._shared_from_widgets())
            shared = {k: val for k, val in v.items() if k in shared_keys}
            if key == self.current:
                self._shared_to_widgets(shared)
            else:
                self._shared.setdefault(key, {}).update(shared)
            if key in FIELD_KEYS:
                (self.li_box if key == "b_sweep_lockin" else self.dc_box).set_values(v)
                if "x" in v:
                    self.f_x.setValue(float(v["x"]))
                if "y" in v:
                    self.f_y.setValue(float(v["y"]))
                if "position_stage" in v:
                    self.f_move.setChecked(bool(v["position_stage"]))
            else:
                self.x_grp.set_range(v.get("x_min"), v.get("x_max"), v.get("x_step"))
                self.y_grp.set_range(v.get("y_min"), v.get("y_max"), v.get("y_step"))
                if "x" in v:
                    self.x_grp.fixed.setValue(float(v["x"]))
                if "y" in v:
                    self.y_grp.fixed.setValue(float(v["y"]))
                if key == "time":
                    interval = float(v.get("interval", self._all_values("time").get("interval", 0.5)))
                    if "duration" in v:
                        self.x_grp.samples.setValue(plans.time_points(float(v["duration"]), interval))
                    if "position_stage" in v:
                        self.time_move.setChecked(bool(v["position_stage"]))
        finally:
            for w in widgets:
                w.blockSignals(False)
        self._update_info()
        self.changed.emit()

    def all_params(self) -> dict:
        return {k: self.params(k) for k in WORKFLOWS}

    def metadata(self) -> dict:
        m = self.meta.get_values()
        m["sample"] = m["sample_id"] or "sample"          # RunRequest / file name
        return m

    def set_metadata(self, meta: dict) -> None:
        self.meta.load_values(meta)

    def set_setup(self, setup: str) -> None:
        for g in (self.x_grp, self.y_grp):
            g.set_device(STAGE_NAMES.get(setup, setup))
        self.meta.fix_incidence("PMOKE" if setup == "polar" else None)

    def set_editable(self, editable: bool) -> None:
        for b in self.type_btns:
            b.setEnabled(editable)
        self.stack.setEnabled(editable)
        self.timing.setEnabled(editable)
        self.meta.setEnabled(editable)
        for sp in list(self.hardware.li.values()) + [self.hardware.coil]:
            sp.setEnabled(editable and (sp is self.hardware.coil or self.workflow().uses_lockin))
        if editable:
            self._apply_mode()

    def add_field_reading(self, value) -> None:
        self.monitor.add(value)

    # ------------------------------------------------------------------ estimate
    def estimate(self) -> tuple[int, float | None, str | None]:
        """(points, estimated seconds or None, validation message or None)."""
        wf = self.workflow()
        try:
            p = wf.coerce(self.params())
        except ValueError as exc:
            return 0, None, str(exc)
        k = wf.key
        if k == "b_sweep_dc":
            plan = plans.b_sweep_dc_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"])
            msg = plans.b_sweep_dc_validation(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"])
            return plan.n_total * p["num_sweeps"], plan.n_total * plan.t_point_s * p["num_sweeps"], msg
        if k == "b_sweep_lockin":
            plan = plans.b_sweep_lockin_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"],
                                             p["time_const"], p["acq_time"])
            msg = plans.b_sweep_lockin_validation(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"],
                                                  p["time_const"], p["acq_time"])
            return plan.n_total * p["num_sweeps"], plan.n_total * plan.t_point_s * p["num_sweeps"], msg
        if k == "x_sweep":
            n = plans.n_points(p["x_min"], p["x_max"], p["x_step"]) * 2 * p["repeat_num"]
            return n, n * (p["acq_time"] + 0.05), None
        if k == "y_sweep":
            n = plans.n_points(p["y_min"], p["y_max"], p["y_step"]) * 2 * p["repeat_num"]
            return n, n * (p["acq_time"] + 0.05), None
        if k == "xy_sweep":
            n = (plans.n_points(p["x_min"], p["x_max"], p["x_step"])
                 * plans.n_points(p["y_min"], p["y_max"], p["y_step"]) * p["repeat_num"])
            return n, n * (p["acq_time"] + 0.05), None
        n = plans.time_points(p["duration"], p["interval"])
        return n, max(p["duration"], n * p["acq_time"]), None

    def summary_text(self) -> str:
        """Samba-style pre-scan estimate for the status line under the plot."""
        n, secs, msg = self.estimate()
        wf = self.workflow()
        if msg:
            return f"{wf.title}: {msg}"
        if secs is None:
            return f"{wf.title}: {n} points"
        moves = " + moves" if wf.key in ("x_sweep", "y_sweep", "xy_sweep") else ""
        return f"{wf.title} (MiniMoke {wf.minimoke_name}): {n} points, ≈ {_dur(secs)}{moves}"

    def _update_info(self) -> None:
        for box in (self.li_box, self.dc_box):
            try:
                p = WORKFLOWS[box.key].coerce(self.params(box.key))
                if box.key == "b_sweep_dc":
                    plan = plans.b_sweep_dc_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"])
                else:
                    plan = plans.b_sweep_lockin_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"],
                                                     p["time_const"], p["acq_time"])
                box.info.setText(f"{plan.n_total} pts per sweep × {p['num_sweeps']} · "
                                 f"≈ {_dur(plan.n_total * plan.t_point_s * p['num_sweeps'])}")
            except (ValueError, KeyError, ZeroDivisionError) as exc:
                box.info.setText(str(exc))


def _dur(s: float) -> str:
    s = float(s)
    if s < 60:
        return f"{s:.1f} s"
    if s < 3600:
        return f"{int(s // 60)} min {int(s % 60)} s"
    return f"{int(s // 3600)} h {int((s % 3600) // 60)} min"
