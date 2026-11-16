"""Right panel: sensor selection, plot configuration and live readouts
(Samba ``panels/right_panel.py`` + ``sensor_picker.py`` adapted to the fixed
MiniMoke channel set)."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QGridLayout, QGroupBox, QLabel, QVBoxLayout, QWidget

from core.plot_config import COLORMAPS, X_NATURAL, X_TIME
from core.scan.channels import SENSORS, sensors_for
from Samba_mini.panels.widgets import NoScrollComboBox

AXES = ["Y1", "Y2", "hidden"]

DEFAULT_AXES = {
    "b_sweep_dc": {"dc_signal": "Y1", "intensity": "Y2"},
    "b_sweep_lockin": {"lockin_x": "Y1", "dc_signal": "Y2"},
    "x_sweep": {"lockin_x": "Y1", "dc_signal": "Y2"},
    "y_sweep": {"lockin_x": "Y1", "dc_signal": "Y2"},
    "xy_sweep": {"dc_signal": "Y1"},
    "time": {"lockin_x": "Y1", "dc_signal": "Y2"},
}

READOUTS = [
    ("daq", "coil_command_V", "Coil command", "V"),
    ("hall", "field_mT", "Hall field", "mT"),
    ("daq", "balanced_V", "Balanced diodes", "V"),
    ("daq", "intensity_V", "Intensity", "V"),
    ("lockin", "x_V", "Lock-in X", "V"),
    ("lockin", "r_V", "Lock-in R", "V"),
]


class RightPanel(QWidget):
    plot_changed = pyqtSignal()
    map_sensor_changed = pyqtSignal(str)
    colormap_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.sens_box = QGroupBox("Sensors")
        self.sens_grid = QGridLayout(self.sens_box)
        self.sens_grid.setVerticalSpacing(4)
        self.axis_combo: dict[str, NoScrollComboBox] = {}
        self.sens_labels: dict[str, QLabel] = {}
        for row, ch in enumerate(SENSORS):
            lbl = QLabel(f"{ch.label} ({ch.unit})")
            combo = NoScrollComboBox()
            combo.setMinimumHeight(24)
            combo.setMinimumWidth(80)
            combo.addItems(AXES)
            combo.setCurrentText("hidden")
            combo.currentTextChanged.connect(lambda _t: self.plot_changed.emit())
            self.sens_grid.addWidget(lbl, row, 0)
            self.sens_grid.addWidget(combo, row, 1)
            self.axis_combo[ch.name] = combo
            self.sens_labels[ch.name] = lbl
        lay.addWidget(self.sens_box)

        plot = QGroupBox("Plot")
        pg = QGridLayout(plot)
        self.x_combo = NoScrollComboBox()
        self.x_combo.currentIndexChanged.connect(lambda _i: self.plot_changed.emit())
        self.map_combo = NoScrollComboBox()
        for ch in SENSORS:
            self.map_combo.addItem(f"{ch.label} ({ch.unit})", ch.name)
        self.map_combo.currentIndexChanged.connect(
            lambda _i: self.map_sensor_changed.emit(self.map_combo.currentData()))
        self.cmap = NoScrollComboBox()
        self.cmap.addItems(COLORMAPS)
        self.cmap.currentTextChanged.connect(self.colormap_changed.emit)
        pg.addWidget(QLabel("1D x-axis"), 0, 0)
        pg.addWidget(self.x_combo, 0, 1)
        pg.addWidget(QLabel("2D map"), 1, 0)
        pg.addWidget(self.map_combo, 1, 1)
        pg.addWidget(QLabel("Colormap"), 2, 0)
        pg.addWidget(self.cmap, 2, 1)
        lay.addWidget(plot)

        live = QGroupBox("Live readouts")
        lg = QGridLayout(live)
        self.readout: dict[tuple[str, str], QLabel] = {}
        for row, (dev, attr, label, unit) in enumerate(READOUTS):
            lg.addWidget(QLabel(label), row, 0)
            v = QLabel("—")
            v.setStyleSheet("font-family:Consolas,'Courier New';color:#a6e3a1;")
            lg.addWidget(v, row, 1)
            self.readout[(dev, attr)] = v
        self.readout_note = QLabel("")
        self.readout_note.setStyleSheet("color:#6c7086;font-size:10px;")
        self.readout_note.setWordWrap(True)
        lg.addWidget(self.readout_note, len(READOUTS), 0, 1, 2)
        lay.addWidget(live)
        lay.addStretch(1)
        self.workflow = "b_sweep_dc"
        self.configure_for("b_sweep_dc")

    def configure_for(self, workflow: str, uses_lockin: bool | None = None) -> None:
        self.workflow = workflow
        from core.scan.workflows import WORKFLOWS
        wf = WORKFLOWS[workflow]
        allowed = {c.name for c in sensors_for(wf.uses_lockin)}
        defaults = DEFAULT_AXES.get(workflow, {})
        for name, combo in self.axis_combo.items():
            vis = name in allowed
            combo.setVisible(vis)
            self.sens_labels[name].setVisible(vis)
            combo.blockSignals(True)
            combo.setCurrentText(defaults.get(name, "hidden"))
            combo.blockSignals(False)
        self.x_combo.blockSignals(True)
        self.x_combo.clear()
        natural = {"b_sweep_dc": "Coil command (V)", "b_sweep_lockin": "Coil command (V)",
                   "x_sweep": "X position (um)", "y_sweep": "Y position (um)", "xy_sweep": "X position (um)",
                   "time": "Time (s)"}[workflow]
        self.x_combo.addItem(natural, X_NATURAL)
        if workflow != "time":
            self.x_combo.addItem("Time (s)", X_TIME)
        for ch in sensors_for(wf.uses_lockin):
            self.x_combo.addItem(f"{ch.label} ({ch.unit})", ch.label)
        self.x_combo.blockSignals(False)
        self.map_combo.blockSignals(True)
        idx = self.map_combo.findData("dc_signal")
        self.map_combo.setCurrentIndex(max(idx, 0))
        self.map_combo.blockSignals(False)
        self.plot_changed.emit()

    def sensors_meta(self) -> list[dict]:
        out = []
        for ch in SENSORS:
            axis = self.axis_combo[ch.name].currentText()
            out.append({"label": ch.label, "unit": ch.unit, "axis": axis if axis != "hidden" else "—",
                        "name": ch.name})
        return out

    def x_key(self) -> str:
        return self.x_combo.currentData() or X_NATURAL

    def set_readout(self, dev: str, attr: str, value, unit: str, quality: str = "") -> None:
        lbl = self.readout.get((dev, attr))
        if lbl is None:
            return
        if value is None or (isinstance(value, float) and value != value):
            lbl.setText("—")
            lbl.setStyleSheet("font-family:Consolas,'Courier New';color:#6c7086;")
            return
        txt = f"{value:.6g} {unit}"
        if quality == "SIMULATED":
            txt += "  (sim)"
        lbl.setText(txt)
        color = "#fab387" if quality == "SIMULATED" else ("#a6adc8" if quality == "STALE" else "#a6e3a1")
        lbl.setStyleSheet(f"font-family:Consolas,'Courier New';color:{color};")
