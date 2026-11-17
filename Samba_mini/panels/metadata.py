"""Samba's metadata box (``Samba_main/panels/_widgets.py`` MokeMetadataGroup).

Same fields, layout and keys as Samba, so files and lab-notebook rows read the
same way: operator, t_FM, t_S, sample, device, R4W, R2W, notes | incidence,
mirror shift (remembered per incidence), polarization, λ/2, λ/4, noDC.
The incidence replaces Samba_mini's former PMOKE/LMOKE buttons; on the polar
setup it is fixed to PMOKE (MiniMoke's polar stage is PMOKE only).
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit

from Samba_mini.panels.widgets import NoScrollComboBox, NoScrollDoubleSpinBox

INCIDENCES = ["PMOKE", "LMOKE+", "LMOKE", "TMOKE"]
DEFAULT_MIRROR_SHIFT = 12.50


def _no_underscore(le: QLineEdit) -> None:
    """Samba strips underscores: they separate the fields of scan names."""
    def fix(t: str) -> None:
        if "_" in t:
            le.blockSignals(True)
            le.setText(t.replace("_", ""))
            le.blockSignals(False)
    le.textChanged.connect(fix)


class MetadataGroup(QGroupBox):
    changed = pyqtSignal()
    sample_edited = pyqtSignal(str)          # user finished editing the sample (not programmatic)

    def __init__(self, title: str = "Metadata", parent=None):
        super().__init__(title, parent)
        top = QHBoxLayout(self)
        top.setSpacing(8)
        top.setContentsMargins(4, 4, 4, 4)

        left = QGridLayout()
        left.setSpacing(2)
        left.setColumnStretch(1, 1)
        left.setColumnStretch(3, 1)
        op_row = QHBoxLayout()
        op_row.setSpacing(4)
        op_row.addWidget(QLabel("Op:"))
        self.meta_operator = QLineEdit()
        self.meta_operator.setPlaceholderText("Name")
        self.meta_operator.setMinimumWidth(50)
        _no_underscore(self.meta_operator)
        op_row.addWidget(self.meta_operator, 1)
        op_row.addWidget(QLabel("t_FM:"))
        self.tfm_spin = NoScrollDoubleSpinBox()
        self.tfm_spin.setRange(0, 1000)
        self.tfm_spin.setDecimals(2)
        self.tfm_spin.setSuffix(" nm")
        self.tfm_spin.setFixedWidth(78)
        self.tfm_spin.setToolTip("Ferromagnet thickness (for SOT efficiency ξ_DL)")
        op_row.addWidget(self.tfm_spin)
        op_row.addWidget(QLabel("t_S:"))
        self.tstack_spin = NoScrollDoubleSpinBox()
        self.tstack_spin.setRange(0, 100000)
        self.tstack_spin.setDecimals(2)
        self.tstack_spin.setSuffix(" nm")
        self.tstack_spin.setFixedWidth(78)
        self.tstack_spin.setToolTip("Full (current-carrying) stack thickness — for J = Ic/(w·t_stack)")
        op_row.addWidget(self.tstack_spin)
        left.addLayout(op_row, 0, 0, 1, 4)

        left.addWidget(QLabel("Sample:"), 1, 0)
        self.meta_sample = QLineEdit()
        self.meta_sample.setPlaceholderText("Sample ID")
        _no_underscore(self.meta_sample)
        self.meta_sample.editingFinished.connect(lambda: self.sample_edited.emit(self.meta_sample.text().strip()))
        left.addWidget(self.meta_sample, 1, 1)
        left.addWidget(QLabel("Dev:"), 1, 2)
        self.meta_device = QLineEdit()
        self.meta_device.setPlaceholderText("Device ID")
        _no_underscore(self.meta_device)
        left.addWidget(self.meta_device, 1, 3)

        left.addWidget(QLabel("R4W:"), 2, 0)
        self.r4w_spin = NoScrollDoubleSpinBox()
        self.r4w_spin.setRange(0, 10_000_000)
        self.r4w_spin.setDecimals(3)
        self.r4w_spin.setSuffix(" Ω")
        self.r4w_spin.setMinimumWidth(80)
        left.addWidget(self.r4w_spin, 2, 1)
        left.addWidget(QLabel("R2W:"), 2, 2)
        self.r2w_spin = NoScrollDoubleSpinBox()
        self.r2w_spin.setRange(0, 10_000_000)
        self.r2w_spin.setDecimals(3)
        self.r2w_spin.setSuffix(" Ω")
        self.r2w_spin.setMinimumWidth(80)
        left.addWidget(self.r2w_spin, 2, 3)

        left.addWidget(QLabel("Notes:"), 3, 0)
        self.meta_notes = QLineEdit()
        self.meta_notes.setPlaceholderText("…")
        _no_underscore(self.meta_notes)
        left.addWidget(self.meta_notes, 3, 1, 1, 3)
        top.addLayout(left, stretch=1)

        right = QGridLayout()
        right.setSpacing(2)
        self._shift_by_inc = {inc: DEFAULT_MIRROR_SHIFT for inc in INCIDENCES}
        self._cur_inc = "LMOKE"
        self._inc_loading = False
        right.addWidget(QLabel("Incidence:"), 0, 0)
        self.incidence_combo = NoScrollComboBox()
        self.incidence_combo.addItems(INCIDENCES)
        self.incidence_combo.setCurrentText("LMOKE")
        self.incidence_combo.currentTextChanged.connect(self._on_incidence_changed)
        right.addWidget(self.incidence_combo, 0, 1)
        right.addWidget(QLabel("Mirror shift:"), 1, 0)
        self.mirror_shift = NoScrollDoubleSpinBox()
        self.mirror_shift.setRange(-50, 50)
        self.mirror_shift.setDecimals(2)
        self.mirror_shift.setValue(DEFAULT_MIRROR_SHIFT)
        self.mirror_shift.setSuffix(" mm")
        self.mirror_shift.setFixedWidth(85)
        self.mirror_shift.setToolTip("Mirror shift — remembered separately for each incidence.")
        self.mirror_shift.valueChanged.connect(self._on_shift_edited)
        right.addWidget(self.mirror_shift, 1, 1)
        right.addWidget(QLabel("Polarization:"), 2, 0)
        self.pol_combo = NoScrollComboBox()
        self.pol_combo.addItems(["s", "45°", "p", "other"])
        self.pol_combo.currentTextChanged.connect(lambda t: self.pol_custom.setVisible(t == "other"))
        right.addWidget(self.pol_combo, 2, 1)
        self.pol_custom = QLineEdit()
        self.pol_custom.setPlaceholderText("custom")
        self.pol_custom.setFixedWidth(70)
        self.pol_custom.setVisible(False)
        _no_underscore(self.pol_custom)
        right.addWidget(self.pol_custom, 2, 2, 1, 2)
        cb_row = QHBoxLayout()
        cb_row.setSpacing(10)
        self.lam2_cb = QCheckBox("λ/2")
        self.lam4_cb = QCheckBox("λ/4")
        self.nodc_cb = QCheckBox("noDC")
        cb_row.addWidget(self.lam2_cb)
        cb_row.addWidget(self.lam4_cb)
        cb_row.addSpacing(12)
        cb_row.addWidget(self.nodc_cb)
        cb_row.addStretch()
        right.addLayout(cb_row, 3, 0, 1, 4)
        top.addLayout(right)

        for w in (self.meta_operator, self.meta_sample, self.meta_device, self.meta_notes, self.pol_custom):
            w.textChanged.connect(self.changed.emit)
        for w in (self.incidence_combo, self.pol_combo):
            w.currentTextChanged.connect(self.changed.emit)
        for w in (self.lam2_cb, self.lam4_cb, self.nodc_cb):
            w.toggled.connect(self.changed.emit)
        for w in (self.mirror_shift, self.r4w_spin, self.r2w_spin, self.tfm_spin, self.tstack_spin):
            w.valueChanged.connect(self.changed.emit)

    # ------------------------------------------------------------------ incidence
    def _on_incidence_changed(self, text: str) -> None:
        if self._inc_loading:
            return
        if self._cur_inc and self._cur_inc != text:
            self._shift_by_inc[self._cur_inc] = self.mirror_shift.value()
        self._cur_inc = text
        self.mirror_shift.blockSignals(True)
        self.mirror_shift.setValue(self._shift_by_inc.get(text, DEFAULT_MIRROR_SHIFT))
        self.mirror_shift.blockSignals(False)

    def _on_shift_edited(self, value: float) -> None:
        if not self._inc_loading and self._cur_inc:
            self._shift_by_inc[self._cur_inc] = float(value)

    def fix_incidence(self, incidence: str | None) -> None:
        """Polar setup: PMOKE only (the combo is locked); None unlocks it."""
        if incidence:
            self.incidence_combo.setCurrentText(incidence)
        self.incidence_combo.setEnabled(incidence is None)
        self.incidence_combo.setToolTip("The polar setup measures PMOKE only" if incidence else "")

    # ------------------------------------------------------------------ values
    def reset(self, operator: str = "") -> None:
        """Explicit defaults (a new user / setup without a remembered experiment).  The
        incidence stays locked to PMOKE on the polar setup."""
        locked = not self.incidence_combo.isEnabled()
        for le in (self.meta_sample, self.meta_device, self.meta_notes, self.pol_custom):
            le.setText("")
        self.meta_operator.setText(operator or "")
        for sp in (self.r4w_spin, self.r2w_spin, self.tfm_spin, self.tstack_spin):
            sp.setValue(0.0)
        self._inc_loading = True
        try:
            if not locked:
                self.incidence_combo.setCurrentText("LMOKE")
            self._shift_by_inc = {inc: DEFAULT_MIRROR_SHIFT for inc in INCIDENCES}
            self._cur_inc = self.incidence_combo.currentText()
            self.mirror_shift.setValue(DEFAULT_MIRROR_SHIFT)
        finally:
            self._inc_loading = False
        self.pol_combo.setCurrentText("s")
        for cb in (self.lam2_cb, self.lam4_cb, self.nodc_cb):
            cb.setChecked(False)

    def get_values(self) -> dict:
        pol = self.pol_combo.currentText()
        if pol == "other":
            pol = self.pol_custom.text().strip() or "other"
        inc = self.incidence_combo.currentText()
        shifts = dict(self._shift_by_inc)
        shifts[inc] = self.mirror_shift.value()
        return {
            "mirror_shift_by_incidence": shifts,
            "operator": self.meta_operator.text().strip(),
            "sample_id": self.meta_sample.text().strip(),
            "device_id": self.meta_device.text().strip(),
            "notes": self.meta_notes.text().strip(),
            "incidence": inc,
            "mirror_shift": self.mirror_shift.value(),
            "polarization": pol,
            "lam2": self.lam2_cb.isChecked(),
            "lam4": self.lam4_cb.isChecked(),
            "noDC": self.nodc_cb.isChecked(),
            "r_4wire_ohm": self.r4w_spin.value(),
            "r_2wire_ohm": self.r2w_spin.value(),
            "fm_thickness_nm": self.tfm_spin.value(),
            "t_stack_nm": self.tstack_spin.value(),
        }

    def load_values(self, cfg: dict) -> None:
        """Load Samba-keyed metadata; earlier Samba_mini keys (sample, geometry) are accepted."""
        cfg = dict(cfg or {})
        if "sample" in cfg and "sample_id" not in cfg:
            cfg["sample_id"] = cfg["sample"]
        if "geometry" in cfg and "incidence" not in cfg:
            cfg["incidence"] = cfg["geometry"]
        for key, le in (("operator", self.meta_operator), ("sample_id", self.meta_sample),
                        ("device_id", self.meta_device), ("notes", self.meta_notes)):
            if key in cfg:
                le.setText(str(cfg[key]))
        if "mirror_shift_by_incidence" in cfg and not ("incidence" in cfg and self.incidence_combo.isEnabled()):
            saved = cfg.get("mirror_shift_by_incidence") or {}
            self._shift_by_inc = {i: float(saved.get(i, DEFAULT_MIRROR_SHIFT)) for i in INCIDENCES}
            if "mirror_shift" not in cfg:
                self._inc_loading = True
                try:
                    self.mirror_shift.setValue(self._shift_by_inc[self.incidence_combo.currentText()])
                finally:
                    self._inc_loading = False
        if "mirror_shift" in cfg and not ("incidence" in cfg and self.incidence_combo.isEnabled()):
            try:                                 # incidence locked (polar): the shift still loads
                self.mirror_shift.setValue(float(cfg["mirror_shift"]))
            except (TypeError, ValueError):
                pass
        if "incidence" in cfg and self.incidence_combo.isEnabled():
            inc = cfg["incidence"]
            self._inc_loading = True
            try:
                if self.incidence_combo.findText(inc) >= 0:
                    self.incidence_combo.setCurrentText(inc)
                saved = cfg.get("mirror_shift_by_incidence") or {}
                self._shift_by_inc = {i: float(saved.get(i, DEFAULT_MIRROR_SHIFT)) for i in INCIDENCES}
                self._cur_inc = self.incidence_combo.currentText()
                ms = float(cfg.get("mirror_shift", self._shift_by_inc[self._cur_inc]))
                self._shift_by_inc[self._cur_inc] = ms
                self.mirror_shift.setValue(ms)
            finally:
                self._inc_loading = False
        if "polarization" in cfg:
            pol = cfg["polarization"]
            if self.pol_combo.findText(pol) >= 0:
                self.pol_combo.setCurrentText(pol)
            else:
                self.pol_combo.setCurrentText("other")
                self.pol_custom.setText(pol)
        for key, cb in (("lam2", self.lam2_cb), ("lam4", self.lam4_cb), ("noDC", self.nodc_cb)):
            if key in cfg:
                cb.setChecked(bool(cfg[key]))
        for key, sp in (("r_4wire_ohm", self.r4w_spin), ("r_2wire_ohm", self.r2w_spin),
                        ("fm_thickness_nm", self.tfm_spin), ("t_stack_nm", self.tstack_spin)):
            if key in cfg:
                try:
                    sp.setValue(float(cfg[key]))
                except (TypeError, ValueError):
                    pass

    def build_scan_name(self, volt: float = 0.0, freq_Hz: float = 0.0, config_name: str = "") -> str:
        """Samba's auto scan name, with the 7270 oscillator amplitude in place of
        Samba's Keithley current: date_sample[_dev]_<V>V_<f>Hz_cfg_inc[_pol]_shift[_notes][_noDC][_lam2][_lam4]."""
        v = self.get_values()
        parts = [datetime.now().strftime("%Y%m%d"), v["sample_id"].replace(" ", "-") or "sample"]
        if v["device_id"]:
            parts.append(v["device_id"].replace(" ", "-"))
        parts += [f"{volt:.4g}V", f"{freq_Hz:.4g}Hz",
                  config_name.replace(" ", "-").replace("_", "-") or "cfg", v["incidence"]]
        pol = v.get("polarization", "")
        pol_tok = {"s": "Spol", "p": "Ppol", "45°": "45deg"}.get(pol, pol.replace("°", "deg").replace(" ", "-"))
        if pol_tok:
            parts.append(pol_tok)
        parts.append(f"{v['mirror_shift']:.2f}mm".replace(".", "p"))
        if v["notes"]:
            parts.append(v["notes"].replace(" ", "-"))
        for key, tok in (("noDC", "noDC"), ("lam2", "lam2"), ("lam4", "lam4")):
            if v[key]:
                parts.append(tok)
        return "_".join(parts)
