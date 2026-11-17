"""Profile tab: the logged-in account's preferences and password.

Preferences are saved automatically (a moment after each change) by the device
service, per account; the line at the top says whether everything is saved,
still being saved, or could not be saved (with Retry -- nothing typed is lost).
The data folder is the same setting as the "Dir:" field of the action bar.

The sample belongs to the current experiment (Trajectory → Metadata) and is
remembered per setup, not here.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from core import paths
from core.gui.bridge import error_text
from core.protocol import ServiceError


class ProfilePanel(QWidget):
    message = pyqtSignal(str, str)

    def __init__(self, bridge_getter, store, parent=None):
        super().__init__(parent)
        self._bridge = bridge_getter
        self.store = store
        self._loading = False
        lay = QHBoxLayout(self)
        box = QGroupBox("My preferences")
        g = QGridLayout(box)
        self.user_lbl = QLabel("Not logged in")
        self.user_lbl.setStyleSheet("color:#89b4fa;font-weight:bold;")
        g.addWidget(self.user_lbl, 0, 0, 1, 2)
        st = QHBoxLayout()
        self.save_state = QLabel("")
        self.save_state.setWordWrap(True)
        st.addWidget(self.save_state, 1)
        self.retry_btn = QPushButton("Retry")
        self.retry_btn.setVisible(False)
        self.retry_btn.clicked.connect(self.store.retry)
        st.addWidget(self.retry_btn)
        g.addLayout(st, 0, 2, 1, 2)
        self.data_dir = QLineEdit(str(paths.default_measurement_dir()))
        browse = QPushButton("…")
        browse.setFixedWidth(30)
        browse.clicked.connect(self._browse)
        self.operator = QLineEdit()
        self.operator.setToolTip("Operator filled in for a new experiment (each experiment keeps its own)")
        self.notebook = QLineEdit()
        self.notebook.setPlaceholderText("<data folder>\\lab_notebook_SambaMini.csv")
        self.csv = QCheckBox("Export CSV after every run")
        g.addWidget(QLabel("Data folder"), 1, 0)
        g.addWidget(self.data_dir, 1, 1, 1, 2)
        g.addWidget(browse, 1, 3)
        g.addWidget(QLabel("Default operator"), 2, 0)
        g.addWidget(self.operator, 2, 1, 1, 2)
        g.addWidget(QLabel("Lab notebook CSV"), 3, 0)
        g.addWidget(self.notebook, 3, 1, 1, 2)
        g.addWidget(self.csv, 4, 1)
        b_pw = QPushButton("Change password…")
        b_pw.clicked.connect(self._change_password)
        g.addWidget(b_pw, 5, 1)
        g.setRowStretch(6, 1)                     # extra height below the fields, not between them
        g.setColumnStretch(1, 1)
        lay.addWidget(box, 2)
        note = QLabel("Saved automatically to your account by the device service on this PC: these "
                      "preferences, and per setup your measurement draft, the current experiment (sample, "
                      "operator, notes) and the calibration-scan settings.\n\n"
                      "Named configurations (left list) and λ/2 calibrations (BD Calibration tab) are saved only "
                      "when you press their Save buttons.\n\n"
                      "Machine wiring, instrument addresses, limits and stage zeros are shared settings edited by "
                      "an administrator in Samba_controls; live instrument values always come from the device "
                      "service.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6c7086;")
        lay.addWidget(note, 1)
        self._fields = {"data_dir": self.data_dir, "default_operator": self.operator, "lab_notebook": self.notebook}
        for key, le in self._fields.items():
            le.textChanged.connect(lambda text, k=key: self._edited(k, text.strip()))
        self.csv.toggled.connect(lambda on: self._edited("export_csv", bool(on)))
        self.store.changed.connect(self._on_store_changed)
        self.store.status_changed.connect(self._show_state)
        self._show_state(self.store.state, self.store.error)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Data folder", self.data_dir.text())
        if d:
            self.data_dir.setText(d)

    def _edited(self, key: str, value) -> None:
        if not self._loading:
            self.store.set(key, value)

    def _on_store_changed(self, key: str, value) -> None:
        le = self._fields.get(key)
        if le is not None and le.text().strip() != str(value or ""):
            self._loading = True
            le.setText(str(value or ""))
            self._loading = False

    def load(self, user: dict | None) -> None:
        """Show the store's values (called after the store was reset for a session)."""
        self._loading = True
        try:
            self.user_lbl.setText(f"{user['username']} ({user['role']})" if user else "Not logged in")
            self.data_dir.setText(self.store.get("data_dir") or str(paths.default_measurement_dir()))
            self.operator.setText(self.store.get("default_operator", (user or {}).get("full_name", "")))
            self.notebook.setText(self.store.get("lab_notebook", ""))
            self.csv.setChecked(bool(self.store.get("export_csv", False)))
        finally:
            self._loading = False
        for w in list(self._fields.values()) + [self.csv]:
            w.setEnabled(user is not None)

    def _show_state(self, state: str, text: str) -> None:
        shown = {"offline": ("Not logged in — nothing is saved.", "#6c7086"),
                 "saved": ("✓ All changes saved", "#a6e3a1"),
                 "pending": ("● Changes waiting to be saved…", "#fab387"),
                 "saving": ("● Saving…", "#fab387"),
                 "error": (f"⚠ {text or 'not saved'} — your changes are kept; Retry", "#f38ba8")}
        label, color = shown.get(state, (state, "#6c7086"))
        self.save_state.setText(label)
        self.save_state.setStyleSheet(f"color:{color};")
        self.retry_btn.setVisible(state == "error")

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
