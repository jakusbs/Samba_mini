"""Right panel: Samba's sensor rows and Plot box (``Samba_main/panels/right_panel.py``
+ ``sensor_picker.py``).

Each row is checkbox | Device | Channel | Axis, as in Samba:

* the **checkbox** decides what is *measured and saved* -- an instrument with no
  ticked channel is not read at all (no lock-in or Hall-probe query per point);
* **Device / Channel** list the channels by the names given in Samba_controls
  (Channels tab), e.g. "BD", "Focus line";
* the **axis** dropdown only decides the plot: Y1 (left), Y2 (right) or hidden.

Each measurement (workflow) keeps its own rows; they are saved with the named
configuration and the working state.  Lock-in rows are greyed in a DC B-sweep,
which does not use the lock-in.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                             QWidget)

from core.config import machine
from core.plot_config import COLORMAPS, X_NATURAL, X_TIME
from core.scan.channels import BY_ID, channel_table
from core.scan.workflows import WORKFLOWS
from Samba_mini.panels.widgets import NoScrollComboBox

AXES = ["Y1", "Y2", "hidden"]

#: What each measurement plots by default (every available channel is measured by default).
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


def default_table() -> list[dict]:
    """The sensor channels with their default names (logged out / older service)."""
    return [r for r in channel_table(machine.DEFAULTS) if r["kind"] == "sensor"]


def allowed_for(workflow: str) -> set[str]:
    uses_lockin = WORKFLOWS[workflow].uses_lockin
    return {cid for cid, c in BY_ID.items() if c.access == "r" and (uses_lockin or not c.lockin)}


class SensorRow(QWidget):
    changed = pyqtSignal()
    remove = pyqtSignal(object)

    def __init__(self, table: list[dict], channel: str, measured: bool, axis: str, allowed: set[str],
                 parent=None):
        super().__init__(parent)
        self.table = table
        self.allowed = allowed
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.cb = QCheckBox()
        self.cb.setToolTip("Measure and save this channel")
        self.dev = NoScrollComboBox()
        self.ch = NoScrollComboBox()
        self.axis = NoScrollComboBox()
        self.axis.addItems(AXES)
        self.axis.setFixedWidth(74)
        self.axis.setToolTip("Plot on the left axis (Y1), the right axis (Y2), or not at all (hidden)")
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
        self._fill_devices(channel)
        self.cb.setChecked(measured)
        self.axis.setCurrentText(axis if axis in AXES else "hidden")
        self.dev.currentIndexChanged.connect(lambda _i: (self._fill_channels(None), self.changed.emit()))
        self.ch.currentIndexChanged.connect(lambda _i: (self._mark(), self.changed.emit()))
        self.cb.toggled.connect(lambda _on: self.changed.emit())
        self.axis.currentTextChanged.connect(lambda _t: self.changed.emit())

    def _device_of(self, channel: str) -> str:
        return next((r["device"] for r in self.table if r["id"] == channel), self.table[0]["device"])

    def _fill_devices(self, channel: str) -> None:
        self.dev.blockSignals(True)
        self.dev.clear()
        for dev in dict.fromkeys(r["device"] for r in self.table):
            label = next(r["device_label"] for r in self.table if r["device"] == dev)
            self.dev.addItem(label, dev)
        self.dev.setCurrentIndex(max(self.dev.findData(self._device_of(channel)), 0))
        self.dev.blockSignals(False)
        self._fill_channels(channel)

    def _fill_channels(self, select: str | None) -> None:
        self.ch.blockSignals(True)
        self.ch.clear()
        for r in self.table:
            if r["device"] == self.dev.currentData():
                self.ch.addItem(r["name"], r["id"])
        if select is not None:
            self.ch.setCurrentIndex(max(self.ch.findData(select), 0))
        self.ch.blockSignals(False)
        self._mark()

    def _mark(self) -> None:
        usable = self.channel() in self.allowed
        for w in (self.dev, self.ch):
            w.setToolTip("" if usable else "Not measured by this measurement (it does not use the lock-in)")
        self.cb.setEnabled(usable)
        self.setStyleSheet("" if usable else "QComboBox{color:#6c7086;}")

    def set_table(self, table: list[dict]) -> None:
        cid = self.channel()
        self.table = table
        self._fill_devices(cid)

    def channel(self) -> str:
        return self.ch.currentData() or ""

    def get(self) -> dict:
        return {"id": self.channel(), "on": self.cb.isChecked(), "axis": self.axis.currentText()}


class RightPanel(QWidget):
    plot_changed = pyqtSignal()
    map_sensor_changed = pyqtSignal(str)
    colormap_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table: list[dict] = default_table()
        self._states: dict[str, list[dict]] = {}       # workflow -> its rows
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        for text, stretch, tip in (("✓", 0, "Measure and save"), ("Device", 3, ""), ("Channel", 4, ""),
                                   ("Plot", 0, "Y1 / Y2 / hidden")):
            lbl = QLabel(text)
            lbl.setStyleSheet(_HDR)
            lbl.setToolTip(tip)
            if text == "Plot":
                lbl.setFixedWidth(98)
            hdr.addWidget(lbl, stretch)
        lay.addLayout(hdr)
        self.rows_w = QWidget()
        self.rows_lay = QVBoxLayout(self.rows_w)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(3)
        lay.addWidget(self.rows_w)
        btns = QHBoxLayout()
        add = QPushButton("+ Add channel")
        add.clicked.connect(self._add_unused)
        reset = QPushButton("↺ Defaults")
        reset.setToolTip("Back to the default channels of this measurement (all measured)")
        reset.clicked.connect(lambda: self._load_rows(self._defaults(self.workflow), emit=True))
        btns.addWidget(add)
        btns.addWidget(reset)
        btns.addStretch()
        lay.addLayout(btns)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#f38ba8;font-size:10px;")
        lay.addWidget(self.note)
        self.rows: list[SensorRow] = []

        plot = QGroupBox("Plot")
        pg = QGridLayout(plot)
        self.map_combo = NoScrollComboBox()
        self.map_combo.currentIndexChanged.connect(
            lambda _i: self.map_sensor_changed.emit(self.map_combo.currentData() or ""))
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
        self._load_rows(self._defaults("x_sweep"), emit=False)
        self._refresh_combos()

    # ------------------------------------------------------------------ channel table
    def set_channels(self, table: list[dict]) -> None:
        """The channel names / devices of the running service (sensors only)."""
        self.table = [r for r in table if r.get("kind", "sensor") == "sensor"] or default_table()
        for row in self.rows:
            row.set_table(self.table)
        self._refresh_combos()
        self.plot_changed.emit()

    def info(self, channel: str) -> dict:
        return next((r for r in self.table if r["id"] == channel), {"id": channel, "name": channel, "unit": ""})

    # ------------------------------------------------------------------ rows
    def _defaults(self, workflow: str) -> list[dict]:
        allowed = allowed_for(workflow)
        axes = DEFAULT_AXES.get(workflow, {})
        return [{"id": r["id"], "on": True, "axis": axes.get(r["id"], "hidden")}
                for r in self.table if r["id"] in allowed]

    def _add_row(self, channel: str, measured: bool, axis: str) -> SensorRow:
        row = SensorRow(self.table, channel, measured, axis, allowed_for(self.workflow))
        row.changed.connect(self._row_changed)
        row.remove.connect(self._remove_row)
        self.rows.append(row)
        self.rows_lay.addWidget(row)
        return row

    def _add_unused(self) -> None:
        used = {r.channel() for r in self.rows}
        free = [r["id"] for r in self.table if r["id"] not in used and r["id"] in allowed_for(self.workflow)]
        self._add_row(free[0] if free else self.table[0]["id"], True, "hidden")
        self._row_changed()

    def _remove_row(self, row: SensorRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()
            self._row_changed()

    def _row_changed(self) -> None:
        self._states[self.workflow] = self.rows_state()
        self._refresh_combos()
        self.plot_changed.emit()

    def _load_rows(self, rows: list[dict], emit: bool) -> None:
        for row in list(self.rows):
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        for r in rows or []:
            if r.get("id") in BY_ID and BY_ID[r["id"]].access == "r":
                self._add_row(r["id"], bool(r.get("on", True)), r.get("axis", "hidden"))
        self._states[self.workflow] = self.rows_state()
        self._refresh_combos()
        if emit:
            self.plot_changed.emit()

    def rows_state(self) -> list[dict]:
        return [row.get() for row in self.rows]

    # ------------------------------------------------------------------ workflows / persistence
    def configure_for(self, workflow: str) -> None:
        """Another measurement selected: keep this one's rows, show that one's (or its defaults)."""
        if self.workflow in WORKFLOWS:
            self._states[self.workflow] = self.rows_state()
        self.workflow = workflow
        self.x_combo.blockSignals(True)
        self.x_combo.clear()                 # refilled for this measurement by _refresh_combos
        self.x_combo.blockSignals(False)
        self._load_rows(self._states.get(workflow) or self._defaults(workflow), emit=False)
        self.plot_changed.emit()

    def state_for(self, workflow: str) -> list[dict]:
        if workflow == self.workflow:
            return self.rows_state()
        return list(self._states.get(workflow) or self._defaults(workflow))

    def all_states(self) -> dict[str, list[dict]]:
        out = dict(self._states)
        out[self.workflow] = self.rows_state()
        return out

    def load_state(self, workflow: str, rows: list[dict] | None) -> None:
        self._states[workflow] = list(rows) if rows else self._defaults(workflow)
        if workflow == self.workflow:
            self._load_rows(self._states[workflow], emit=True)

    def load_all(self, states: dict | None) -> None:
        """A new session / setup: exactly these rows (or each measurement's defaults)."""
        self._states = {k: list(v) for k, v in (states or {}).items() if k in WORKFLOWS and isinstance(v, list)}
        self._load_rows(self._states.get(self.workflow) or self._defaults(self.workflow), emit=True)

    # ------------------------------------------------------------------ selection
    def selected(self) -> list[dict]:
        """The ticked channels this measurement records (one entry per channel, in row order)."""
        return self.selected_for(self.workflow)

    def selected_for(self, workflow: str) -> list[dict]:
        allowed = allowed_for(workflow)
        out, seen = [], set()
        for r in self.state_for(workflow):
            cid = r.get("id")
            if r.get("on") and cid in allowed and cid not in seen:
                seen.add(cid)
                info = self.info(cid)
                out.append({"id": cid, "name": info["name"], "unit": info["unit"],
                            "device": info.get("device", BY_ID[cid].device), "line": info.get("line", ""),
                            "y_axis": r.get("axis", "hidden")})
        return out

    def set_running(self, running: bool) -> None:
        """During a run the measured channels are fixed (they are what the file records); only the
        plot axis of each row can still be changed."""
        for row in self.rows:
            for w in (row.dev, row.ch, row.rm):
                w.setEnabled(not running)
            row.cb.setEnabled(not running and row.channel() in row.allowed)
        for b in self.findChildren(QPushButton):
            if b.text() in ("+ Add channel", "↺ Defaults"):
                b.setEnabled(not running)

    def sensors_meta(self) -> list[dict]:
        """The measured channels with the axis they are plotted on ('—' = hidden)."""
        return [{"name": r["id"], "label": r["name"], "unit": r["unit"],
                 "axis": r["y_axis"] if r["y_axis"] in ("Y1", "Y2") else "—"} for r in self.selected()]

    def _refresh_combos(self) -> None:
        sel = self.selected()
        self.note.setText("" if sel else "No channel ticked: nothing would be measured.")
        cur = self.map_combo.currentData()
        self.map_combo.blockSignals(True)
        self.map_combo.clear()
        for r in sel:
            self.map_combo.addItem(r["name"], r["id"])
        i = self.map_combo.findData(cur) if cur else -1
        if i < 0:
            i = self.map_combo.findData("dc_signal")
        self.map_combo.setCurrentIndex(max(i, 0))
        self.map_combo.blockSignals(False)
        xcur = self.x_combo.currentData()
        self.x_combo.blockSignals(True)
        while self.x_combo.count() > (1 if self.workflow == "time" else 2):
            self.x_combo.removeItem(self.x_combo.count() - 1)
        if self.x_combo.count() == 0:
            self.x_combo.addItem(NATURAL_X[self.workflow], X_NATURAL)
            if self.workflow != "time":
                self.x_combo.addItem("Time (s)", X_TIME)
        for r in sel:
            self.x_combo.addItem(f"{r['name']} ({r['unit']})", r["name"])
        self.x_combo.setCurrentIndex(max(self.x_combo.findData(xcur), 0))
        self.x_combo.blockSignals(False)

    def x_key(self) -> str:
        return self.x_combo.currentData() or X_NATURAL
