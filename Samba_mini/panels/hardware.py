"""Hardware box of the Trajectory tab (Samba ``panels/hardware_panel.py`` layout).

Samba shows Lock-in / Current Source / Field & Relay; MiniMoke has no Keithley
or relay, so the boxes are Lock-in (7270) / Field / Detectors.

The lock-in is set only here, with "Apply now": measurements read it as it is
set and never change it.  The coil command is the field of spatial and time
scans (applied at their start) and keeps running between measurements; "Set"
applies it now and "Zero outputs" switches it off.  The buttons act on the
instruments immediately and need control of the setup (the service refuses
them otherwise).  Readbacks come from the service's readback events.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton

from core.gui.bridge import call_guarded, error_text
from core.lockin_ranges import mv_text, sensitivity_ranges, truncated_discrete
from Samba_mini.panels.widgets import NoScrollComboBox, NoScrollDoubleSpinBox

LOCKIN_FIELDS = [   # param, label, unit, min, max, decimals  (union of the workflows' limits)
    ("volt", "Ampl:", "V", 0.0, 5.0, 3),
    ("sensi", "Sens:", "V", 1e-9, 1.0, 9),          # a list of the 7270's ranges (SensitivityCombo)
    ("lockin_freq", "Freq:", "Hz", 1.0, 102000.0, 1),
    ("time_const", "TC:", "s", 1e-5, 100.0, 5),
    ("phase", "Phase:", "deg", -180.0, 180.0, 2),
]

_VAL = "font-family:Consolas,'Courier New';font-size:13px;color:#a6e3a1;"
_SMALL = "color:#6c7086;font-size:9px;"


class SensitivityCombo(NoScrollComboBox):
    """The 7270's sensitivity ranges, written in mV (… 0.5 mV, 1 mV, 2 mV …).  The value is in V
    like the workflow parameter; a value between ranges shows the next range up, which is what
    the 7270 would use."""
    valueChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ranges = sensitivity_ranges()
        for v in self._ranges:
            self.addItem(mv_text(v), v)
            self.setItemData(self.count() - 1, f"{v:g} V", Qt.ItemDataRole.ToolTipRole)
        self.setMaxVisibleItems(14)
        self.currentIndexChanged.connect(lambda _i: self.valueChanged.emit(self.value()))

    def value(self) -> float:
        return float(self._ranges[max(self.currentIndex(), 0)])

    def setValue(self, volts: float) -> None:
        self.setCurrentIndex(self._ranges.index(truncated_discrete(float(volts), self._ranges)))


def _readout() -> QLabel:
    lbl = QLabel("—")
    lbl.setStyleSheet(_VAL)
    return lbl


class HardwareGroup(QGroupBox):
    changed = pyqtSignal()
    message = pyqtSignal(str, str)          # level, text

    def __init__(self, bridge_getter, parent=None):
        super().__init__("Hardware", parent)
        self._bridge = bridge_getter
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        # ---------------------------------------------------------- lock-in
        self.li_box = QGroupBox("Lock-in (7270)")
        self.li_box.setToolTip("Ametek 7270: Apply now sends these settings. Measurements never change the "
                               "lock-in; they read it as it is set.")
        lg = QGridLayout(self.li_box)
        lg.setSpacing(3)
        self.li: dict[str, NoScrollDoubleSpinBox | SensitivityCombo] = {}
        for i, (key, label, unit, lo, hi, dec) in enumerate(LOCKIN_FIELDS):
            if key == "sensi":
                sp = SensitivityCombo()
            else:
                sp = NoScrollDoubleSpinBox()
                sp.setDecimals(dec)
                sp.setRange(lo, hi)
                sp.setSuffix(f" {unit}")
                sp.setKeyboardTracking(False)
            sp.setMinimumWidth(96)
            sp.valueChanged.connect(self.changed.emit)
            r, c = divmod(i, 2)
            lg.addWidget(QLabel(label), 1 + r, 2 * c)
            lg.addWidget(sp, 1 + r, 2 * c + 1)
            self.li[key] = sp
        self.li["sensi"].setToolTip("Sensitivity: one of the 7270's ranges (full scale), sent with Apply now")
        self.li["time_const"].setToolTip("Rounded up to the next 7270 value by Apply now")
        self.li["volt"].setToolTip("Oscillator amplitude (OA.): drives the sample current through the "
                                   "external source")
        b_apply = QPushButton("Apply now")
        b_apply.setToolTip("Send these settings to the 7270 now (REFMODE 0). This is the only way the "
                           "lock-in is changed: measurements do not configure it.")
        b_apply.clicked.connect(self._lockin_apply)
        b_off = QPushButton("Osc. off")
        b_off.clicked.connect(lambda: self._cmd("lockin", "output_off"))
        b_read = QPushButton("Read")
        b_read.clicked.connect(self._lockin_read)
        lg.addWidget(b_apply, 4, 0, 1, 2)
        lg.addWidget(b_off, 4, 2)
        lg.addWidget(b_read, 4, 3)
        lg.addWidget(QLabel("X:"), 5, 0)
        self.li_x = _readout()
        lg.addWidget(self.li_x, 5, 1)
        lg.addWidget(QLabel("R:"), 5, 2)
        self.li_r = _readout()
        lg.addWidget(self.li_r, 5, 3)
        self.li_read = QLabel("")
        self.li_read.setStyleSheet(_SMALL)
        lg.addWidget(self.li_read, 6, 0, 1, 4)
        self.li_applied = QLabel("")
        self.li_applied.setStyleSheet(_SMALL)
        self.li_applied.setWordWrap(True)
        self.li_applied.setToolTip("What the 7270 accepted at the last 'Apply now' (the fields above are the "
                                   "values requested for the measurement)")
        lg.addWidget(self.li_applied, 7, 0, 1, 4)
        lay.addWidget(self.li_box, 3)

        # ---------------------------------------------------------- field
        fb = QGroupBox("Field")
        fb.setToolTip("DAQ coil output and Hall probe")
        fg = QGridLayout(fb)
        fg.setSpacing(3)
        fg.addWidget(QLabel("Coil cmd:"), 1, 0)
        self.coil = NoScrollDoubleSpinBox()
        self.coil.setDecimals(4)
        self.coil.setRange(-6.0, 6.0)
        self.coil.setSuffix(" V")
        self.coil.setKeyboardTracking(False)
        self.coil.setToolTip("DAQ coil command of spatial and time scans (MiniMoke's field 'b', labelled 'A' "
                             "there). 'Set' applies it now; runs apply it at their start.")
        self.coil.valueChanged.connect(self.changed.emit)
        fg.addWidget(self.coil, 1, 1)
        b_set = QPushButton("Set")
        b_set.setFixedWidth(44)
        b_set.clicked.connect(lambda: self._cmd("daq", "set_outputs", self._show_coil_applied,
                                                coil_V=self.coil.value()))
        fg.addWidget(b_set, 1, 2)
        fg.addWidget(QLabel("Field:"), 2, 0)
        self.hall = _readout()
        fg.addWidget(self.hall, 2, 1, 1, 2)
        fg.addWidget(QLabel("Coil:"), 3, 0)
        self.coil_rb = _readout()
        fg.addWidget(self.coil_rb, 3, 1, 1, 2)
        b_zero = QPushButton("Zero outputs")
        b_zero.clicked.connect(lambda: self._cmd("daq", "zero_outputs", self._show_coil_applied))
        b_hall0 = QPushButton("Zero Hall")
        b_hall0.setToolTip("Zero the Hall probe (only at zero field)")
        b_hall0.clicked.connect(self._hall_zero)
        fg.addWidget(b_zero, 4, 0, 1, 2)
        fg.addWidget(b_hall0, 4, 2)
        self.coil_applied = QLabel("")
        self.coil_applied.setStyleSheet(_SMALL)
        self.coil_applied.setToolTip("The coil command the DAQ accepted at the last 'Set' / 'Zero outputs'")
        fg.addWidget(self.coil_applied, 5, 0, 1, 3)
        lay.addWidget(fb, 2)

        # ---------------------------------------------------------- detectors
        db = QGroupBox("Detectors")
        db.setToolTip("DAQ analogue inputs (balanced diodes, intensity)")
        dg = QGridLayout(db)
        dg.setSpacing(3)
        dg.addWidget(QLabel("Bal. DC:"), 1, 0)
        self.bal = _readout()
        dg.addWidget(self.bal, 1, 1)
        dg.addWidget(QLabel("Intensity:"), 2, 0)
        self.inten = _readout()
        dg.addWidget(self.inten, 2, 1)
        dg.setRowStretch(3, 1)
        lay.addWidget(db, 2)

        self._readouts = {("hall", "field_mT"): (self.hall, "mT"), ("daq", "coil_command_V"): (self.coil_rb, "V"),
                          ("daq", "balanced_V"): (self.bal, "V"), ("daq", "intensity_V"): (self.inten, "V"),
                          ("lockin", "x_V"): (self.li_x, "V"), ("lockin", "r_V"): (self.li_r, "V")}
        self._dev_state: dict[str, str] = {}

    # ------------------------------------------------------------------ values
    def lockin_values(self) -> dict:
        return {k: sp.value() for k, sp in self.li.items()}

    def set_lockin_values(self, values: dict) -> None:
        for k, sp in self.li.items():
            if k in values:
                sp.blockSignals(True)
                try:
                    sp.setValue(float(values[k]))
                except (TypeError, ValueError):
                    pass
                sp.blockSignals(False)

    def set_lockin_used(self, used: bool) -> None:
        """DC B-sweeps do not use the lock-in: grey its settings like Samba greys unused boxes."""
        for sp in self.li.values():
            sp.setEnabled(used)
        self.li_box.setTitle("Lock-in (7270)" if used else "Lock-in (7270) · not used by a DC B-sweep")

    def set_coil_used(self, used: bool) -> None:
        self.coil.setToolTip(self.coil.toolTip().split("\n")[0] + ("" if used else
                             "\nField sweeps set the coil themselves; this value is only for 'Set'."))

    def set_device_state(self, dev: str, state: str, status: str = "") -> None:
        """A device left (or re-entered) a usable state: its readouts say so instead of showing an
        old number as if it were live."""
        usable = state in ("ON", "MOVING", "RUNNING")
        self._dev_state[dev] = state
        for (d, _a), (lbl, _u) in self._readouts.items():
            if d == dev and not usable:
                lbl.setText(state.lower() if state else "—")
                lbl.setToolTip(f"{dev}: {state} {status}".strip())
                lbl.setStyleSheet("font-family:Consolas,'Courier New';font-size:13px;color:#6c7086;")

    def reset_session(self) -> None:
        """New login/logout: forget readouts and applied values (live values arrive again)."""
        for lbl, _u in self._readouts.values():
            lbl.setText("—")
            lbl.setToolTip("")
            lbl.setStyleSheet(_VAL)
        for lbl in (self.li_read, self.li_applied, self.coil_applied):
            lbl.setText("")

    def set_readout(self, dev: str, attr: str, value, quality: str = "") -> bool:
        entry = self._readouts.get((dev, attr))
        if entry is None:
            return False
        if self._dev_state.get(dev, "ON") not in ("ON", "MOVING", "RUNNING"):
            return True                          # keep showing the state (a stale cache value is not live)
        lbl, unit = entry
        if value is None or (isinstance(value, float) and value != value):
            lbl.setText("—")
            return True
        lbl.setText(f"{value:.4g} {unit}")
        lbl.setToolTip("simulated value" if quality == "SIMULATED" else ("stale" if quality == "STALE" else ""))
        color = "#fab387" if quality == "SIMULATED" else ("#a6adc8" if quality == "STALE" else "#a6e3a1")
        lbl.setStyleSheet(f"font-family:Consolas,'Courier New';font-size:13px;color:{color};")
        return True

    # ------------------------------------------------------------------ actions
    def _client(self):
        b = self._bridge()
        return b.client if b else None

    def _cmd(self, device: str, command: str, on_result=None, **args) -> None:
        c = self._client()
        if c is None:
            return

        def ok(r):
            self.message.emit("info", f"{device}.{command}: done")
            if on_result is not None:
                on_result(r)
        # A reply that arrives after a logout / another login is dropped (call_guarded); the
        # service itself checks the lease and refuses the command otherwise.
        call_guarded(c, lambda: c.device(device).command(command, **args), ok,
                     lambda e: self.message.emit("error", f"{device}.{command}: {error_text(e)}"))

    def _lockin_apply(self) -> None:
        v = self.lockin_values()
        self.li_applied.setText("Applying…")
        self._cmd("lockin", "configure", self._show_lockin_applied, volt=v["volt"], sensi=v["sensi"],
                  freq=v["lockin_freq"], time_const=v["time_const"], phase=v["phase"])

    def _show_lockin_applied(self, r) -> None:
        if not isinstance(r, dict):
            self.li_applied.setText(f"Applied: {r}")
            return
        parts = []
        for key, label, unit in (("oscillator_V", "Ampl", "V"), ("sensitivity_V", "Sens", "V"),
                                 ("frequency_Hz", "Freq", "Hz"), ("time_constant_s", "TC", "s"),
                                 ("phase_deg", "Phase", "°")):
            if isinstance(r.get(key), (int, float)):
                parts.append(f"Sens {mv_text(r[key])}" if key == "sensitivity_V"
                             else f"{label} {r[key]:.4g} {unit}")
        self.li_applied.setText("Applied: " + ("  ".join(parts) if parts else str(r)))

    def _show_coil_applied(self, r) -> None:
        v = r.get("coil_V") if isinstance(r, dict) else None
        self.coil_applied.setText(f"Applied: coil {v:+.4g} V" if isinstance(v, (int, float)) else "Applied")

    def _lockin_read(self) -> None:
        c = self._client()
        if c is None:
            return
        call_guarded(c, lambda: c.device("lockin").command("read_xyrt"),
                     lambda r: self.li_read.setText(f"X {r['x_V']:.4g} V   Y {r['y_V']:.4g} V   "
                                                    f"θ {r['theta_deg']:.1f}°"),
                     lambda e: self.message.emit("error", f"lock-in read: {error_text(e)}"))

    def _hall_zero(self) -> None:
        if QMessageBox.question(self, "Zero Hall probe", "Zero the Hall probe now? The field must be zero."
                                ) != QMessageBox.StandardButton.Yes:
            return
        self._cmd("hall", "zero")
