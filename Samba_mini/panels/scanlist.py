"""Scanlist tab (Samba ``Samba_main/panels/scanlist.py``).

Runs the measurement set up in the Trajectory tab N times, optionally
reversing the field between scans (Samba's "field flip", AB or ABBA order from
``core.polarity``), and writes Samba's scanlist ``.txt`` (path, relay_sign,
field_T) so Samba's analysis scripts can read it.  Start runs the scanlist
while this tab is open, as in Samba.

MiniMoke has no optical relay, current source or autofocus, so Samba's relay
flip, current sweep and refocus are not offered.  The field flip reverses the
coil command of spatial scans and time traces; field sweeps sweep the field
themselves, so it is not available for them.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.polarity import FLIP_ORDERS, ORDER_AB, flip_phase, order_preview, phase_label
from Samba_mini.panels.widgets import NoScrollSpinBox

_TOGGLE = ("QPushButton{background:#252538;border:1px solid #45475a;border-radius:5px;color:#cdd6f4;"
           "padding:4px 10px;}QPushButton:checked{background:#a6e3a1;color:#1e1e2e;border-color:#a6e3a1;"
           "font-weight:bold;}QPushButton:disabled{color:#45475a;border-color:#313244;}")


class ScanlistPanel(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        top = QHBoxLayout()

        pol = QGroupBox("Polarity control")
        pl = QVBoxLayout(pol)
        self.field_flip = QPushButton("Field flip: OFF")
        self.field_flip.setCheckable(True)
        self.field_flip.setStyleSheet(_TOGGLE)
        self.field_flip.toggled.connect(self._on_flip)
        self.order_btn = QPushButton(f"Switching: {ORDER_AB}")
        self.order_btn.setStyleSheet(_TOGGLE)
        self.order_btn.clicked.connect(self._next_order)
        self._order = ORDER_AB
        pl.addWidget(self.field_flip)
        pl.addWidget(self.order_btn)
        self.order_lbl = QLabel("")
        self.order_lbl.setStyleSheet("color:#6c7086;font-size:10px;")
        pl.addWidget(self.order_lbl)
        pl.addStretch()
        top.addWidget(pol, 0)

        sl = QGroupBox("Scanlist")
        g = QGridLayout(sl)
        g.addWidget(QLabel("Measurement:"), 0, 0)
        self.meas_lbl = QLabel("")
        self.meas_lbl.setStyleSheet("color:#89b4fa;font-weight:bold;")
        g.addWidget(self.meas_lbl, 0, 1)
        g.addWidget(QLabel("N scans:"), 0, 2)
        self.n_scans = NoScrollSpinBox()
        self.n_scans.setRange(1, 10000)
        self.n_scans.setValue(4)
        self.n_scans.setMinimumWidth(80)
        self.n_scans.valueChanged.connect(self._emit)
        g.addWidget(self.n_scans, 0, 3)
        g.addWidget(QLabel("Name:"), 1, 0)
        self.name = QLineEdit("")
        self.name.setStyleSheet("font-family:Consolas,'Courier New';color:#a6e3a1;")
        self.name.setToolTip("Scanlist file name (automatic from the metadata until you edit it)")
        self.name.textEdited.connect(self._name_edited)
        g.addWidget(self.name, 1, 1, 1, 3)
        b_auto = QPushButton("Auto")
        b_auto.setFixedWidth(50)
        b_auto.setToolTip("Back to the automatic name")
        b_auto.clicked.connect(self._auto_again)
        g.addWidget(b_auto, 1, 4)
        g.setColumnStretch(1, 1)
        top.addWidget(sl, 3)

        plan = QGroupBox("Plan")
        pg = QVBoxLayout(plan)
        self.plan_lbl = QLabel("")
        self.plan_lbl.setWordWrap(True)
        pg.addWidget(self.plan_lbl)
        pg.addStretch()
        top.addWidget(plan, 2)
        root.addLayout(top)

        note = QLabel("The measurement and its settings come from the <b>Trajectory</b> tab. Press Start while "
                      "this tab is open to run the scanlist; each scan is its own file, listed in "
                      "<i>ScanLists_&lt;setup&gt;\\&lt;name&gt;.txt</i> next to the data (Samba's format: "
                      "path, relay sign, field in T from the Hall probe). Aborted scans are not listed.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6c7086;")
        root.addWidget(note)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Field", "Coil (V)", "Status", "File"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self.table.horizontalHeader()
        for col in range(4):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)
        self._auto_name = ""
        self._name_user = False
        self._flip_allowed = True
        self._update_order()

    # ------------------------------------------------------------------
    def _emit(self, *_):
        self._update_order()
        self.changed.emit()

    def _on_flip(self, on: bool) -> None:
        self.field_flip.setText(f"Field flip: {'ON' if on else 'OFF'}")
        self._emit()

    def _next_order(self) -> None:
        i = FLIP_ORDERS.index(self._order)
        self._order = FLIP_ORDERS[(i + 1) % len(FLIP_ORDERS)]
        self.order_btn.setText(f"Switching: {self._order}")
        self._emit()

    def _update_order(self) -> None:
        flip = self.field_flip.isChecked() and self._flip_allowed
        self.order_btn.setEnabled(flip)
        self.order_lbl.setText(f"Field: {order_preview(self._order, min(self.n_scans.value(), 8))}"
                               if flip else "")

    def _name_edited(self, _t: str) -> None:
        self._name_user = True
        self.changed.emit()

    def _auto_again(self) -> None:
        self._name_user = False
        self.name.setText(self._auto_name)
        self.changed.emit()

    # ------------------------------------------------------------------
    def set_auto_name(self, name: str) -> None:
        self._auto_name = name
        if not self._name_user:
            self.name.setText(name)

    def set_measurement(self, title: str, flip_allowed: bool, plan_html: str) -> None:
        self.meas_lbl.setText(title)
        self._flip_allowed = flip_allowed
        self.field_flip.setEnabled(flip_allowed)
        self.field_flip.setToolTip("" if flip_allowed else
                                   "Field sweeps set the field themselves: no field flip")
        self.plan_lbl.setText(plan_html)
        self._update_order()

    def preview(self, coil: float | None) -> None:
        """Planned scans: polarity state and coil command of each (before a run)."""
        v = self.values()
        n = v["n_scans"]
        self.table.setRowCount(n)
        for i in range(n):
            phase = flip_phase(i, v["order"]) if v["field_flip"] else 0
            c = "" if coil is None else f"{(-coil if phase else coil):+.4g}"
            for col, text in enumerate([str(i + 1), phase_label(phase) if v["field_flip"] else "—", c,
                                        "planned", ""]):
                self.table.setItem(i, col, QTableWidgetItem(text))

    def mark(self, i: int, status: str, path: str = "") -> None:
        if 0 <= i < self.table.rowCount():
            colors = {"running": "#89b4fa", "completed": "#a6e3a1", "aborted": "#fab387", "failed": "#f38ba8"}
            item = QTableWidgetItem(status)
            from PyQt6.QtGui import QColor
            item.setForeground(QColor(colors.get(status, "#cdd6f4")))
            self.table.setItem(i, 3, item)
            if path:
                self.table.setItem(i, 4, QTableWidgetItem(path))

    def values(self) -> dict:
        return {"n_scans": self.n_scans.value(),
                "field_flip": bool(self.field_flip.isChecked() and self._flip_allowed),
                "order": self._order, "name": self.name.text().strip() or self._auto_name or "scanlist"}

    def set_editable(self, editable: bool) -> None:
        for w in (self.field_flip, self.order_btn, self.n_scans, self.name):
            w.setEnabled(editable)
        if editable:
            self.field_flip.setEnabled(self._flip_allowed)
            self._update_order()
