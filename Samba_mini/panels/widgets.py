"""Small widget primitives (NoScroll spin/combo boxes from Samba panels/_widgets.py)."""

from __future__ import annotations

from PyQt6.QtWidgets import QComboBox, QDoubleSpinBox, QPushButton, QSpinBox


class NoScrollComboBox(QComboBox):
    def wheelEvent(self, ev):
        ev.ignore()


class NoScrollSpinBox(QSpinBox):
    def wheelEvent(self, ev):
        ev.ignore()


class NoScrollDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, ev):
        ev.ignore()


def pill(text: str, object_name: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setCheckable(True)
    b.setProperty("class", "pill")
    if object_name:
        b.setObjectName(object_name)
    return b


def fmt(value: float, unit: str = "", digits: int = 4) -> str:
    try:
        if value is None or value != value:
            return "—"
        return f"{value:.{digits}g} {unit}".strip()
    except (TypeError, ValueError):
        return str(value)
