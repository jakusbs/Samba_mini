"""Device table and per-device detail panel for Samba_controls."""

from __future__ import annotations

import json
import math

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit,
                             QVBoxLayout, QWidget)

from core.gui.bridge import call_async, error_text
from core.theme import STATUS_COLORS

COLUMNS = ["Device", "Kind", "State", "Status", "Busy", "Identity"]


class DeviceTable(QTableWidget):
    selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(0, len(COLUMNS), parent)
        self.setHorizontalHeaderLabels(COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for i in (0, 1, 2, 4):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.itemSelectionChanged.connect(self._sel)
        self._rows: dict[str, int] = {}

    def _sel(self):
        r = self.currentRow()
        if r >= 0:
            self.selected.emit(self.item(r, 0).text())

    def update_devices(self, devices: list[dict]) -> None:
        names = [d["name"] for d in devices]
        if names != list(self._rows):
            self.setRowCount(len(devices))
            self._rows = {n: i for i, n in enumerate(names)}
        for d in devices:
            r = self._rows[d["name"]]
            ident = d.get("identity") or {}
            status = d.get("status") or ""
            sim = " (simulated)" if d.get("simulated") and "simulated" not in status else ""
            kind = d["kind"] + ("" if d.get("physical") else " · logical")
            vals = [d["name"], kind, d["state"], status + sim,
                    d.get("busy_with") or ("yes" if d.get("busy") else ""),
                    " ".join(str(x) for x in (ident.get("model"), ident.get("serial") or "",
                                              ident.get("resource") or "") if x)]
            for c, v in enumerate(vals):
                it = self.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.setItem(r, c, it)
                it.setText(str(v))
            color = STATUS_COLORS.get(d["state"], "#cdd6f4")
            self.item(r, 2).setForeground(QColor(color))
            self.item(r, 2).setData(Qt.ItemDataRole.ToolTipRole, (d.get("last_error") or {}).get("message", ""))

    def set_state(self, name: str, state: str, status: str) -> None:
        r = self._rows.get(name)
        if r is None:
            return
        self.item(r, 2).setText(state)
        self.item(r, 2).setForeground(QColor(STATUS_COLORS.get(state, "#cdd6f4")))
        self.item(r, 3).setText(status)


class DevicePanel(QWidget):
    """Readbacks, manual controls, configuration and errors of one device."""

    message = pyqtSignal(str, str)

    def __init__(self, bridge_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        self.name: str | None = None
        self.info: dict | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        head = QHBoxLayout()
        self.title = QLabel("Select a device")
        self.title.setStyleSheet("color:#89b4fa;font-weight:bold;font-size:14px;")
        head.addWidget(self.title, 1)
        for text, slot in (("Connect", "connect"), ("Disconnect", "disconnect"), ("Reconnect", "reconnect")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, s=slot: self._lifecycle(s))
            head.addWidget(b)
        lay.addLayout(head)
        self.tabs = QTabWidget()
        # readbacks
        rb = QWidget()
        rl = QVBoxLayout(rb)
        self.attr_table = QTableWidget(0, 5)
        self.attr_table.setHorizontalHeaderLabels(["Attribute", "Value", "Unit", "Quality", "Access"])
        self.attr_table.verticalHeader().setVisible(False)
        self.attr_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.attr_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        rl.addWidget(self.attr_table)
        row = QHBoxLayout()
        b_read = QPushButton("Read all")
        b_read.clicked.connect(self.read_all)
        self.auto = QCheckBox("Auto refresh (2 s)")
        row.addWidget(b_read)
        row.addWidget(self.auto)
        row.addStretch(1)
        self.write_attr = QComboBox()
        self.write_val = QLineEdit()
        self.write_val.setPlaceholderText("value")
        b_write = QPushButton("Write")
        b_write.clicked.connect(self._write)
        row.addWidget(QLabel("Write"))
        row.addWidget(self.write_attr)
        row.addWidget(self.write_val)
        row.addWidget(b_write)
        rl.addLayout(row)
        self.tabs.addTab(rb, "Readbacks")
        # manual controls (commands)
        self.cmd_box = QWidget()
        self.cmd_lay = QVBoxLayout(self.cmd_box)
        self.tabs.addTab(self.cmd_box, "Manual control")
        # configuration
        self.cfg_view = QTextEdit()
        self.cfg_view.setReadOnly(True)
        self.tabs.addTab(self.cfg_view, "Configuration")
        # errors
        self.err_view = QTextEdit()
        self.err_view.setReadOnly(True)
        self.tabs.addTab(self.err_view, "Errors")
        lay.addWidget(self.tabs, 1)
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(lambda: self.auto.isChecked() and self.isVisible() and self.read_all())
        self._timer.start()

    def _client(self):
        b = self._bridge()
        return b.client if b else None

    def show_device(self, name: str) -> None:
        c = self._client()
        if c is None or not c.session:
            return
        self.name = name
        call_async(lambda: c.device(name).info(refresh=True), self._set_info,
                   lambda e: self.message.emit("error", f"{name}: {error_text(e)}"))

    def _set_info(self, info: dict) -> None:
        self.info = info
        ident = info.get("identity") or {}
        sim = "  — SIMULATED" if info.get("simulated") else ""
        self.title.setText(f"{info['name']}  ({info['kind']}){sim}  ·  {info['state']}: {info['status']}")
        attrs = info["attributes"]
        self.attr_table.setRowCount(len(attrs))
        self.write_attr.clear()
        for r, a in enumerate(attrs):
            for c, v in enumerate([a["name"], "—", a.get("unit", ""), "", a.get("access", "r")]):
                self.attr_table.setItem(r, c, QTableWidgetItem(str(v)))
            if a.get("access") == "rw":
                lim = ""
                if a.get("min") is not None or a.get("max") is not None:
                    lim = f" [{a.get('min')}, {a.get('max')}] {a.get('unit', '')}"
                self.write_attr.addItem(a["name"] + lim, a["name"])
        self.cfg_view.setPlainText(json.dumps({"identity": ident, "config": info.get("config")}, indent=2,
                                              default=str))
        err = info.get("last_error")
        self.err_view.setPlainText(json.dumps(err, indent=2, default=str) if err else "No error recorded.")
        self._build_commands(info)

    def _build_commands(self, info: dict) -> None:
        while self.cmd_lay.count():
            item = self.cmd_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        note = QLabel("Manual controls need control of the setup (the lease). The service rejects them "
                      "otherwise and while a measurement operation is running.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6c7086;")
        self.cmd_lay.addWidget(note)
        for cmd in info.get("commands", []):
            box = QGroupBox(cmd["name"])
            form = QFormLayout(box)
            widgets = {}
            for arg in cmd.get("args", []):
                if arg.get("choices"):
                    w = QComboBox()
                    w.addItems([str(x) for x in arg["choices"]])
                    if arg.get("default") is not None:
                        w.setCurrentText(str(arg["default"]))
                elif arg["dtype"] == "bool":
                    w = QCheckBox()
                    w.setChecked(bool(arg.get("default")))
                elif arg["dtype"] in ("float", "int"):
                    w = QDoubleSpinBox()
                    w.setDecimals(6 if arg["dtype"] == "float" else 0)
                    w.setRange(arg["min"] if arg.get("min") is not None else -1e9,
                               arg["max"] if arg.get("max") is not None else 1e9)
                    if arg.get("default") is not None:
                        w.setValue(float(arg["default"]))
                    if arg.get("unit"):
                        w.setSuffix(f" {arg['unit']}")
                else:
                    w = QLineEdit(str(arg.get("default") or ""))
                widgets[arg["name"]] = (w, arg)
                form.addRow(arg["name"], w)
            b = QPushButton(f"Run {cmd['name']}")
            b.setToolTip(cmd.get("description", ""))
            if cmd["name"] == "stop":
                b.setObjectName("abort_btn")
            b.clicked.connect(lambda _=False, c=cmd, ws=widgets: self._run_command(c, ws))
            form.addRow(b)
            if cmd.get("description"):
                d = QLabel(cmd["description"])
                d.setWordWrap(True)
                d.setStyleSheet("color:#6c7086;font-size:10px;")
                form.addRow(d)
            self.cmd_lay.addWidget(box)
        self.cmd_lay.addStretch(1)

    def _run_command(self, cmd: dict, widgets: dict) -> None:
        c = self._client()
        if c is None or self.name is None:
            return
        args = {}
        for name, (w, arg) in widgets.items():
            if isinstance(w, QComboBox):
                args[name] = w.currentText()
            elif isinstance(w, QCheckBox):
                args[name] = w.isChecked()
            elif isinstance(w, QDoubleSpinBox):
                args[name] = int(w.value()) if arg["dtype"] == "int" else w.value()
            else:
                args[name] = w.text()
        dev = self.name
        call_async(lambda: c.device(dev).command(cmd["name"], **args),
                   lambda r: self.message.emit("info", f"{dev}.{cmd['name']}: {_short(r)}"),
                   lambda e: self.message.emit("error", f"{dev}.{cmd['name']}: {error_text(e)}"))

    def read_all(self) -> None:
        c = self._client()
        if c is None or not c.session or self.info is None:
            return
        dev = self.name
        names = [a["name"] for a in self.info["attributes"]]

        def read():
            out = {}
            for n in names:
                try:
                    out[n] = c.device(dev).read(n, timeout_s=15.0)[n]
                except Exception as exc:  # noqa: BLE001
                    out[n] = exc
            return out
        call_async(read, self._show_readings)

    def _show_readings(self, readings: dict) -> None:
        for r in range(self.attr_table.rowCount()):
            name = self.attr_table.item(r, 0).text()
            v = readings.get(name)
            if v is None:
                continue
            if isinstance(v, Exception):
                self.attr_table.item(r, 1).setText(f"error: {error_text(v)}")
                self.attr_table.item(r, 3).setText("ERROR")
                continue
            val = v.value
            if isinstance(val, float):
                txt = "—" if math.isnan(val) else f"{val:.6g}"
            elif isinstance(val, dict):
                txt = ", ".join(f"{k}={x:.5g}" if isinstance(x, float) else f"{k}={x}" for k, x in val.items())
            else:
                txt = "—" if val is None else str(val)
            self.attr_table.item(r, 1).setText(txt)
            q = v.quality.value + (f" ({v.age_s:.0f} s)" if v.quality.value == "STALE" else "")
            self.attr_table.item(r, 3).setText(q)

    def _write(self) -> None:
        c = self._client()
        attr = self.write_attr.currentData()
        if c is None or not attr:
            return
        raw = self.write_val.text().strip()
        try:
            value = float(raw)
        except ValueError:
            self.message.emit("error", f"{raw!r} is not a number")
            return
        dev = self.name
        call_async(lambda: c.device(dev).write(attr, value),
                   lambda r: (self.message.emit("info", f"{dev}.{attr} = {r}"), self.read_all()),
                   lambda e: self.message.emit("error", f"{dev}.{attr}: {error_text(e)}"))

    def _lifecycle(self, action: str) -> None:
        c = self._client()
        if c is None or self.name is None:
            return
        dev = self.name
        call_async(lambda: getattr(c.device(dev), action)(),
                   lambda info: (self.message.emit("info", f"{dev}: {action} -> {info['state']}"),
                                 self._set_info(info)),
                   lambda e: self.message.emit("error", f"{dev} {action}: {error_text(e)}"))


def _short(r) -> str:
    try:
        s = json.dumps(r, default=str)
    except (TypeError, ValueError):
        s = str(r)
    return s if len(s) < 160 else s[:157] + "…"
