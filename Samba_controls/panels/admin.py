"""Users (administrators only) and machine configuration panels."""

from __future__ import annotations

import json
import os

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
                             QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
                             QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

from core import paths
from core.gui.bridge import error_text
from core.protocol import ServiceError


class UsersPanel(QWidget):
    message = pyqtSignal(str, str)

    def __init__(self, bridge_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["User", "Role", "Full name", "Disabled", "Last login"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for text, slot in (("Refresh", self.refresh), ("Add user…", self._add), ("Toggle admin", self._toggle_role),
                           ("Enable/disable", self._toggle_disabled), ("Reset password…", self._reset_pw)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.note = QLabel("Administrators manage accounts and shared settings; operators run measurements.")
        self.note.setStyleSheet("color:#6c7086;")
        lay.addWidget(self.note)
        self._users: list[dict] = []

    def _client(self):
        b = self._bridge()
        return b.client if b else None

    def refresh(self) -> None:
        c = self._client()
        if c is None or not c.session:
            return
        try:
            self._users = c.call("auth.list_users")
        except ServiceError as exc:
            self.note.setText(error_text(exc))
            self.table.setRowCount(0)
            return
        self.table.setRowCount(len(self._users))
        import time
        for r, u in enumerate(self._users):
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(u["last_login"])) if u.get("last_login") else ""
            for c_, v in enumerate([u["username"], u["role"], u.get("full_name", ""),
                                    "yes" if u["disabled"] else "", last]):
                self.table.setItem(r, c_, QTableWidgetItem(str(v)))

    def _selected(self) -> dict | None:
        r = self.table.currentRow()
        return self._users[r] if 0 <= r < len(self._users) else None

    def _call(self, method, params, ok_msg):
        try:
            self._client().call(method, params, 15.0)
            self.message.emit("info", ok_msg)
        except ServiceError as exc:
            self.message.emit("error", error_text(exc))
        self.refresh()

    def _add(self):
        name, ok = QInputDialog.getText(self, "Add user", "User name:")
        if not ok or not name.strip():
            return
        pw, ok = QInputDialog.getText(self, "Add user", "Initial password (≥ 8 characters):",
                                      QLineEdit.EchoMode.Password)
        if not ok:
            return
        role, ok = QInputDialog.getItem(self, "Add user", "Role:", ["operator", "admin"], 0, False)
        if not ok:
            return
        self._call("auth.create_user", {"username": name.strip(), "password": pw, "role": role},
                   f"user {name.strip()} created")

    def _toggle_role(self):
        u = self._selected()
        if u:
            self._call("auth.update_user", {"user_id": u["id"], "role": "operator" if u["role"] == "admin" else "admin"},
                       f"role of {u['username']} changed")

    def _toggle_disabled(self):
        u = self._selected()
        if u:
            self._call("auth.update_user", {"user_id": u["id"], "disabled": not u["disabled"]},
                       f"{u['username']} {'enabled' if u['disabled'] else 'disabled'}")

    def _reset_pw(self):
        u = self._selected()
        if not u:
            return
        pw, ok = QInputDialog.getText(self, "Reset password", f"New password for {u['username']}:",
                                      QLineEdit.EchoMode.Password)
        if ok:
            self._call("auth.update_user", {"user_id": u["id"], "password": pw}, "password reset")


class ConfigPanel(QWidget):
    message = pyqtSignal(str, str)

    def __init__(self, bridge_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        lay = QVBoxLayout(self)
        self.head = QLabel("")
        self.head.setWordWrap(True)
        lay.addWidget(self.head)
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        b_ref = QPushButton("Refresh")
        b_ref.clicked.connect(self.refresh)
        b_open = QPushButton("Open configuration folder")
        b_open.clicked.connect(lambda: os.startfile(str(paths.config_dir())) if hasattr(os, "startfile") else None)
        b_imp = QPushButton("Import MiniMoke settings…")
        b_imp.clicked.connect(self._import)
        self.mode = QComboBox()
        self.mode.addItems(["keep mode", "real", "simulation"])
        row.addWidget(b_ref)
        row.addWidget(b_open)
        row.addWidget(b_imp)
        row.addWidget(QLabel("Mode after import:"))
        row.addWidget(self.mode)
        row.addStretch(1)
        lay.addLayout(row)
        note = QLabel("Machine wiring, addresses, limits and calibration are shared by everyone on this PC. "
                      "Edit machine.toml (a backup is kept in config\\backups) and restart the service. "
                      "The MiniMoke import only reads the MiniMoke folder.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6c7086;")
        lay.addWidget(note)

    def refresh(self) -> None:
        b = self._bridge()
        if b is None or not b.client.session:
            from core.config import machine
            cfg = machine.load()
            self.head.setText(f"<b>{cfg.path}</b> — {cfg.status} (local file; service not connected)")
            self.view.setPlainText(json.dumps(cfg.data, indent=2))
            return
        try:
            res = b.client.call("config.get")
        except ServiceError as exc:
            self.head.setText(error_text(exc))
            return
        probs = "<br>".join(f"<span style='color:#fab387'>{p}</span>" for p in res.get("problems") or [])
        self.head.setText(f"<b>{res['path']}</b> — status {res['status']} — service mode <b>{res['mode']}</b>"
                          + (f"<br>{probs}" if probs else ""))
        self.view.setPlainText(json.dumps(res["data"], indent=2))

    def _import(self) -> None:
        b = self._bridge()
        if b is None or not b.client.session or (b.client.user or {}).get("role") != "admin":
            QMessageBox.warning(self, "Import", "Log in as an administrator to change the machine configuration.")
            return
        src = QFileDialog.getExistingDirectory(self, "MiniMoke folder (contains configs\\)",
                                               os.path.expanduser("~\\Documents"))
        if not src:
            return
        from core.config import minimoke_import
        try:
            res = minimoke_import.read_minimoke(src)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Import", str(exc))
            return
        summary = json.dumps(res["machine"], indent=2) + "\n\nStage offsets: " + json.dumps(res["offsets"]) \
            + "\n\nNotes:\n" + "\n".join(res["notes"]) + f"\n\nPresets: {', '.join(sorted(res['presets']))}"
        box = QMessageBox(QMessageBox.Icon.Question, "Import MiniMoke settings",
                          "Write these values into machine.toml (a backup is made) and stage offsets?\n"
                          "Your MiniMoke procedure settings will be saved as presets in your account.",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDetailedText(summary)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        mode = None if self.mode.currentIndex() == 0 else self.mode.currentText()
        try:
            written = minimoke_import.apply(res, mode)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Import", f"Could not write the configuration: {exc}")
            return
        for key, values in res["presets"].items():
            for setup in ("longitudinal", "polar"):
                try:
                    b.client.call("presets.save", {"workflow": f"samba_mini/{setup}",
                                                   "name": f"MiniMoke {key}",
                                                   "value": {"workflow": key, "params": values}})
                except ServiceError as exc:
                    self.message.emit("warning", f"preset {key}: {error_text(exc)}")
        self.message.emit("info", f"Imported MiniMoke settings: {written}. Restart the service to apply.")
        self.refresh()
