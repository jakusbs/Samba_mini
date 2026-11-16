"""BD Calibration tab — Samba's λ/2 plate calibration (``core/bd_calibration.py``).

Six balanced-diode readings (mV) at the λ/2 micrometer ticks 0, 5, 10, 15, 20,
25.  They are written into every new scan as ``/data/calibration`` (all zeros =
no calibration, not written, as in Samba) and drive the θ (µrad) display of the
plots and the Data Browser (``core.kerr``).  Saved per user and setup in the
service's account database.

"Fit & Import" reads the newest TIME scan whose name ends in "calibration" in
today's data folder (``core.bd_fit``) — in Samba_mini: a time scan (no axis
ticked) run from a configuration named e.g. "calibration" while stepping the
plate through the six ticks — and fills the six values.
"""

from __future__ import annotations

import os
from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                             QPushButton, QVBoxLayout, QWidget)

from core import bd_fit
from Samba_mini.panels.widgets import NoScrollDoubleSpinBox

TICKS = [0, 5, 10, 15, 20, 25]


class BDCalibrationPanel(QWidget):
    calibration_changed = pyqtSignal(list)     # user edits / fit (drives nothing automatic)
    calibration_applied = pyqtSignal(list)     # any change incl. programmatic (drives the θ display)
    save_requested = pyqtSignal(list)
    load_requested = pyqtSignal()

    def __init__(self, dir_getter=None, parent=None):
        super().__init__(parent)
        self._dir = dir_getter
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)
        hdr_row = QHBoxLayout()
        hdr = QLabel("λ/2 Plate Calibration")
        hdr.setStyleSheet("color:#cba6f7;font-size:13px;font-weight:bold;")
        hdr_row.addWidget(hdr)
        self._fit_btn = QPushButton("⤓ Fit && Import")
        self._fit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._fit_btn.setFixedHeight(26)
        self._fit_btn.setToolTip("Read the newest TIME …calibration.h5 in today's data folder,\n"
                                 "fit the DC staircase, and fill the six mV values below.\n"
                                 "If the newest file isn't a calibration scan you can pick one.")
        self._fit_btn.setStyleSheet(
            "QPushButton{background:#313244;color:#cdd6f4;border:1px solid #45475a;border-radius:5px;"
            "padding:3px 12px;font-size:11px;font-weight:bold;}QPushButton:hover{background:#45475a;}")
        self._fit_btn.clicked.connect(self._on_fit_import)
        hdr_row.addWidget(self._fit_btn)
        hdr_row.addStretch()
        root.addLayout(hdr_row)
        desc = QLabel("Enter the measured MOKE signal (mV) at each tick position of the λ/2 plate.\n"
                      "The calibration is saved in every new HDF5 scan file under /data/calibration and "
                      "drives the θ (µrad) display of the plots and the Data Browser.\n"
                      "Fit & Import: run a time scan (no axis ticked) from a configuration named "
                      "\"calibration\" while stepping the plate through the six ticks.")
        desc.setStyleSheet("color:#a6adc8;font-size:10px;")
        desc.setWordWrap(True)
        root.addWidget(desc)

        cal_grp = QGroupBox("Calibration Values")
        cal_grp.setStyleSheet("QGroupBox{border:1px solid #45475a;border-radius:6px;margin-top:9px;"
                              "padding-top:9px;font-weight:bold;color:#cba6f7;}"
                              "QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}")
        g = QGridLayout(cal_grp)
        g.setSpacing(6)
        g.setContentsMargins(10, 14, 10, 10)
        for col, tick in enumerate(TICKS):
            lbl = QLabel(f"{tick}°")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color:#6c7086;font-size:10px;font-weight:bold;")
            g.addWidget(lbl, 0, col + 1)
        g.addWidget(QLabel("Ticks:"), 1, 0)
        for col, tick in enumerate(TICKS):
            lbl = QLabel(str(tick))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("background:#313244;border:1px solid #45475a;border-radius:3px;"
                              "padding:3px 6px;color:#a6adc8;font-size:11px;")
            g.addWidget(lbl, 1, col + 1)
        mv = QLabel("mV:")
        mv.setStyleSheet("color:#cdd6f4;font-weight:bold;")
        g.addWidget(mv, 2, 0)
        self._mv_spins: list[NoScrollDoubleSpinBox] = []
        for col in range(6):
            sp = NoScrollDoubleSpinBox()
            sp.setRange(-1e6, 1e6)
            sp.setDecimals(2)
            sp.setMinimumWidth(80)
            sp.valueChanged.connect(self._on_value_changed)
            g.addWidget(sp, 2, col + 1)
            self._mv_spins.append(sp)
        root.addWidget(cal_grp)

        btn_row = QHBoxLayout()
        save = QPushButton("Save calibration")
        save.setStyleSheet("QPushButton{background:#a6e3a1;color:#1e1e2e;font-weight:bold;border:none;"
                           "border-radius:5px;padding:4px 14px;}QPushButton:hover{background:#94d992;}")
        save.clicked.connect(lambda: self.save_requested.emit(self.get_calibration()))
        load = QPushButton("Load saved")
        load.setStyleSheet("QPushButton{background:#89b4fa;color:#1e1e2e;font-weight:bold;border:none;"
                           "border-radius:5px;padding:4px 14px;}QPushButton:hover{background:#7aa2e8;}")
        load.clicked.connect(self.load_requested.emit)
        clear = QPushButton("Clear")
        clear.setToolTip("All zeros = no calibration (not written to files)")
        clear.clicked.connect(lambda: (self.load_calibration([0.0] * 6),
                                       self.calibration_changed.emit(self.get_calibration())))
        for b in (save, load, clear):
            btn_row.addWidget(b)
        btn_row.addStretch()
        root.addLayout(btn_row)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#45475a;")
        root.addWidget(sep)
        self._status_lbl = QLabel("No calibration saved yet.")
        self._status_lbl.setStyleSheet("color:#6c7086;font-size:10px;")
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)
        root.addStretch()

    # ------------------------------------------------------------------ values
    def get_calibration(self) -> list[float]:
        return [sp.value() for sp in self._mv_spins]

    def load_calibration(self, vals) -> None:
        """Set the six values without emitting calibration_changed (θ display still follows)."""
        for i, sp in enumerate(self._mv_spins):
            if i < len(vals):
                sp.blockSignals(True)
                sp.setValue(float(vals[i]))
                sp.blockSignals(False)
        self.calibration_applied.emit(self.get_calibration())

    def set_status(self, text: str) -> None:
        self._status_lbl.setText(text)

    def _on_value_changed(self) -> None:
        vals = self.get_calibration()
        self.calibration_changed.emit(vals)
        self.calibration_applied.emit(vals)

    # ------------------------------------------------------------------ fit & import
    def _day_folder(self) -> str:
        base = os.path.expanduser((self._dir() if self._dir else "") or "").strip()
        if not base:
            return ""
        day = os.path.join(base, datetime.now().strftime("%Y%m%d"))
        return day if os.path.isdir(day) else base

    def _on_fit_import(self) -> None:
        folder = self._day_folder()
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "No data folder", "Could not find today's data folder.\n\n"
                                f"Looked in: {folder or '(the Dir field is empty)'}")
            return
        newest = bd_fit.latest_h5(folder)
        if newest and bd_fit.is_time_calibration(newest):
            path = newest
        else:
            why = ("The folder contains no .h5 files." if not newest else
                   f"The newest file is not a TIME calibration scan:\n{os.path.basename(newest)}")
            QMessageBox.information(self, "Choose a calibration file", f"{why}\n\nPick the calibration scan to use.")
            path, _ = QFileDialog.getOpenFileName(self, "Choose a calibration scan", folder, "HDF5 scans (*.h5)")
            if not path:
                return
        self.fit_path(path)

    def fit_path(self, path: str) -> bool:
        try:
            result = bd_fit.fit_file(path)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            QMessageBox.warning(self, "Could not read file", f"{os.path.basename(path)}\n\n{exc}")
            return False
        if result.ok:
            self.load_calibration(result.levels_mV)
            self.calibration_changed.emit(self.get_calibration())
            extra = ""
            if result.n_candidates > 1:
                extra = (f" {result.n_candidates} evenly spaced runs of 6 were possible; took the one "
                         f"centred on zero (centre {result.midpoint_mV:+.2f} mV).")
            self.set_status(f"Fitted {os.path.basename(path)} — {len(result.all_plateaus)} plateau(s) found, "
                            f"6 imported.{extra} Review, then Save calibration.")
            return True
        QMessageBox.warning(self, "Could not fit this file",
                            f"{os.path.basename(path)} could not be fitted.\n\n{result.reason}.\n\n"
                            "The calibration values were left unchanged.")
        self.set_status(f"Fit failed for {os.path.basename(path)} — {result.reason}.")
        return False
