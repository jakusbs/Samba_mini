"""Named configuration sidebar (adapted from Samba ``panels/config_list.py``).

One list per setup (Longitudinal / Polar), switched from the action bar like
Samba's Green/IR tabs.  Configurations (measurement presets) are stored per
user in the service's account database, so they follow the login, not the PC.

A configuration is a reusable recipe: workflow, parameters and optics.  It is
saved only on request: **+** saves the current settings under a new name,
**S** saves them into the selected configuration, **C** copies the *saved*
selected configuration, **R** renames it (refused, never overwriting, when the
name is taken).  The selected item shows "•" while the current settings differ
from what is saved; the line under the list keeps the last save error visible.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

SETUPS = ["longitudinal", "polar"]
NAME_ROLE = Qt.ItemDataRole.UserRole

_BTN_STYLE = ("QPushButton{background:#313244;border:1px solid #45475a;border-radius:4px;padding:0;"
              "color:#cdd6f4;font-weight:bold;}QPushButton:hover{background:#45475a;}"
              "QPushButton:pressed{background:#585b70;}")


class ConfigListPanel(QWidget):
    config_selected = pyqtSignal(str, str)        # setup, name (the window may refuse and re-select)
    new_requested = pyqtSignal(str, str)          # setup, new name: save the current settings as new
    copy_requested = pyqtSignal(str, str, str)    # setup, source name, new name
    delete_requested = pyqtSignal(str, str)       # setup, name
    rename_requested = pyqtSignal(str, str, str)  # setup, old, new
    save_requested = pyqtSignal(str, str)         # setup, name: save the current settings into it

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)
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

        btn("+", "Save the current settings as a new configuration", self._new)
        btn("-", "Delete the selected configuration", self._delete)
        btn("C", "Copy the saved selected configuration under a new name", self._copy)
        btn("R", "Rename the selected configuration", self._rename)
        btn("S", "Save the current settings into the selected configuration", self._save)
        row.addStretch(1)
        lay.addLayout(row)
        self.stack = QStackedWidget()
        self.lists: dict[str, QListWidget] = {}
        self._dirty: dict[str, str | None] = {s: None for s in SETUPS}
        for s in SETUPS:
            lw = QListWidget()
            lw.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            lw.itemClicked.connect(lambda item, s=s: self.config_selected.emit(s, item.data(NAME_ROLE)))
            self.lists[s] = lw
            self.stack.addWidget(lw)
        lay.addWidget(self.stack, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#6c7086;font-size:10px;")
        lay.addWidget(self.status)
        self.setToolTip("Named configurations (workflow, parameters, optics), saved to your account.\n"
                        "They never contain the sample or the operator.")
        self.setup = SETUPS[0]

    def set_setup(self, setup: str) -> None:
        self.setup = setup
        self.stack.setCurrentWidget(self.lists[setup])
        self._show_dirty()

    def names(self, setup: str | None = None) -> list[str]:
        lw = self.lists[setup or self.setup]
        return [lw.item(i).data(NAME_ROLE) for i in range(lw.count())]

    def set_items(self, setup: str, names: list[str], current: str | None = None) -> None:
        lw = self.lists[setup]
        lw.blockSignals(True)
        lw.clear()
        for n in names:
            it = QListWidgetItem(n)
            it.setData(NAME_ROLE, n)
            lw.addItem(it)
            if n == current:
                lw.setCurrentRow(lw.count() - 1)
        if current is None:
            lw.setCurrentRow(-1)
            lw.clearSelection()
        lw.blockSignals(False)
        if self._dirty.get(setup) not in names:
            self._dirty[setup] = None
        self._show_dirty()

    def current_name(self, setup: str | None = None) -> str | None:
        it = self.lists[setup or self.setup].currentItem()
        return it.data(NAME_ROLE) if it is not None and it.isSelected() else None

    def select(self, name: str | None, setup: str | None = None) -> None:
        lw = self.lists[setup or self.setup]
        lw.blockSignals(True)
        lw.setCurrentRow(-1)
        lw.clearSelection()
        for i in range(lw.count()):
            if lw.item(i).data(NAME_ROLE) == name:
                lw.setCurrentRow(i)
        lw.blockSignals(False)
        self._show_dirty()

    def set_dirty(self, setup: str, name: str | None) -> None:
        """Mark *name* as having unsaved changes in the current settings (None = nothing unsaved)."""
        self._dirty[setup] = name
        if setup == self.setup:
            self._show_dirty()

    def _show_dirty(self) -> None:
        for s, lw in self.lists.items():
            for i in range(lw.count()):
                it = lw.item(i)
                n = it.data(NAME_ROLE)
                dirty = n == self._dirty.get(s)
                it.setText(f"{n}  •" if dirty else n)
                f = it.font()
                f.setItalic(dirty)
                it.setFont(f)
                it.setToolTip("The current settings differ from this saved configuration" if dirty else "")
        name = self._dirty.get(self.setup)
        if name and not self.status.property("error"):
            self.set_status(f"Unsaved changes to '{name}' — S saves them, + saves them under a new name.")
        elif not name and not self.status.property("error"):
            self.set_status("")

    def set_status(self, text: str, level: str = "") -> None:
        color = {"error": "#f38ba8", "ok": "#a6e3a1"}.get(level, "#fab387" if text else "#6c7086")
        self.status.setProperty("error", level == "error")
        self.status.setStyleSheet(f"color:{color};font-size:10px;")
        self.status.setText(text)

    def clear_error(self) -> None:
        self.status.setProperty("error", False)
        self._show_dirty()

    # ------------------------------------------------------------------ buttons
    def _ask_name(self, title: str, label: str, text: str = "") -> str | None:
        while True:
            name, ok = QInputDialog.getText(self, title, label, text=text)
            if not ok:
                return None
            name = name.strip()
            if not name:
                continue
            if name in self.names():
                QMessageBox.warning(self, title, f"A configuration named '{name}' already exists.\n"
                                    "Choose another name (configurations are never overwritten here).")
                text = name
                continue
            return name

    def _new(self):
        name = self._ask_name("New configuration", "Name for the current settings:")
        if name:
            self.new_requested.emit(self.setup, name)

    def _copy(self):
        cur = self.current_name()
        if not cur:
            return
        name = self._ask_name("Copy configuration", f"Name of the copy of the saved '{cur}':", f"{cur} copy")
        if name:
            self.copy_requested.emit(self.setup, cur, name)

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
        name = self._ask_name("Rename", "New name:", cur)
        if name and name != cur:
            self.rename_requested.emit(self.setup, cur, name)

    def _save(self):
        cur = self.current_name()
        if cur:
            self.save_requested.emit(self.setup, cur)
        else:
            self._new()
