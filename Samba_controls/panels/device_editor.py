"""Device settings: the instrument part of machine.toml, edited in a form.

* Every instrument: display name (the id that measurements use -- ``daq``,
  ``lockin``, ``hall``, ``stage_<key>`` -- never changes), enabled, start order,
  and its connection settings (address, serial numbers, port, ...).
* Stages: more stages of a type that has a driver (Thorlabs Kinesis, Trinamic
  TMCM-3212) can be added, and added ones removed.  Samba_mini's two setup
  stages can be disabled but not removed.  A new instrument *type* needs a
  driver in code (docs/adding-a-device.md).

Only administrators can save; the service checks that, validates the result,
refuses to overwrite changes made to the file meanwhile, and backs the old file
up.  The running service keeps its settings until it is restarted (the window
offers to restart it).  Unsaved edits stay in the form; a failed save says why
and keeps them.
"""

from __future__ import annotations

import copy
import json

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSplitter, QVBoxLayout,
                             QWidget)

from core.config import machine
from core.scan import channels as _channels
from core.gui.bridge import error_text
from core.protocol import ServiceError

FIRST = ("label", "enabled", "start_order")
#: Edited in the Channels tab (names and physical lines), not here.
HIDDEN = {"channels"} | {c.wiring for c in _channels.ALL if c.wiring}
HELP = {
    "label": "Display name shown in Samba_controls; measurements keep using the device id",
    "enabled": "Disabled instruments are not connected at start",
    "start_order": "Instruments connect one after another at service start, lowest number first",
    "resource": "VISA resource, e.g. TCPIP0::<IP>::50000::SOCKET or USB0::0x0A2D::0x001B::<serial>::RAW",
    "dll_path": "64-bit MagnetPhysik.Usb.dll (not a downloaded copy)",
    "port": "COM port, or auto",
    "driver": "Stage driver (fixed for an existing stage)",
}


def _section_ids(devices: dict) -> list[str]:
    ids = [s for s in machine.DEVICE_SECTIONS if s in devices]
    ids += [f"stages.{k}" for k in (devices.get("stages") or {})]
    return ids


def _device_id(section: str) -> str:
    return f"stage_{section.split('.', 1)[1]}" if section.startswith("stages.") else section


class DeviceEditor(QWidget):
    message = pyqtSignal(str, str)
    saved = pyqtSignal()

    def __init__(self, bridge_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        self._orig: dict = {}            # device sections as read
        self._edits: dict = {}           # device sections being edited
        self._hash: str | None = None
        self._errors: dict[tuple[str, str], str] = {}
        self._admin = False
        self._section: str | None = None
        lay = QVBoxLayout(self)
        self.head = QLabel("Log in to see the device settings.")
        self.head.setWordWrap(True)
        self.head.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.head)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda cur, _prev: self._show(cur.data(Qt.ItemDataRole.UserRole)
                                                                           if cur else None))
        lv.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.add_btn = QPushButton("Add stage…")
        self.add_btn.setToolTip("Another stage of a type that has a driver (Thorlabs Kinesis, Trinamic TMCM-3212)")
        self.add_btn.clicked.connect(self._add_stage)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setToolTip("Remove an added stage (the two setup stages can only be disabled)")
        self.remove_btn.clicked.connect(self._remove_stage)
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
        lv.addLayout(row)
        split.addWidget(left)
        self.form_host = QScrollArea()
        self.form_host.setWidgetResizable(True)
        split.addWidget(self.form_host)
        split.setSizes([260, 640])
        lay.addWidget(split, 1)
        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(lambda: self.refresh(ask=True))
        self.save_btn = QPushButton("Save device settings")
        self.save_btn.setObjectName("start_btn")
        self.save_btn.clicked.connect(self.save)
        bottom.addWidget(self.reload_btn)
        bottom.addWidget(self.save_btn)
        lay.addLayout(bottom)
        self._update_buttons()

    # ------------------------------------------------------------------ data
    def _client(self):
        b = self._bridge()
        return b.client if b else None

    def is_dirty(self) -> bool:
        return json.dumps(self._edits, sort_keys=True) != json.dumps(self._orig, sort_keys=True)

    def refresh(self, ask: bool = False) -> bool:
        if ask and self.is_dirty() and QMessageBox.question(
                self, "Device settings", "Discard the unsaved device settings and reload them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return False
        c = self._client()
        if c is None or not c.session:
            self._set_data({}, None, False)
            self.head.setText("Log in to see the device settings (administrators can change them).")
            return False
        try:
            res = c.call("config.get", {}, 10.0)
        except ServiceError as exc:
            self.head.setText(f"<span style='color:#f38ba8'>{error_text(exc)}</span>")
            return False
        if "file" not in res:
            self.head.setText("<span style='color:#fab387'>The running device service is older than this window; "
                              "restart it to edit device settings here.</span>")
            self._set_data({}, None, False)
            return False
        self._admin = (c.user or {}).get("role") == "admin"
        devices = {k: copy.deepcopy(res["file"][k]) for k in (*machine.DEVICE_SECTIONS, "stages") if k in res["file"]}
        self._set_data(devices, res.get("file_hash"), self._admin)
        probs = res.get("file_problems") if res.get("file_status") == "invalid" else []
        head = f"<b>{res['path']}</b>"
        if res.get("restart_required"):
            head += ("<br><span style='color:#fab387'>Saved settings differ from what the running service uses: "
                     "restart the service to apply them.</span>")
        if probs:
            head += "<br><span style='color:#f38ba8'>" + "<br>".join(probs) + "</span>"
        if not self._admin:
            head += "<br><span style='color:#6c7086'>Read-only: log in as an administrator to change them.</span>"
        self.head.setText(head)
        return True

    def _set_data(self, devices: dict, file_hash: str | None, editable: bool) -> None:
        self._orig = copy.deepcopy(devices)
        self._edits = copy.deepcopy(devices)
        self._hash = file_hash
        self._errors.clear()
        self._admin = editable
        self._fill_list()
        self.status.setText("")
        self._update_buttons()

    def _fill_list(self, select: str | None = None) -> None:
        current = select or self._section
        self.list.blockSignals(True)
        self.list.clear()
        for sec in _section_ids(self._edits):
            data = self._get(sec)
            dev = _device_id(sec)
            it = QListWidgetItem(f"{data.get('label') or dev}\n{dev}"
                                 + ("" if data.get("enabled", True) else "  · disabled"))
            it.setData(Qt.ItemDataRole.UserRole, sec)
            self.list.addItem(it)
            if sec == current:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        sec = self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else None
        self._show(sec)

    def _get(self, sec: str) -> dict:
        if sec.startswith("stages."):
            return self._edits["stages"][sec.split(".", 1)[1]]
        return self._edits[sec]

    # ------------------------------------------------------------------ form
    def _show(self, sec: str | None) -> None:
        self._section = sec
        w = QWidget()
        form = QFormLayout(w)
        if sec is None:
            form.addRow(QLabel("No device"))
            self.form_host.setWidget(w)
            self._update_buttons()
            return
        data = self._get(sec)
        dev = _device_id(sec)
        title = QLabel(f"<b>{data.get('label') or dev}</b> — device id <code>{dev}</code>")
        title.setTextFormat(Qt.TextFormat.RichText)
        form.addRow(title)
        keys = [k for k in FIRST if k in data] + [k for k in data if k not in FIRST and k not in HIDDEN]
        for key in keys:
            form.addRow(key, self._field(sec, key, data[key]))
        if any(k in data for k in HIDDEN):
            ch_note = QLabel("Channel names and their physical lines: Channels tab.")
            ch_note.setStyleSheet("color:#89b4fa;font-size:10px;")
            form.addRow(ch_note)
        self.fields_note = QLabel("Settings apply after the device service restarts.")
        self.fields_note.setStyleSheet("color:#6c7086;font-size:10px;")
        form.addRow(self.fields_note)
        self.form_host.setWidget(w)
        self._update_buttons()

    def _field(self, sec: str, key: str, value):
        editable = self._admin and not (key == "driver")
        if isinstance(value, bool):
            wdg = QCheckBox()
            wdg.setChecked(value)
            wdg.toggled.connect(lambda on, s=sec, k=key: self._set(s, k, bool(on)))
        else:
            text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            wdg = QLineEdit(text)
            kind = ("int" if isinstance(value, int) else "float" if isinstance(value, float) else
                    "json" if isinstance(value, (dict, list)) else "str")
            wdg.textChanged.connect(lambda t, s=sec, k=key, kd=kind, w=wdg: self._parse(s, k, kd, t, w))
        wdg.setEnabled(editable)
        wdg.setToolTip(HELP.get(key, ""))
        wdg.setObjectName(f"field:{key}")
        return wdg

    def _parse(self, sec: str, key: str, kind: str, text: str, wdg: QLineEdit) -> None:
        try:
            if kind == "int":
                value = int(text.strip())
            elif kind == "float":
                value = float(text.strip().replace(",", "."))
            elif kind == "json":
                value = json.loads(text) if text.strip() else None
            else:
                value = text
            self._errors.pop((sec, key), None)
            wdg.setStyleSheet("")
            self._set(sec, key, value)
        except ValueError:
            self._errors[(sec, key)] = f"{_device_id(sec)}.{key}: not a valid {kind}"
            wdg.setStyleSheet("border:1px solid #f38ba8;")
            self._update_buttons()

    def _set(self, sec: str, key: str, value) -> None:
        self._get(sec)[key] = value
        if key in ("label", "enabled"):
            it = self.list.currentItem()
            if it is not None:
                data = self._get(sec)
                dev = _device_id(sec)
                it.setText(f"{data.get('label') or dev}\n{dev}" + ("" if data.get("enabled", True) else "  · disabled"))
        self._update_buttons()

    def set_value(self, device: str, key: str, value) -> None:
        """Programmatic edit (as the form does), by device id."""
        sec = f"stages.{device[len('stage_'):]}" if device.startswith("stage_") else device
        self._get(sec)[key] = value
        self._fill_list(select=sec)
        self._update_buttons()

    # ------------------------------------------------------------------ stages
    def _add_stage(self) -> None:
        drivers = list(machine.STAGE_DRIVERS)
        labels = [f"{d} — {machine.STAGE_DRIVERS[d]}" for d in drivers]
        choice, ok = QInputDialog.getItem(self, "Add stage", "Type (driver):", labels, 0, False)
        if not ok:
            return
        driver = drivers[labels.index(choice)]
        key, ok = QInputDialog.getText(self, "Add stage", "Name (lower case, e.g. micro):")
        if ok and key.strip():
            self.add_stage(key.strip(), driver)

    def add_stage(self, key: str, driver: str) -> bool:
        stages = self._edits.setdefault("stages", {})
        if not machine.STAGE_KEY_RE.match(key):
            self.status.setText(f"<span style='color:#f38ba8'>{key!r}: lower-case letters, digits and '_', "
                                "starting with a letter</span>")
            return False
        if key in stages:
            self.status.setText(f"<span style='color:#f38ba8'>A stage named {key!r} already exists.</span>")
            return False
        st = machine.stage_template(driver, key)
        st["start_order"] = max([s.get("start_order", 0) for s in stages.values()] + [50]) + 10
        stages[key] = st
        self._fill_list(select=f"stages.{key}")
        self.status.setText(f"Added stage_{key} ({machine.STAGE_DRIVERS[driver]}): fill in its serial numbers / "
                            "port, then Save.")
        return True

    def _remove_stage(self) -> None:
        sec = self._section
        if not sec or not sec.startswith("stages."):
            return
        key = sec.split(".", 1)[1]
        if key in machine.SETUP_STAGES:
            QMessageBox.information(self, "Remove stage", f"stage_{key} is used by Samba_mini's {key} setup; "
                                    "untick 'enabled' to switch it off instead.")
            return
        if QMessageBox.question(self, "Remove stage", f"Remove stage_{key} from the configuration?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.remove_stage(key)

    def remove_stage(self, key: str) -> bool:
        if key in machine.SETUP_STAGES or key not in self._edits.get("stages", {}):
            return False
        del self._edits["stages"][key]
        self._section = None
        self._fill_list()
        self._update_buttons()
        return True

    # ------------------------------------------------------------------ save
    def save(self) -> bool:
        c = self._client()
        if c is None or not c.session:
            self.status.setText("<span style='color:#f38ba8'>Log in as an administrator to save.</span>")
            return False
        if self._errors:
            self.status.setText("<span style='color:#f38ba8'>Fix: " + "; ".join(self._errors.values()) + "</span>")
            return False
        try:
            res = c.call("config.save_devices", {"devices": self._edits, "base_hash": self._hash}, 15.0)
        except ServiceError as exc:
            self.status.setText(f"<span style='color:#f38ba8'>Not saved: {exc.message} — your changes are still "
                                "in the form.</span>")
            self.message.emit("error", f"device settings not saved: {error_text(exc)}")
            return False
        self._orig = copy.deepcopy(self._edits)
        self._hash = res.get("file_hash")
        self.status.setText("<span style='color:#a6e3a1'>Saved (the previous file is in config\\backups). "
                            "Restart the device service to apply.</span>")
        self.message.emit("info", f"Device settings saved to {res.get('path')}; restart the service to apply.")
        self._update_buttons()
        self.saved.emit()
        return True

    def _update_buttons(self) -> None:
        dirty = self.is_dirty()
        sec = self._section or ""
        self.save_btn.setEnabled(self._admin and dirty and not self._errors)
        self.add_btn.setEnabled(self._admin)
        self.remove_btn.setEnabled(self._admin and sec.startswith("stages.")
                                   and sec.split(".", 1)[1] not in machine.SETUP_STAGES)
        self.save_btn.setText("Save device settings" + (" •" if dirty else ""))
