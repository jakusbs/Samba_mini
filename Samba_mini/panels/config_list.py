"""Named configuration sidebar (adapted from Samba ``panels/config_list.py``).

One list per setup (Longitudinal / Polar), switched from the action bar like
Samba's Green/IR tabs.  Configurations are stored per user in the service's
account database (presets), so they follow the login, not the PC.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QHBoxLayout, QInputDialog, QLabel, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout,
                             QWidget)

SETUPS = ["longitudinal", "polar"]

_BTN_STYLE = ("QPushButton{background:#313244;border:1px solid #45475a;border-radius:4px;padding:0;"
              "color:#cdd6f4;font-weight:bold;}QPushButton:hover{background:#45475a;}"
              "QPushButton:pressed{background:#585b70;}")


class ConfigListPanel(QWidget):
    config_selected = pyqtSignal(str, str)        # setup, name
    new_requested = pyqtSignal(str)               # setup
    copy_requested = pyqtSignal(str, str)         # setup, new name
    delete_requested = pyqtSignal(str, str)       # setup, name
    rename_requested = pyqtSignal(str, str, str)  # setup, old, new
    save_requested = pyqtSignal(str, str)         # setup, name

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)
        title = QLabel("Configurations")
        title.setStyleSheet("color:#89b4fa;font-weight:bold;")
        lay.addWidget(title)
        row = QHBoxLayout()
        row.setSpacing(3)

        def btn(text, tip, slot):
            b = QPushButton(text)
            b.setFixedSize(24, 24)
            b.setStyleSheet(_BTN_STYLE)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(slot)
            row.addWidget(b)
            return b

        btn("+", "New configuration from the current settings", self._new)
        btn("-", "Delete configuration", self._delete)
        btn("C", "Copy configuration", self._copy)
        btn("R", "Rename configuration", self._rename)
        btn("S", "Save current settings into the selected configuration", self._save)
        row.addStretch(1)
        lay.addLayout(row)
        self.stack = QStackedWidget()
        self.lists: dict[str, QListWidget] = {}
        for s in SETUPS:
            lw = QListWidget()
            lw.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            lw.itemClicked.connect(lambda item, s=s: self.config_selected.emit(s, item.text()))
            self.lists[s] = lw
            self.stack.addWidget(lw)
        lay.addWidget(self.stack, 1)
        self.hint = QLabel("Log in to use your saved configurations.")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#6c7086;font-size:10px;")
        lay.addWidget(self.hint)
        self.setup = SETUPS[0]

    def set_setup(self, setup: str) -> None:
        self.setup = setup
        self.stack.setCurrentWidget(self.lists[setup])

    def set_items(self, setup: str, names: list[str], current: str | None = None) -> None:
        lw = self.lists[setup]
        lw.blockSignals(True)
        lw.clear()
        for n in names:
            lw.addItem(QListWidgetItem(n))
            if n == current:
                lw.setCurrentRow(lw.count() - 1)
        lw.blockSignals(False)

    def current_name(self) -> str | None:
        it = self.lists[self.setup].currentItem()
        return it.text() if it else None

    def select(self, name: str) -> None:
        lw = self.lists[self.setup]
        for i in range(lw.count()):
            if lw.item(i).text() == name:
                lw.setCurrentRow(i)

    def _new(self):
        name, ok = QInputDialog.getText(self, "New configuration", "Name:")
        if ok and name.strip():
            self.new_requested.emit(self.setup)
            self.save_requested.emit(self.setup, name.strip())

    def _copy(self):
        cur = self.current_name()
        if not cur:
            return
        name, ok = QInputDialog.getText(self, "Copy configuration", "Name of the copy:", text=f"{cur} copy")
        if ok and name.strip():
            self.copy_requested.emit(self.setup, name.strip())

    def _delete(self):
        cur = self.current_name()
        if not cur:
            return
        ans = QMessageBox.question(self, "Delete configuration", f'Delete "{cur}"?\nThis cannot be undone.',
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(self.setup, cur)

    def _rename(self):
        cur = self.current_name()
        if not cur:
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=cur)
        if ok and name.strip() and name.strip() != cur:
            self.rename_requested.emit(self.setup, cur, name.strip())

    def _save(self):
        cur = self.current_name()
        if cur:
            self.save_requested.emit(self.setup, cur)
        else:
            self._new()
