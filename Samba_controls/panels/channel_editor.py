"""Channels: name every measured / set quantity and choose its physical line.

One row per channel of the DAQ, the lock-in and the Hall probe:

* **Name** -- what Samba_mini shows in its sensor rows and plots, and the name
  the measurement files save it under (``/data/<name>``, as in Samba).
* **Access** -- *read* channels (sensors) are what a measurement records;
  *read/write* channels (outputs: the magnet coil command, AO1, AO2) are set by
  measurements and the Hardware box.
* **Line** -- for the DAQ, the physical input/output (``Dev1/ai0`` ...); the
  lock-in and Hall channels have no line to choose.
* Unit, the fixed channel id the software uses, and the dataset name.

Only administrators can save (through the service, validated: names unique and
not used by the files themselves; refused when machine.toml changed meanwhile;
old file backed up).  Names apply after the device service restarts.
"""

from __future__ import annotations

import copy

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.gui.bridge import error_text
from core.protocol import ServiceError
from core.scan import channels as chans

COLS = ["Device", "Name", "Access", "Line", "Unit", "Id", "Saved as"]
C_DEV, C_NAME, C_ACCESS, C_LINE, C_UNIT, C_ID, C_DS = range(len(COLS))
SECTIONS = ("daq", "lockin", "hall")


class ChannelEditor(QWidget):
    message = pyqtSignal(str, str)
    saved = pyqtSignal()

    def __init__(self, bridge_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        self._orig: dict = {}
        self._edits: dict = {}
        self._hash: str | None = None
        self._admin = False
        self._filling = False
        lay = QVBoxLayout(self)
        self.head = QLabel("Log in to see the channels.")
        self.head.setWordWrap(True)
        self.head.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.head)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C_NAME, QHeaderView.ResizeMode.Stretch)
        for c in (C_DEV, C_ACCESS, C_LINE, C_UNIT, C_ID, C_DS):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self._edited)
        lay.addWidget(self.table, 1)
        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet("color:#f38ba8;")
        lay.addWidget(self.problems)
        row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(lambda: self.refresh(ask=True))
        self.save_btn = QPushButton("Save channels")
        self.save_btn.setObjectName("start_btn")
        self.save_btn.clicked.connect(self.save)
        row.addWidget(self.reload_btn)
        row.addWidget(self.save_btn)
        lay.addLayout(row)
        note = QLabel("Read channels are what Samba_mini can measure and save (tick them in its sensor rows); "
                      "read/write channels are set by measurements and the Hardware box. Names apply after the "
                      "device service restarts; files save each channel under its name, with the channel id "
                      "as an attribute.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6c7086;font-size:10px;")
        lay.addWidget(note)
        self._update_buttons()

    # ------------------------------------------------------------------ data
    def _client(self):
        b = self._bridge()
        return b.client if b else None

    def is_dirty(self) -> bool:
        return self._edits != self._orig

    def refresh(self, ask: bool = False) -> bool:
        if ask and self.is_dirty() and QMessageBox.question(
                self, "Channels", "Discard the unsaved channel names and reload them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return False
        c = self._client()
        if c is None or not c.session:
            self._set({}, None, False)
            self.head.setText("Log in to see the channels (administrators can rename them).")
            return False
        try:
            res = c.call("config.get", {}, 10.0)
        except ServiceError as exc:
            self.head.setText(f"<span style='color:#f38ba8'>{error_text(exc)}</span>")
            return False
        if "file" not in res:
            self.head.setText("<span style='color:#fab387'>The running device service is older than this window; "
                              "restart it to edit the channels here.</span>")
            self._set({}, None, False)
            return False
        admin = (c.user or {}).get("role") == "admin"
        self._set({k: copy.deepcopy(res["file"][k]) for k in SECTIONS if k in res["file"]},
                  res.get("file_hash"), admin)
        running = {r["id"]: r["name"] for r in chans.channel_table(res["data"])}
        stale = [r["id"] for r in chans.channel_table(self._edits) if running.get(r["id"]) != r["name"]]
        head = "Channels of the DAQ, the lock-in and the Hall probe."
        if stale:
            head += ("<br><span style='color:#fab387'>Saved names differ from what the running service uses: "
                     "restart the service to apply them.</span>")
        if not admin:
            head += "<br><span style='color:#6c7086'>Read-only: log in as an administrator to rename.</span>"
        self.head.setText(head)
        return True

    def _set(self, sections: dict, file_hash: str | None, admin: bool) -> None:
        self._orig = copy.deepcopy(sections)
        self._edits = copy.deepcopy(sections)
        self._hash = file_hash
        self._admin = admin
        self._fill()
        self.status.setText("")

    def _fill(self) -> None:
        self._filling = True
        try:
            rows = chans.channel_table(self._edits) if self._edits else []
            self.table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                vals = [row["device_label"], row["name"], "read" if row["access"] == "r" else "read / write",
                        row["line"] or "—", row["unit"], row["id"], f"/data/{row['dataset']}"
                        if row["kind"] == "sensor" else "(set-point)"]
                for c, v in enumerate(vals):
                    it = QTableWidgetItem(str(v))
                    editable = self._admin and (c == C_NAME or (c == C_LINE and row["wiring"]))
                    flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                    if editable:
                        flags |= Qt.ItemFlag.ItemIsEditable
                    it.setFlags(flags)
                    if c == C_ID:
                        it.setData(Qt.ItemDataRole.UserRole, (row["device"], row["id"], row["wiring"]))
                    if c == C_ACCESS:
                        it.setForeground(QColor("#a6e3a1" if row["access"] == "r" else "#89b4fa"))
                    self.table.setItem(r, c, it)
        finally:
            self._filling = False
        self._check()

    def _edited(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        r, c = item.row(), item.column()
        dev, cid, wiring = self.table.item(r, C_ID).data(Qt.ItemDataRole.UserRole)
        text = item.text().strip()
        if c == C_NAME:
            self._edits[dev].setdefault("channels", {})[cid] = text
            self._filling = True
            self.table.item(r, C_DS).setText(f"/data/{chans.dataset_key(text)}"
                                             if chans.BY_ID[cid].access == "r" else "(set-point)")
            self._filling = False
        elif c == C_LINE and wiring:
            self._edits[dev][wiring] = text
        self._check()

    def set_name(self, channel_id: str, name: str) -> None:
        """Programmatic rename (as editing the Name cell)."""
        for r in range(self.table.rowCount()):
            if self.table.item(r, C_ID).text() == channel_id:
                self.table.item(r, C_NAME).setText(name)
                return
        raise KeyError(channel_id)

    def _check(self) -> list[str]:
        probs = chans.validate_names(self._edits) if self._edits else []
        for dev in SECTIONS:
            for cid in chans.DEVICE_CHANNELS[dev]:
                ch = chans.BY_ID[cid]
                if ch.wiring and not str((self._edits.get(dev) or {}).get(ch.wiring, "x")).strip():
                    probs.append(f"{dev}.{ch.wiring}: the physical line of {cid} is empty")
        self.problems.setText("<br>".join(probs))
        self._update_buttons(bool(probs))
        return probs

    # ------------------------------------------------------------------ save
    def save(self) -> bool:
        c = self._client()
        if c is None or not c.session:
            self.status.setText("<span style='color:#f38ba8'>Log in as an administrator to save.</span>")
            return False
        if self._check():
            self.status.setText("<span style='color:#f38ba8'>Fix the problems above first.</span>")
            return False
        try:
            res = c.call("config.save_devices", {"devices": self._edits, "base_hash": self._hash}, 15.0)
        except ServiceError as exc:
            self.status.setText(f"<span style='color:#f38ba8'>Not saved: {exc.message} — your changes are still "
                                "in the table.</span>")
            self.message.emit("error", f"channels not saved: {error_text(exc)}")
            return False
        self._orig = copy.deepcopy(self._edits)
        self._hash = res.get("file_hash")
        self.status.setText("<span style='color:#a6e3a1'>Saved (the previous file is in config\\backups). "
                            "Restart the device service to apply.</span>")
        self.message.emit("info", "Channel names saved; restart the service to apply.")
        self._update_buttons()
        self.saved.emit()
        return True

    def _update_buttons(self, problems: bool = False) -> None:
        dirty = self.is_dirty()
        self.save_btn.setEnabled(self._admin and dirty and not problems)
        self.save_btn.setText("Save channels" + (" •" if dirty else ""))
