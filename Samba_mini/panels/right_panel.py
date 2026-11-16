"""Right panel: Samba's sensor rows and Plot box (``Samba_main/panels/right_panel.py``
+ ``sensor_picker.py``) for the fixed MiniMoke channel set.

Each row is checkbox | Device | Channel | Axis like Samba.  Difference: every
MiniMoke channel is always recorded (as MiniMoke did); the checkbox only
shows or hides the curve.  Live readouts moved into the Hardware box.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                             QWidget)

from core.plot_config import COLORMAPS, X_NATURAL, X_TIME
from core.scan.channels import SENSORS, sensors_for
from Samba_mini.panels.widgets import NoScrollComboBox

AXES = ["Y1", "Y2"]

DEVICES = {
    "DAQ": ["dc_signal", "dc_std", "intensity", "intensity_std"],
    "Hall probe": ["hall_field"],
    "Lock-in": ["lockin_x", "lockin_y", "lockin_r", "lockin_theta"],
}
DEVICE_OF = {ch: dev for dev, chans in DEVICES.items() for ch in chans}
CH = {c.name: c for c in SENSORS}

DEFAULT_AXES = {
    "b_sweep_dc": {"dc_signal": "Y1", "intensity": "Y2"},
    "b_sweep_lockin": {"lockin_x": "Y1", "dc_signal": "Y2"},
    "x_sweep": {"lockin_x": "Y1", "dc_signal": "Y2"},
    "y_sweep": {"lockin_x": "Y1", "dc_signal": "Y2"},
    "xy_sweep": {"dc_signal": "Y1"},
    "time": {"lockin_x": "Y1", "dc_signal": "Y2"},
}
NATURAL_X = {"b_sweep_dc": "Coil command (V)", "b_sweep_lockin": "Coil command (V)", "x_sweep": "X position (um)",
             "y_sweep": "Y position (um)", "xy_sweep": "X position (um)", "time": "Time (s)"}

_HDR = "color:#6c7086;font-size:10px;"


class SensorRow(QWidget):
    changed = pyqtSignal()
    remove = pyqtSignal(object)

    def __init__(self, channel: str, plotted: bool, axis: str, allowed: set[str], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.cb = QCheckBox()
        self.cb.setToolTip("Plot this channel (every channel is recorded anyway)")
        self.dev = NoScrollComboBox()
        self.dev.addItems(list(DEVICES))
        self.ch = NoScrollComboBox()
        self.axis = NoScrollComboBox()
        self.axis.addItems(AXES)
        self.axis.setFixedWidth(58)
        for combo, n in ((self.dev, 6), (self.ch, 10)):
            combo.setSizeAdjustPolicy(NoScrollComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(n)
        for w, stretch in ((self.cb, 0), (self.dev, 3), (self.ch, 4), (self.axis, 0)):
            w.setMinimumHeight(24)
            lay.addWidget(w, stretch)
        self.rm = QPushButton("×")
        self.rm.setFixedSize(20, 20)
        self.rm.setToolTip("Remove this row")
        self.rm.setStyleSheet("QPushButton{color:#f38ba8;background:transparent;border:none;font-weight:bold;"
                              "font-size:14px;padding:0;}QPushButton:hover{color:#eba0ac;}")
        self.rm.clicked.connect(lambda: self.remove.emit(self))
        lay.addWidget(self.rm)
        self.allowed = allowed
        self.dev.setCurrentText(DEVICE_OF.get(channel, "DAQ"))
        self._fill_channels(channel)
        self.cb.setChecked(plotted)
        self.axis.setCurrentText(axis)
        self.dev.currentTextChanged.connect(lambda _t: (self._fill_channels(None), self.changed.emit()))
        self.ch.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self.cb.toggled.connect(lambda _on: self.changed.emit())
        self.axis.currentTextChanged.connect(lambda _t: self.changed.emit())

    def _fill_channels(self, select: str | None) -> None:
        self.ch.blockSignals(True)
        self.ch.clear()
        for name in DEVICES[self.dev.currentText()]:
            self.ch.addItem(CH[name].label, name)
        if select is not None:
            i = self.ch.findData(select)
            self.ch.setCurrentIndex(max(i, 0))
        self.ch.blockSignals(False)
        usable = self.channel() in self.allowed
        self.ch.setToolTip("" if usable else "Not measured by this measurement (no lock-in)")
        self.setEnabled(True)

    def channel(self) -> str:
        return self.ch.currentData() or ""


class RightPanel(QWidget):
    plot_changed = pyqtSignal()
    map_sensor_changed = pyqtSignal(str)
    colormap_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        for text, stretch in (("✓", 0), ("Device", 3), ("Channel", 4), ("Axis", 0)):
            lbl = QLabel(text)
            lbl.setStyleSheet(_HDR)
            if text == "Axis":
                lbl.setFixedWidth(82)
            hdr.addWidget(lbl, stretch)
        lay.addLayout(hdr)
        self.rows_w = QWidget()
        self.rows_lay = QVBoxLayout(self.rows_w)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(3)
        lay.addWidget(self.rows_w)
        btns = QHBoxLayout()
        add = QPushButton("+ Add channel")
        add.clicked.connect(lambda: self._add_row("dc_signal", True, "Y2"))
        reset = QPushButton("↺ Defaults")
        reset.setToolTip("Back to the default curves of this measurement")
        reset.clicked.connect(lambda: self.configure_for(self.workflow))
        btns.addWidget(add)
        btns.addWidget(reset)
        btns.addStretch()
        lay.addLayout(btns)
        self.rows: list[SensorRow] = []

        plot = QGroupBox("Plot")
        pg = QGridLayout(plot)
        self.map_combo = NoScrollComboBox()
        for ch in SENSORS:
            self.map_combo.addItem(ch.label, ch.name)
        self.map_combo.currentIndexChanged.connect(
            lambda _i: self.map_sensor_changed.emit(self.map_combo.currentData()))
        self.cmap = NoScrollComboBox()
        self.cmap.addItems(COLORMAPS)
        self.cmap.currentTextChanged.connect(self.colormap_changed.emit)
        self.x_combo = NoScrollComboBox()
        self.x_combo.currentIndexChanged.connect(lambda _i: self.plot_changed.emit())
        pg.addWidget(QLabel("2D sensor:"), 0, 0)
        pg.addWidget(self.map_combo, 0, 1)
        pg.addWidget(QLabel("Colormap:"), 0, 2)
        pg.addWidget(self.cmap, 0, 3)
        pg.addWidget(QLabel("X axis:"), 1, 0)
        pg.addWidget(self.x_combo, 1, 1, 1, 3)
        pg.setColumnStretch(1, 1)
        lay.addWidget(plot)
        lay.addStretch(1)
        self.workflow = "x_sweep"
        self.configure_for("x_sweep")

    # ------------------------------------------------------------------ rows
    def _add_row(self, channel: str, plotted: bool, axis: str) -> SensorRow:
        row = SensorRow(channel, plotted, axis, self._allowed())
        row.changed.connect(self.plot_changed.emit)
        row.remove.connect(self._remove_row)
        self.rows.append(row)
        self.rows_lay.addWidget(row)
        self.plot_changed.emit()
        return row

    def _remove_row(self, row: SensorRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()
            self.plot_changed.emit()

    def _allowed(self) -> set[str]:
        from core.scan.workflows import WORKFLOWS
        return {c.name for c in sensors_for(WORKFLOWS[self.workflow].uses_lockin)}

    def configure_for(self, workflow: str) -> None:
        self.workflow = workflow
        allowed = self._allowed()
        defaults = DEFAULT_AXES.get(workflow, {})
        for row in list(self.rows):
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        self.blockSignals(True)
        try:
            for ch in SENSORS:      # the std channels are recorded too; "+ Add channel" plots them
                if ch.name in allowed and not ch.name.endswith("_std"):
                    self._add_row(ch.name, ch.name in defaults, defaults.get(ch.name, "Y2"))
        finally:
            self.blockSignals(False)
        self.x_combo.blockSignals(True)
        self.x_combo.clear()
        self.x_combo.addItem(NATURAL_X[workflow], X_NATURAL)
        if workflow != "time":
            self.x_combo.addItem("Time (s)", X_TIME)
        for ch in SENSORS:
            if ch.name in allowed:
                self.x_combo.addItem(f"{ch.label} ({ch.unit})", ch.label)
        self.x_combo.blockSignals(False)
        self.map_combo.blockSignals(True)
        self.map_combo.setCurrentIndex(max(self.map_combo.findData("dc_signal"), 0))
        self.map_combo.blockSignals(False)
        self.plot_changed.emit()

    def sensors_meta(self) -> list[dict]:
        """Every channel with the axis it is plotted on ('—' = not plotted)."""
        allowed = self._allowed()
        axis_of: dict[str, str] = {}
        for row in self.rows:
            name = row.channel()
            if row.cb.isChecked() and name in allowed and name not in axis_of:
                axis_of[name] = row.axis.currentText()
        return [{"label": ch.label, "unit": ch.unit, "axis": axis_of.get(ch.name, "—"), "name": ch.name}
                for ch in SENSORS]

    def x_key(self) -> str:
        return self.x_combo.currentData() or X_NATURAL
