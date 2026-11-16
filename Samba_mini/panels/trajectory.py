"""Trajectory panel: workflow pills, parameter forms and run metadata.

Mirrors Samba's Trajectory tab (scan-type pills + parameter groups + timing
summary + metadata) for MiniMoke's workflows.  Forms are generated from
``core.scan.workflows`` so the UI, the engine and the stored parameters share
one definition (names, units, limits).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QStackedWidget, QVBoxLayout, QWidget)

from core.scan import plans
from core.scan.workflows import WORKFLOWS, Workflow
from Samba_mini.panels.widgets import NoScrollDoubleSpinBox, NoScrollSpinBox, pill

GROUP_ORDER = ["Field", "Position", "Lock-in", "Acquisition", "Time", "Scan"]


class ParamForm(QWidget):
    changed = pyqtSignal()

    def __init__(self, wf: Workflow, parent=None):
        super().__init__(parent)
        self.wf = wf
        self.widgets: dict[str, QWidget] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        groups: dict[str, list] = {}
        for p in wf.params:
            groups.setdefault(p.group, []).append(p)
        for gname in sorted(groups, key=lambda g: GROUP_ORDER.index(g) if g in GROUP_ORDER else 99):
            box = QGroupBox(gname)
            grid = QGridLayout(box)
            grid.setHorizontalSpacing(6)
            grid.setVerticalSpacing(3)
            for row, p in enumerate(groups[gname]):
                if p.dtype == "bool":
                    w = QCheckBox(p.label)
                    w.setChecked(bool(p.default))
                    w.toggled.connect(self.changed.emit)
                    grid.addWidget(w, row, 0, 1, 2)
                else:
                    lbl = QLabel(p.label)
                    if p.dtype == "int":
                        w = NoScrollSpinBox()
                        w.setRange(int(p.min if p.min is not None else -10**9),
                                   int(p.max if p.max is not None else 10**9))
                        w.setValue(int(p.default))
                        w.valueChanged.connect(self.changed.emit)
                    else:
                        w = NoScrollDoubleSpinBox()
                        w.setDecimals(p.decimals)
                        w.setRange(p.min if p.min is not None else -1e9, p.max if p.max is not None else 1e9)
                        w.setSingleStep(p.step or 10 ** -(min(p.decimals, 3)))
                        w.setValue(float(p.default))
                        w.setKeyboardTracking(False)
                        w.valueChanged.connect(self.changed.emit)
                    if p.unit:
                        w.setSuffix(f" {p.unit}")
                    w.setMinimumWidth(110)
                    grid.addWidget(lbl, row, 0)
                    grid.addWidget(w, row, 1)
                if p.help:
                    w.setToolTip(p.help)
                w.setObjectName(f"param_{p.name}")
                self.widgets[p.name] = w
            lay.addWidget(box)
        lay.addStretch(1)

    def values(self) -> dict:
        out = {}
        for p in self.wf.params:
            w = self.widgets[p.name]
            out[p.name] = w.isChecked() if isinstance(w, QCheckBox) else w.value()
        return out

    def set_values(self, values: dict) -> None:
        for name, v in (values or {}).items():
            w = self.widgets.get(name)
            if w is None:
                continue
            w.blockSignals(True)
            try:
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(v))
                elif isinstance(w, NoScrollSpinBox):
                    w.setValue(int(round(float(v))))
                else:
                    w.setValue(float(v))
            except (TypeError, ValueError):
                pass
            finally:
                w.blockSignals(False)
        self.changed.emit()


class TrajectoryPanel(QWidget):
    workflow_changed = pyqtSignal(str)
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        top = QHBoxLayout()
        top.addWidget(QLabel("Measurement:"))
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.pills: dict[str, object] = {}
        for key, wf in WORKFLOWS.items():
            b = pill(wf.title, f"wf_{key}")
            b.setToolTip(wf.description or f"MiniMoke procedure: {wf.minimoke_name}")
            self.group.addButton(b)
            self.pills[key] = b
            top.addWidget(b)
            b.clicked.connect(lambda _=False, k=key: self.set_workflow(k))
        top.addStretch(1)
        lay.addLayout(top)

        self.stack = QStackedWidget()
        self.forms: dict[str, ParamForm] = {}
        for key, wf in WORKFLOWS.items():
            form = ParamForm(wf)
            form.changed.connect(self._update_summary)
            form.changed.connect(self.changed.emit)
            self.forms[key] = form
            self.stack.addWidget(form)
        lay.addWidget(self.stack, 1)

        bottom = QHBoxLayout()
        meta = QGroupBox("Metadata")
        mg = QGridLayout(meta)
        self.sample = QLineEdit("MySample")
        self.operator = QLineEdit("")
        self.notes = QLineEdit("")
        self.export_csv = QCheckBox("Also export CSV")
        mg.addWidget(QLabel("Sample"), 0, 0)
        mg.addWidget(self.sample, 0, 1)
        mg.addWidget(QLabel("Operator"), 0, 2)
        mg.addWidget(self.operator, 0, 3)
        mg.addWidget(QLabel("Notes"), 1, 0)
        mg.addWidget(self.notes, 1, 1, 1, 3)
        mg.addWidget(self.export_csv, 2, 0, 1, 2)
        for w in (self.sample, self.operator, self.notes):
            w.textChanged.connect(self.changed.emit)
        bottom.addWidget(meta, 2)
        timing = QGroupBox("Plan")
        tl = QVBoxLayout(timing)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        tl.addWidget(self.summary)
        bottom.addWidget(timing, 1)
        lay.addLayout(bottom)
        self.current = next(iter(WORKFLOWS))
        self.set_workflow(self.current)

    # ------------------------------------------------------------------
    def set_workflow(self, key: str) -> None:
        if key not in WORKFLOWS:
            return
        self.current = key
        self.pills[key].setChecked(True)
        self.stack.setCurrentWidget(self.forms[key])
        self._update_summary()
        self.workflow_changed.emit(key)

    def workflow(self) -> Workflow:
        return WORKFLOWS[self.current]

    def params(self) -> dict:
        return self.forms[self.current].values()

    def set_params(self, key: str, values: dict) -> None:
        if key in self.forms:
            self.forms[key].set_values(values)

    def all_params(self) -> dict:
        return {k: f.values() for k, f in self.forms.items()}

    def metadata(self) -> dict:
        return {"sample": self.sample.text().strip() or "sample", "operator": self.operator.text().strip(),
                "notes": self.notes.text().strip(), "export_csv": self.export_csv.isChecked()}

    def set_metadata(self, meta: dict) -> None:
        if "sample" in meta:
            self.sample.setText(str(meta["sample"]))
        if "operator" in meta:
            self.operator.setText(str(meta["operator"]))
        if "notes" in meta:
            self.notes.setText(str(meta["notes"]))
        if "export_csv" in meta:
            self.export_csv.setChecked(bool(meta["export_csv"]))

    def set_editable(self, editable: bool) -> None:
        self.stack.setEnabled(editable)
        for b in self.pills.values():
            b.setEnabled(editable)
        for w in (self.sample, self.operator, self.notes, self.export_csv):
            w.setEnabled(editable)

    def estimate(self) -> tuple[int, float | None, str | None]:
        """(points, estimated seconds or None, validation message or None)."""
        wf = self.workflow()
        try:
            p = wf.coerce(self.params())
        except ValueError as exc:
            return 0, None, str(exc)
        k = wf.key
        if k == "b_sweep_dc":
            plan = plans.b_sweep_dc_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"])
            msg = plans.b_sweep_dc_validation(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"])
            return plan.n_total * p["num_sweeps"], plan.n_total * plan.t_point_s * p["num_sweeps"], msg
        if k == "b_sweep_lockin":
            plan = plans.b_sweep_lockin_plan(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"],
                                             p["time_const"], p["acq_time"])
            msg = plans.b_sweep_lockin_validation(p["b_min"], p["b_max"], p["b_step"], p["sweep_freq"],
                                                  p["time_const"], p["acq_time"])
            return plan.n_total * p["num_sweeps"], plan.n_total * plan.t_point_s * p["num_sweeps"], msg
        if k == "x_sweep":
            n = plans.n_points(p["x_min"], p["x_max"], p["x_step"]) * 2 * p["repeat_num"]
            return n, n * (p["acq_time"] + 0.05), None
        if k == "y_sweep":
            n = plans.n_points(p["y_min"], p["y_max"], p["y_step"]) * 2 * p["repeat_num"]
            return n, n * (p["acq_time"] + 0.05), None
        if k == "xy_sweep":
            n = (plans.n_points(p["x_min"], p["x_max"], p["x_step"])
                 * plans.n_points(p["y_min"], p["y_max"], p["y_step"]) * p["repeat_num"])
            return n, n * (p["acq_time"] + 0.05), None
        n = plans.time_points(p["duration"], p["interval"])
        return n, max(p["duration"], n * p["acq_time"]), None

    def _update_summary(self) -> None:
        n, secs, msg = self.estimate()
        wf = self.workflow()
        parts = [f"<b>{wf.title}</b> (MiniMoke: {wf.minimoke_name})", f"Points: <b>{n}</b>"]
        if secs is not None:
            label = "Estimated duration" if wf.key in ("b_sweep_dc", "b_sweep_lockin", "time") \
                else "Acquisition time (moves not included)"
            parts.append(f"{label}: <b>{_dur(secs)}</b>")
        if wf.key in ("b_sweep_dc", "b_sweep_lockin"):
            parts.append("Coil values are the DAQ command in V (MiniMoke labelled them 'A').")
        if msg:
            parts.append(f"<span style='color:#f38ba8'>{msg}</span>")
        self.summary.setText("<br>".join(parts))


def _dur(s: float) -> str:
    s = float(s)
    if s < 60:
        return f"{s:.1f} s"
    if s < 3600:
        return f"{int(s // 60)} min {int(s % 60)} s"
    return f"{int(s // 3600)} h {int((s % 3600) // 60)} min"
