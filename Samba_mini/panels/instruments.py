"""Instruments tab: lock-in settings, coil command, Hall zeroing and manual
stage positioning for the selected setup (MiniMoke's motor tabs and live tab).

Every action goes through the device service and needs control of the setup
(the lease); the service rejects it otherwise, whatever this panel shows.
Slow calls run off the GUI thread.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                             QVBoxLayout, QWidget)

from core.gui.bridge import call_async, error_text
from Samba_mini.panels.widgets import NoScrollComboBox, NoScrollDoubleSpinBox

JOG_STEPS_MM = [10.0, 1.0, 0.1, 0.01, 0.001, 0.0001, 0.00001]   # MiniMoke odometer steps


class InstrumentsPanel(QWidget):
    message = pyqtSignal(str, str)          # level, text

    def __init__(self, bridge_getter, setup_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        self._setup = setup_getter
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        # ---------------------------------------------------------- lock-in
        li = QGroupBox("Lock-in (7270)")
        lg = QGridLayout(li)
        self.li_fields = {}
        for row, (key, label, unit, lo, hi, dec, default) in enumerate([
                ("volt", "Oscillator amplitude", "V", 0, 5, 3, 0.0),
                ("sensi", "Sensitivity", "V", 2e-9, 1, 9, 1e-3),
                ("freq", "Frequency", "Hz", 1, 250000, 1, 1777.0),
                ("time_const", "Time constant", "s", 1e-5, 100, 5, 0.1),
                ("phase", "Phase", "deg", -360, 360, 2, 0.0)]):
            sp = NoScrollDoubleSpinBox()
            sp.setDecimals(dec)
            sp.setRange(lo, hi)
            sp.setValue(default)
            sp.setSuffix(f" {unit}")
            lg.addWidget(QLabel(label), row, 0)
            lg.addWidget(sp, row, 1)
            self.li_fields[key] = sp
        b_apply = QPushButton("Apply (REFMODE 0)")
        b_apply.clicked.connect(self._lockin_apply)
        b_off = QPushButton("Oscillator off")
        b_off.clicked.connect(lambda: self._cmd("lockin", "output_off"))
        b_read = QPushButton("Read X/Y")
        b_read.clicked.connect(self._lockin_read)
        lg.addWidget(b_apply, 5, 0)
        lg.addWidget(b_off, 5, 1)
        lg.addWidget(b_read, 6, 0)
        self.li_read = QLabel("—")
        lg.addWidget(self.li_read, 6, 1)
        lg.setRowStretch(7, 1)
        lay.addWidget(li)

        # ---------------------------------------------------------- field
        fb = QGroupBox("Field")
        fg = QGridLayout(fb)
        self.coil = NoScrollDoubleSpinBox()
        self.coil.setDecimals(4)
        self.coil.setRange(-6, 6)
        self.coil.setSuffix(" V")
        self.coil.setToolTip("DAQ coil command (MiniMoke labelled it 'A')")
        b_set = QPushButton("Set coil")
        b_set.clicked.connect(lambda: self._cmd("daq", "set_outputs", coil_V=self.coil.value()))
        b_zero = QPushButton("Zero outputs")
        b_zero.clicked.connect(lambda: self._cmd("daq", "zero_outputs"))
        b_hall0 = QPushButton("Zero Hall probe")
        b_hall0.setToolTip("Only at zero field")
        b_hall0.clicked.connect(self._hall_zero)
        fg.addWidget(QLabel("Coil command"), 0, 0)
        fg.addWidget(self.coil, 0, 1)
        fg.addWidget(b_set, 1, 0)
        fg.addWidget(b_zero, 1, 1)
        fg.addWidget(b_hall0, 2, 0, 1, 2)
        fg.setRowStretch(3, 1)
        lay.addWidget(fb)

        # ---------------------------------------------------------- stage
        sb = QGroupBox("Stage")
        sg = QGridLayout(sb)
        self.stage_title = QLabel("")
        sg.addWidget(self.stage_title, 0, 0, 1, 5)
        self.pos_lbl = {}
        self.goto = {}
        for row, ax in enumerate(("x", "y", "z"), start=1):
            sg.addWidget(QLabel(ax.upper()), row, 0)
            lbl = QLabel("—")
            lbl.setStyleSheet("font-family:Consolas,'Courier New';color:#a6e3a1;min-width:90px;")
            self.pos_lbl[ax] = lbl
            sg.addWidget(lbl, row, 1)
            minus = QPushButton("◀")
            plus = QPushButton("▶")
            minus.setFixedWidth(32)
            plus.setFixedWidth(32)
            minus.clicked.connect(lambda _=False, a=ax: self._jog(a, -1))
            plus.clicked.connect(lambda _=False, a=ax: self._jog(a, +1))
            sg.addWidget(minus, row, 2)
            sg.addWidget(plus, row, 3)
            g = NoScrollDoubleSpinBox()
            g.setDecimals(5)
            g.setRange(-100, 100)
            g.setSuffix(" mm")
            self.goto[ax] = g
            sg.addWidget(g, row, 4)
        self.step = NoScrollComboBox()
        for s in JOG_STEPS_MM:
            self.step.addItem(f"{s:g} mm", s)
        self.step.setCurrentIndex(3)
        sg.addWidget(QLabel("Jog step"), 4, 0)
        sg.addWidget(self.step, 4, 1, 1, 3)
        b_go = QPushButton("Go to")
        b_go.clicked.connect(self._goto)
        b_stop = QPushButton("STOP")
        b_stop.setObjectName("abort_btn")
        b_stop.clicked.connect(lambda: self._cmd(self._stage(), "stop", quiet=True))
        b_zero_s = QPushButton("Set zero here")
        b_zero_s.setToolTip("MiniMoke 'home': the current position becomes the software zero")
        b_zero_s.clicked.connect(self._set_zero)
        b_read_s = QPushButton("Read")
        b_read_s.clicked.connect(self.refresh_positions)
        sg.addWidget(b_go, 5, 4)
        sg.addWidget(b_stop, 5, 0, 1, 2)
        sg.addWidget(b_zero_s, 6, 0, 1, 2)
        sg.addWidget(b_read_s, 6, 4)
        sg.setRowStretch(7, 1)
        lay.addWidget(sb)
        lay.addStretch(1)
        self.update_setup()

    # ------------------------------------------------------------------
    def _stage(self) -> str:
        return f"stage_{self._setup()}"

    def update_setup(self) -> None:
        s = self._setup()
        self.stage_title.setText("Longitudinal stage (Thorlabs Kinesis)" if s == "longitudinal"
                                 else "Polar stage (Trinamic TMCM-3212)")

    def _client(self):
        b = self._bridge()
        return b.client if b else None

    def _cmd(self, device: str, command: str, quiet: bool = False, **args) -> None:
        c = self._client()
        if c is None:
            return

        def ok(res):
            if not quiet:
                self.message.emit("info", f"{device}.{command}: done")
            if device.startswith("stage"):
                self.refresh_positions()

        call_async(lambda: c.device(device).command(command, **args), ok,
                   lambda e: self.message.emit("error", f"{device}.{command}: {error_text(e)}"))

    def _lockin_apply(self):
        f = self.li_fields
        self._cmd("lockin", "configure", volt=f["volt"].value(), sensi=f["sensi"].value(),
                  freq=f["freq"].value(), time_const=f["time_const"].value(), phase=f["phase"].value())

    def _lockin_read(self):
        c = self._client()
        if c is None:
            return
        call_async(lambda: c.device("lockin").command("read_xyrt"),
                   lambda r: self.li_read.setText(f"X {r['x_V']:.4g} V  Y {r['y_V']:.4g} V  "
                                                  f"θ {r['theta_deg']:.1f}°"),
                   lambda e: self.message.emit("error", f"lock-in read: {error_text(e)}"))

    def _hall_zero(self):
        if QMessageBox.question(self, "Zero Hall probe", "Zero the Hall probe now? The field must be zero."
                                ) != QMessageBox.StandardButton.Yes:
            return
        self._cmd("hall", "zero")

    def _jog(self, axis: str, sign: int):
        self._cmd(self._stage(), "jog", quiet=True, axis=axis, delta_mm=sign * float(self.step.currentData()))

    def _goto(self):
        c = self._client()
        if c is None:
            return
        st = self._stage()
        targets = {a: self.goto[a].value() for a in ("x", "y", "z")}

        def go():
            out = {}
            for a, v in targets.items():
                out[a] = c.device(st).command("move_to", axis=a, position_mm=v)
            return out
        call_async(go, lambda r: self.refresh_positions(),
                   lambda e: self.message.emit("error", f"move: {error_text(e)}"))

    def _set_zero(self):
        if QMessageBox.question(self, "Set zero", "Make the current position the software zero of all axes?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        self._cmd(self._stage(), "set_zero", axis="all")

    def refresh_positions(self):
        c = self._client()
        if c is None or not c.session:
            return
        st = self._stage()
        call_async(lambda: c.device(st).read("x_mm", "y_mm", "z_mm"), self._show_positions,
                   lambda e: None)

    def _show_positions(self, readings):
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
        lbl.setText(f"{value:+.5f} mm" + (" (sim)" if quality == "SIMULATED" else ""))
