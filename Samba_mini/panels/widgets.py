"""Small widget primitives taken from Samba (``panels/_widgets.py``,
``panels/trajectory.py``, ``core/nstep.py``): NoScroll spin/combo boxes, the
segmented pill buttons, read-only field style and the N <-> step coupling."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QButtonGroup, QComboBox, QDoubleSpinBox, QHBoxLayout, QPushButton, QSpinBox

from core.scan import plans


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


#: Samba's read-only field look (ActuatorGroup device/label/unit boxes).
RO_STYLE = ("background:#1e1e2e;color:#6c7086;border:1px solid #313244;"
            "border-radius:4px;padding:2px 4px;")


def segmented_pills(labels: list[str], parent, color: str = "#89b4fa",
                    height: int = 28, min_width: int = 80) -> tuple[QHBoxLayout, QButtonGroup, list[QPushButton]]:
    """Samba's scan-type selector: exclusive pill buttons with rounded outer ends."""
    row = QHBoxLayout()
    row.setSpacing(0)
    grp = QButtonGroup(parent)
    grp.setExclusive(True)
    btns = []
    for idx, label in enumerate(labels):
        b = QPushButton(label)
        b.setCheckable(True)
        b.setChecked(idx == 0)
        b.setFixedHeight(height)
        b.setMinimumWidth(min_width)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if len(labels) == 1:
            radius = "border-radius:6px;"
        elif idx == 0:
            radius = ("border-top-left-radius:6px;border-bottom-left-radius:6px;"
                      "border-top-right-radius:0;border-bottom-right-radius:0;")
        elif idx == len(labels) - 1:
            radius = ("border-top-left-radius:0;border-bottom-left-radius:0;"
                      "border-top-right-radius:6px;border-bottom-right-radius:6px;")
        else:
            radius = "border-radius:0;"
        b.setStyleSheet(
            f"QPushButton{{background:#252538;border:1px solid #45475a;"
            f"color:#6c7086;font-size:11px;font-weight:bold;padding:0 14px;{radius}}}"
            f"QPushButton:hover{{background:#313244;color:#cdd6f4;}}"
            f"QPushButton:checked{{background:{color};color:#1e1e2e;border-color:{color};}}"
            f"QPushButton:disabled{{background:#1e1e2e;color:#45475a;border-color:#313244;}}")
        grp.addButton(b, idx)
        row.addWidget(b)
        btns.append(b)
    return row, grp, btns


class StepNPair:
    """Keep an N spinbox and a step spinbox consistent (Samba's NStepPair).

    Unlike Samba, the STEP is the value handed to the engine (MiniMoke's
    procedures take from/to/step), so N is always derived with MiniMoke's own
    rule, ``plans.n_points``; typing N sets the step to span / (N - 1).
    """

    def __init__(self, npts_spin, step_spin, span_getter):
        self._n = npts_spin
        self._s = step_spin
        self._span = span_getter
        self._guard = False
        for w in (npts_spin, step_spin):
            w.setKeyboardTracking(False)
        npts_spin.valueChanged.connect(self._on_n_edited)
        step_spin.valueChanged.connect(self._on_s_edited)

    def _on_n_edited(self, *_):
        if self._guard:
            return
        span = abs(float(self._span()))
        n = max(2, int(self._n.value()))
        if span > 0:
            self._set(self._s, span / (n - 1))
            self.span_changed()          # N after rounding of the derived step

    def _on_s_edited(self, *_):
        if not self._guard:
            self.span_changed()

    def span_changed(self, *_):
        step = float(self._s.value())
        n = plans.n_points(0.0, float(self._span()), step)
        n = min(n, int(self._n.maximum()))
        self._set(self._n, max(int(self._n.minimum()), n))

    def _set(self, spin, value) -> None:
        self._guard = True
        try:
            spin.setValue(value)
        finally:
            self._guard = False


def fmt(value: float, unit: str = "", digits: int = 4) -> str:
    try:
        if value is None or value != value:
            return "—"
        return f"{value:.{digits}g} {unit}".strip()
    except (TypeError, ValueError):
        return str(value)
