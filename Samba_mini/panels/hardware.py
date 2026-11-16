"""Hardware box of the Trajectory tab (Samba ``panels/hardware_panel.py`` layout).

Samba shows Lock-in / Current Source / Field & Relay; MiniMoke has no Keithley
or relay, so the boxes are Lock-in (7270) / Field / Detectors.

The lock-in values and the coil command are the settings of the measurement
(saved with the configuration and applied by the engine at run start, as
MiniMoke did); the buttons act on the instruments immediately and need control
of the setup (the service refuses them otherwise).  Readbacks come from the
service's readback events.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton

from core.gui.bridge import call_async, error_text
from Samba_mini.panels.widgets import NoScrollDoubleSpinBox

LOCKIN_FIELDS = [   # param, label, unit, min, max, decimals  (union of the workflows' limits)
    ("volt", "Ampl:", "V", 0.0, 5.0, 3),
    ("sensi", "Sens:", "V", 1e-9, 1.0, 9),
    ("lockin_freq", "Freq:", "Hz", 1.0, 102000.0, 1),
    ("time_const", "TC:", "s", 1e-5, 100.0, 5),
    ("phase", "Phase:", "deg", -180.0, 180.0, 2),
]

_VAL = "font-family:Consolas,'Courier New';font-size:13px;color:#a6e3a1;"
_SMALL = "color:#6c7086;font-size:9px;"


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
        self.li_box.setToolTip("Ametek 7270: the settings of this measurement, applied at run start")
        lg = QGridLayout(self.li_box)
        lg.setSpacing(3)
        self.li: dict[str, NoScrollDoubleSpinBox] = {}
        for i, (key, label, unit, lo, hi, dec) in enumerate(LOCKIN_FIELDS):
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
        self.li["sensi"].setToolTip("Rounded up to the next 7270 range at run start")
        self.li["time_const"].setToolTip("Rounded up to the next 7270 value at run start")
        self.li["volt"].setToolTip("Oscillator amplitude (OA.): drives the sample current through the "
                                   "external source")
        b_apply = QPushButton("Apply now")
        b_apply.setToolTip("Send these settings to the 7270 now (REFMODE 0). Runs apply them anyway.")
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
        b_set.clicked.connect(lambda: self._cmd("daq", "set_outputs", coil_V=self.coil.value()))
        fg.addWidget(b_set, 1, 2)
        fg.addWidget(QLabel("Field:"), 2, 0)
        self.hall = _readout()
        fg.addWidget(self.hall, 2, 1, 1, 2)
        fg.addWidget(QLabel("Coil:"), 3, 0)
        self.coil_rb = _readout()
        fg.addWidget(self.coil_rb, 3, 1, 1, 2)
        b_zero = QPushButton("Zero outputs")
        b_zero.clicked.connect(lambda: self._cmd("daq", "zero_outputs"))
        b_hall0 = QPushButton("Zero Hall")
        b_hall0.setToolTip("Zero the Hall probe (only at zero field)")
        b_hall0.clicked.connect(self._hall_zero)
        fg.addWidget(b_zero, 4, 0, 1, 2)
        fg.addWidget(b_hall0, 4, 2)
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

    def set_readout(self, dev: str, attr: str, value, quality: str = "") -> bool:
        entry = self._readouts.get((dev, attr))
        if entry is None:
            return False
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

    def _cmd(self, device: str, command: str, **args) -> None:
        c = self._client()
        if c is None:
            return
        call_async(lambda: c.device(device).command(command, **args),
                   lambda r: self.message.emit("info", f"{device}.{command}: done"),
                   lambda e: self.message.emit("error", f"{device}.{command}: {error_text(e)}"))

    def _lockin_apply(self) -> None:
        v = self.lockin_values()
        self._cmd("lockin", "configure", volt=v["volt"], sensi=v["sensi"], freq=v["lockin_freq"],
                  time_const=v["time_const"], phase=v["phase"])

    def _lockin_read(self) -> None:
        c = self._client()
        if c is None:
            return
        call_async(lambda: c.device("lockin").command("read_xyrt"),
                   lambda r: self.li_read.setText(f"X {r['x_V']:.4g} V   Y {r['y_V']:.4g} V   "
                                                  f"θ {r['theta_deg']:.1f}°"),
                   lambda e: self.message.emit("error", f"lock-in read: {error_text(e)}"))

    def _hall_zero(self) -> None:
        if QMessageBox.question(self, "Zero Hall probe", "Zero the Hall probe now? The field must be zero."
                                ) != QMessageBox.StandardButton.Yes:
            return
        self._cmd("hall", "zero")
