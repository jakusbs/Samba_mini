"""Calibration tab (next to 2D Map / 1D Plot, as in Samba): stage positioning.

Samba's Calibration tab holds the stage positioning group (plus autofocus,
which MiniMoke does not have).  Here: readback, jog with MiniMoke's odometer
steps, go-to, STOP and "set zero here" for the selected setup's stage.

Every action goes through the device service and needs control of the setup
(the lease); the service rejects it otherwise.  Slow calls run off the GUI
thread.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
                             QWidget)

from core.gui.bridge import call_async, error_text
from Samba_mini.panels.widgets import NoScrollComboBox, NoScrollDoubleSpinBox

JOG_STEPS_MM = [10.0, 1.0, 0.1, 0.01, 0.001, 0.0001, 0.00001]   # MiniMoke odometer steps
STAGE_TITLES = {"longitudinal": "Longitudinal stage · Thorlabs Kinesis",
                "polar": "Polar stage · Trinamic TMCM-3212"}

_AXIS = "color:#89b4fa;font-size:16px;font-weight:bold;"
_RB = "font-family:Consolas,'Courier New';font-size:14px;color:#a6e3a1;min-width:150px;"


class CalibrationPanel(QWidget):
    message = pyqtSignal(str, str)          # level, text

    def __init__(self, bridge_getter, setup_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        self._setup = setup_getter
        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        box = QGroupBox("Stage positioning")
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
            sp.setMinimumWidth(120)
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
        left = QVBoxLayout()
        left.addWidget(box)
        left.addStretch(1)
        outer.addLayout(left, 0)
        outer.addStretch(1)
        self.update_setup()

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
        lbl.setText(f"{value:+.5f} mm" + ("  (sim)" if quality == "SIMULATED" else ""))
