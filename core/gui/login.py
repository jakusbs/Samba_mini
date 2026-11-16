"""Login / first-administrator dialog shared by both applications."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox,
                             QVBoxLayout)

from core.gui.bridge import ServiceBridge, error_text
from core.protocol import ServiceError


class LoginDialog(QDialog):
    def __init__(self, bridge: ServiceBridge, app_name: str, parent=None, username: str = ""):
        super().__init__(parent)
        self.bridge = bridge
        self.setWindowTitle(f"{app_name} — log in")
        self.setMinimumWidth(380)
        info = bridge.hello_info or bridge.probe() or {}
        self.bootstrap = bool(info.get("needs_bootstrap"))
        lay = QVBoxLayout(self)
        head = QLabel("<b>Create the first administrator account</b><br>No accounts exist yet on this PC."
                      if self.bootstrap else f"<b>{app_name}</b> — local account")
        head.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(head)
        form = QFormLayout()
        self.user = QLineEdit(username)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("User name", self.user)
        form.addRow("Password", self.pw)
        self.pw2 = None
        if self.bootstrap:
            self.pw2 = QLineEdit()
            self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("Repeat password", self.pw2)
        lay.addLayout(form)
        self.msg = QLabel("")
        self.msg.setStyleSheet("color:#f38ba8;")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        note = QLabel("Accounts organise control of the shared setup and record who measured. "
                      "They do not protect files from other Windows users or administrators.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6c7086;font-size:10px;")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        (self.pw if username else self.user).setFocus()

    def _accept(self):
        name, pw = self.user.text().strip(), self.pw.text()
        try:
            if self.bootstrap:
                if pw != (self.pw2.text() if self.pw2 else pw):
                    self.msg.setText("The passwords differ.")
                    return
                self.bridge.client.bootstrap(name, pw)
                QMessageBox.information(self, "Account created",
                                        f"Administrator '{name}' created. You are now logged in.")
            self.bridge.client.login(name, pw)
        except ServiceError as exc:
            self.msg.setText(error_text(exc))
            return
        self.accept()
