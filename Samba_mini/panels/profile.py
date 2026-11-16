"""Profile tab: per-user preferences stored by the service (data folder,
sample/operator defaults, lab notebook, CSV export) and password change."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from core import paths
from core.gui.bridge import error_text
from core.protocol import ServiceError


class ProfilePanel(QWidget):
    saved = pyqtSignal(dict)
    message = pyqtSignal(str, str)

    def __init__(self, bridge_getter, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        lay = QHBoxLayout(self)
        box = QGroupBox("My preferences")
        g = QGridLayout(box)
        self.user_lbl = QLabel("Not logged in")
        self.user_lbl.setStyleSheet("color:#89b4fa;font-weight:bold;")
        g.addWidget(self.user_lbl, 0, 0, 1, 3)
        self.data_dir = QLineEdit(str(paths.default_measurement_dir()))
        browse = QPushButton("…")
        browse.setFixedWidth(30)
        browse.clicked.connect(self._browse)
        self.operator = QLineEdit()
        self.sample = QLineEdit()
        self.notebook = QLineEdit()
        self.notebook.setPlaceholderText("<data folder>\\lab_notebook_SambaMini.csv")
        self.csv = QCheckBox("Export CSV after every run")
        g.addWidget(QLabel("Data folder"), 1, 0)
        g.addWidget(self.data_dir, 1, 1)
        g.addWidget(browse, 1, 2)
        g.addWidget(QLabel("Default operator"), 2, 0)
        g.addWidget(self.operator, 2, 1)
        g.addWidget(QLabel("Default sample"), 3, 0)
        g.addWidget(self.sample, 3, 1)
        g.addWidget(QLabel("Lab notebook CSV"), 4, 0)
        g.addWidget(self.notebook, 4, 1)
        g.addWidget(self.csv, 5, 1)
        b_save = QPushButton("Save preferences")
        b_save.clicked.connect(self.save)
        b_pw = QPushButton("Change password…")
        b_pw.clicked.connect(self._change_password)
        g.addWidget(b_save, 6, 1)
        g.addWidget(b_pw, 7, 1)
        lay.addWidget(box, 2)
        note = QLabel("Preferences and named configurations are stored per account by the device service "
                      "on this PC. Machine wiring, instrument addresses, limits and calibration are shared "
                      "settings edited by an administrator in Samba_controls.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6c7086;")
        lay.addWidget(note, 1)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Data folder", self.data_dir.text())
        if d:
            self.data_dir.setText(d)

    def load(self, user: dict | None, prefs: dict) -> None:
        self.user_lbl.setText(f"{user['username']} ({user['role']})" if user else "Not logged in")
        self.data_dir.setText(prefs.get("data_dir") or str(paths.default_measurement_dir()))
        self.operator.setText(prefs.get("default_operator", user.get("full_name", "") if user else ""))
        self.sample.setText(prefs.get("default_sample", ""))
        self.notebook.setText(prefs.get("lab_notebook", ""))
        self.csv.setChecked(bool(prefs.get("export_csv", False)))

    def values(self) -> dict:
        return {"data_dir": self.data_dir.text().strip(), "default_operator": self.operator.text().strip(),
                "default_sample": self.sample.text().strip(), "lab_notebook": self.notebook.text().strip(),
                "export_csv": self.csv.isChecked()}

    def save(self) -> None:
        b = self._bridge()
        if not b or not b.client.session:
            self.message.emit("warning", "Log in to save preferences.")
            return
        try:
            prefs = b.client.call("prefs.set", {"values": self.values()})
        except ServiceError as exc:
            self.message.emit("error", f"preferences not saved: {error_text(exc)}")
            return
        self.message.emit("info", "Preferences saved.")
        self.saved.emit(prefs)

    def _change_password(self):
        b = self._bridge()
        if not b or not b.client.session:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Change password")
        f = QFormLayout(dlg)
        old, new, rep = QLineEdit(), QLineEdit(), QLineEdit()
        for w in (old, new, rep):
            w.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Current password", old)
        f.addRow("New password", new)
        f.addRow("Repeat", rep)
        msg = QLabel("")
        msg.setStyleSheet("color:#f38ba8;")
        f.addRow(msg)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        f.addRow(bb)

        def accept():
            if new.text() != rep.text():
                msg.setText("The new passwords differ.")
                return
            try:
                b.client.call("auth.change_password", {"old_password": old.text(), "new_password": new.text()},
                              15.0)
            except ServiceError as exc:
                msg.setText(error_text(exc))
                return
            dlg.accept()
            self.message.emit("info", "Password changed.")
        bb.accepted.connect(accept)
        bb.rejected.connect(dlg.reject)
        dlg.exec()
