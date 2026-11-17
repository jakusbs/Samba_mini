"""BD Calibration tab — Samba's λ/2 plate calibration (``core/bd_calibration.py``), with
identifiable, versioned records.

Six balanced-diode readings (mV) at the λ/2 micrometer ticks 0, 5, 10, 15, 20, 25 convert
volts to Kerr rotation (``core.kerr``).  The workflow is

    ① Acquire  (Calibration tab: Start runs the calibration time scan)
    ② Fit & review  (the finished scan is fitted here automatically, or Fit & Import… a file)
    ③ Save  (an immutable record in the device service: id, version, creator, time, setup,
             sample, six values in mV, real/simulation mode, source file, fit quality)
    ④ Activate for this experiment  (per account and setup)

Only the *active* record is used: it is frozen into every run (``/data/calibration`` plus its
identity in ``/metadata``) and drives the θ (µrad) display.  Values in the editor do nothing
until they are saved and activated, so unsaved edits never reach a file, and nothing here
reloads or replaces them behind the user's back: actions that would drop them ask first.

Fit & Import checks the file's setup, sample, acquisition status and real/simulation mode
against the current experiment and explains any mismatch; it never picks a file just because
it is the newest.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout,
                             QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from core import bd_fit
from core.gui.bridge import error_text
from core.protocol import ServiceError
from Samba_mini.panels.widgets import NoScrollDoubleSpinBox
from Samba_mini.state import canonical

TICKS = [0, 5, 10, 15, 20, 25]
FEATURE = "calibration-records-1"
SETUP_LABEL = {"longitudinal": "Longitudinal", "polar": "Polar"}

_BTN = ("QPushButton{{background:{bg};color:{fg};font-weight:bold;border:none;border-radius:5px;"
        "padding:4px 12px;}}QPushButton:hover{{background:{hover};}}"
        "QPushButton:disabled{{background:#313244;color:#6c7086;}}")


def _fmt_time(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "unknown date"


def describe_record(rec: dict | None, short: bool = False) -> str:
    """One line naming a calibration record (unknown provenance said as such)."""
    if not rec:
        return "none"
    name = rec.get("name") or "calibration"
    head = f"{name} v{rec.get('version', '?')} ({rec.get('id', '?')})"
    if short:
        return head
    sample = rec.get("sample_id")
    mode = rec.get("mode")
    parts = [head, f"sample {sample!r}" if sample else "sample unknown",
             mode if mode else "mode unknown",
             f"by {rec.get('creator') or 'unknown'}, {_fmt_time(rec.get('created'))}"]
    if rec.get("origin") == "fit" and rec.get("source_file"):
        parts.append(f"fit of {os.path.basename(rec['source_file'])}")
    elif rec.get("origin") == "migrated":
        parts.append("migrated from an earlier version")
    return " · ".join(parts)


def snapshot(active: dict | None) -> dict | None:
    """The run-time copy of the active calibration (what a measurement freezes and records)."""
    rec = (active or {}).get("record")
    if not rec:
        return None
    snap = {k: rec.get(k) for k in ("id", "series", "version", "name", "creator", "created", "setup",
                                    "sample_id", "values", "unit", "mode", "source_file", "origin", "fit")}
    snap["values"] = [float(v) for v in rec["values"]]
    snap["activated_for_sample"] = (active or {}).get("sample_id")
    snap["activated"] = (active or {}).get("activated")
    return snap


def read_file_info(path: str) -> dict:
    """Provenance of a scan file as recorded by Samba_mini (missing entries are None)."""
    import h5py
    info = {"path": path, "name": os.path.basename(path), "mtime": None, "status": None, "scan_type": None,
            "mode": None, "setup": None, "sample_id": None, "workflow": None, "calibration_scan": None,
            "timestamp": None, "error": None}
    try:
        info["mtime"] = os.path.getmtime(path)
        with h5py.File(path, "r") as f:
            a = f.attrs
            meta = f["metadata"].attrs if "metadata" in f else {}

            def get(src, key):
                if key not in src:
                    return None
                v = src[key]
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "replace")
                if hasattr(v, "item"):
                    v = v.item()
                return v
            info["status"] = get(a, "scan_status")
            info["scan_type"] = get(a, "scan_type")
            info["timestamp"] = get(a, "timestamp")
            info["mode"] = get(meta, "mode") or get(a, "mode")
            info["setup"] = get(meta, "setup")
            info["sample_id"] = get(meta, "sample_id")
            info["workflow"] = get(meta, "workflow")
            cs = get(meta, "calibration_scan")
            info["calibration_scan"] = None if cs is None else bool(cs)
    except Exception as exc:  # noqa: BLE001 - reported to the user
        info["error"] = str(exc)
    return info


def file_problems(info: dict, ctx: dict) -> list[str]:
    """Why a file might not be a calibration of the current experiment (empty = it matches)."""
    if info.get("error"):
        return [f"the file could not be read: {info['error']}"]
    p = []
    setup, sample, mode = ctx.get("setup"), (ctx.get("sample_id") or ""), ctx.get("mode")
    if info.get("scan_type") not in (None, "TIME") or (info.get("workflow") not in (None, "time")):
        p.append(f"it is not a time scan ({info.get('workflow') or info.get('scan_type')})")
    if info.get("status") is None:
        p.append("its acquisition status is not recorded")
    elif info["status"] != "completed":
        p.append(f"the acquisition did not complete (status: {info['status']})")
    if info.get("setup") is None:
        p.append("it does not record its setup")
    elif setup and info["setup"] != setup:
        p.append(f"it was measured on the {info['setup']} setup, not the {setup} setup")
    if info.get("sample_id") is None:
        p.append("it does not record its sample")
    elif sample and info["sample_id"] != sample:
        p.append(f"it was measured on sample {info['sample_id']!r}, the current sample is {sample!r}")
    if info.get("mode") is None:
        p.append("it does not record whether it was real or simulated")
    elif mode and info["mode"] != mode:
        p.append(f"it is a {info['mode']} measurement and the service now runs {mode} instruments")
    return p


def candidate_files(folders: list[str]) -> list[str]:
    seen, out = set(), []
    for folder in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for n in names:
            p = os.path.join(folder, n)
            if n.lower().endswith(".h5") and os.path.isfile(p) and os.path.normcase(p) not in seen:
                seen.add(os.path.normcase(p))
                out.append(p)
    return out


class BDCalibrationPanel(QWidget):
    message = pyqtSignal(str, str)          # level, text (log)
    active_changed = pyqtSignal(object)     # the active calibration info (or None)
    editor_changed = pyqtSignal()           # editor values / saved state changed

    def __init__(self, dir_getter=None, context_getter=None, parent=None):
        super().__init__(parent)
        self._dir = dir_getter
        self._context = context_getter or (lambda: {})
        self._base: dict | None = None          # the saved record shown in the editor
        self._prov: dict = {"origin": "manual"}
        self._prov_changed = False
        self._clean: list[float] = [0.0] * 6
        self._active: dict | None = None
        self._active_error = ""
        self._run_snapshot: dict | None = None
        self._running = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(6)
        hdr_row = QHBoxLayout()
        hdr = QLabel("λ/2 Plate Calibration")
        hdr.setStyleSheet("color:#cba6f7;font-size:13px;font-weight:bold;")
        hdr_row.addWidget(hdr)
        self.steps = QLabel("")
        self.steps.setStyleSheet("color:#6c7086;font-size:10px;")
        hdr_row.addWidget(self.steps, 1)
        root.addLayout(hdr_row)

        top = QHBoxLayout()
        self._fit_btn = QPushButton("⤓ Fit && Import…")
        self._fit_btn.setToolTip("Choose a calibration time scan, check that it belongs to this setup, sample and\n"
                                 "real/simulation mode, fit the DC staircase and put the six values in the editor.")
        self._fit_btn.clicked.connect(self.fit_import)
        self._load_btn = QPushButton("Load saved…")
        self._load_btn.setToolTip("Read your saved calibrations of this setup from the device service and open one")
        self._load_btn.clicked.connect(lambda: self.load_saved())
        self._new_btn = QPushButton("New")
        self._new_btn.setToolTip("Start an empty calibration in the editor (all zeros)")
        self._new_btn.clicked.connect(lambda: self.confirm_discard("starting a new calibration") and self.new_editor())
        for b in (self._fit_btn, self._load_btn, self._new_btn):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            top.addWidget(b)
        top.addStretch()
        root.addLayout(top)

        self.cal_grp = QGroupBox("Editor")
        self.cal_grp.setStyleSheet("QGroupBox{border:1px solid #45475a;border-radius:6px;margin-top:9px;"
                                   "padding-top:9px;font-weight:bold;color:#cba6f7;}"
                                   "QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}")
        g = QGridLayout(self.cal_grp)
        g.setSpacing(6)
        g.setContentsMargins(10, 14, 10, 8)
        g.addWidget(QLabel("Ticks:"), 0, 0)
        for col, tick in enumerate(TICKS):
            lbl = QLabel(str(tick))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("background:#313244;border:1px solid #45475a;border-radius:3px;"
                              "padding:3px 6px;color:#a6adc8;font-size:11px;")
            g.addWidget(lbl, 0, col + 1)
        mv = QLabel("mV:")
        mv.setStyleSheet("color:#cdd6f4;font-weight:bold;")
        g.addWidget(mv, 1, 0)
        self._mv_spins: list[NoScrollDoubleSpinBox] = []
        for col in range(6):
            sp = NoScrollDoubleSpinBox()
            sp.setRange(-1e6, 1e6)
            sp.setDecimals(2)
            sp.setMinimumWidth(80)
            sp.valueChanged.connect(self._on_value_changed)
            g.addWidget(sp, 1, col + 1)
            self._mv_spins.append(sp)
        self.prov_lbl = QLabel("")
        self.prov_lbl.setWordWrap(True)
        self.prov_lbl.setStyleSheet("color:#a6adc8;font-size:10px;")
        g.addWidget(self.prov_lbl, 2, 0, 1, 7)
        root.addWidget(self.cal_grp)

        btn_row = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.save_btn.setStyleSheet(_BTN.format(bg="#a6e3a1", fg="#1e1e2e", hover="#94d992"))
        self.save_btn.setToolTip("Save the editor's values: a new version of the calibration shown, or a new "
                                 "calibration")
        self.save_btn.clicked.connect(lambda: self.save())
        self.save_as_btn = QPushButton("Save as new…")
        self.save_as_btn.clicked.connect(lambda: self.save(as_new=True))
        self.activate_btn = QPushButton("Activate for this experiment")
        self.activate_btn.setStyleSheet(_BTN.format(bg="#89b4fa", fg="#1e1e2e", hover="#7aa2e8"))
        self.activate_btn.setToolTip("Use the saved calibration shown in the editor for the measurements of the "
                                     "current sample on this setup (written into every new file)")
        self.activate_btn.clicked.connect(lambda: self.activate())
        self.deactivate_btn = QPushButton("Deactivate")
        self.deactivate_btn.setToolTip("Measure without a λ/2 calibration (none is written to the files)")
        self.deactivate_btn.clicked.connect(lambda: self.deactivate())
        for b in (self.save_btn, self.save_as_btn, self.activate_btn, self.deactivate_btn):
            btn_row.addWidget(b)
        btn_row.addStretch()
        root.addLayout(btn_row)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#45475a;")
        root.addWidget(sep)
        self.active_lbl = QLabel("")
        self.active_lbl.setWordWrap(True)
        self.active_lbl.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self.active_lbl)
        self.run_lbl = QLabel("")
        self.run_lbl.setWordWrap(True)
        self.run_lbl.setStyleSheet("color:#a6e3a1;font-size:10px;")
        self.run_lbl.setVisible(False)
        root.addWidget(self.run_lbl)
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)
        root.addStretch()
        self.set_status("")
        self._refresh()

    # ------------------------------------------------------------------ context
    def _ctx(self) -> dict:
        try:
            return dict(self._context() or {})
        except Exception:  # noqa: BLE001
            return {}

    def _client(self, ctx: dict | None = None):
        ctx = ctx if ctx is not None else self._ctx()
        c = ctx.get("client")
        if c is None or not c.session:
            self.set_status("Log in to use saved calibrations.", "warning")
            return None
        if not c.has_feature(FEATURE):
            self.set_status("The running device service is older than this Samba_mini and cannot store "
                            "calibration records. Restart the service (Samba_controls) when no measurement "
                            "is running.", "error")
            return None
        return c

    # ------------------------------------------------------------------ values
    def get_calibration(self) -> list[float]:
        return [round(sp.value(), 2) for sp in self._mv_spins]

    def _set_values(self, vals) -> None:
        for i, sp in enumerate(self._mv_spins):
            sp.blockSignals(True)
            sp.setValue(float(vals[i]) if i < len(vals) else 0.0)
            sp.blockSignals(False)

    def load_calibration(self, vals) -> None:
        """Put values into the editor as a manual entry (programmatic; not saved, not active)."""
        self._set_values(vals)
        if self._prov.get("origin") != "manual":
            self._prov = {"origin": "manual"}
            self._prov_changed = True
        self._refresh()

    def _on_value_changed(self) -> None:
        if self._prov.get("origin") == "fit":
            self._prov = dict(self._prov, edited=True)
        self._refresh()

    def is_dirty(self) -> bool:
        return self._prov_changed or canonical(self.get_calibration()) != canonical(self._clean)

    def editor_record(self) -> dict | None:
        return self._base

    def active(self) -> dict | None:
        return self._active

    def active_snapshot(self) -> dict | None:
        return snapshot(self._active)

    # ------------------------------------------------------------------ editor state
    def new_editor(self) -> None:
        self._base = None
        self._prov = {"origin": "manual"}
        self._prov_changed = False
        self._clean = [0.0] * 6
        self._set_values(self._clean)
        self._refresh()

    def load_record(self, rec: dict) -> None:
        self._base = rec
        self._prov = {"origin": rec.get("origin"), "source_file": rec.get("source_file"),
                      "mode": rec.get("mode"), "file_sample": rec.get("sample_id"), "fit": rec.get("fit")}
        self._prov_changed = False
        self._clean = [round(float(v), 2) for v in rec["values"]]
        self._set_values(self._clean)
        self._refresh()

    def reload(self, active: dict | None, error: str = "") -> None:
        """New session or setup: show its active calibration (or an empty editor)."""
        self._set_active(active, error)
        rec = (active or {}).get("record")
        if rec:
            self.load_record(rec)
        else:
            self.new_editor()
        self.set_status("")

    def reload_from_service(self) -> None:
        """Read the active calibration of the current setup and show it (session / setup change)."""
        act = self.refresh_active()
        self.reload(act, self._active_error)

    @property
    def active_error(self) -> str:
        return self._active_error

    def draft(self) -> dict | None:
        """The unsaved editor state (kept over a session expiry), or None."""
        if not self.is_dirty():
            return None
        return {"values": self.get_calibration(), "prov": dict(self._prov), "base": self._base}

    def restore_draft(self, d: dict) -> None:
        self._base = d.get("base")
        self._prov = dict(d.get("prov") or {"origin": "manual"})
        self._prov_changed = True
        self._set_values(d.get("values") or [0.0] * 6)
        self.set_status("Unsaved calibration values of the expired session restored — review, then Save.",
                        "warning")
        self._refresh()

    def confirm_discard(self, action: str) -> bool:
        """True when the editor has no unsaved values, or the user saved or discarded them."""
        if not self.is_dirty():
            return True
        ans = QMessageBox.question(
            self, "Unsaved calibration",
            f"The BD calibration editor has unsaved values ({', '.join(f'{v:g}' for v in self.get_calibration())}"
            f" mV).\n\nSave them before {action}?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if ans == QMessageBox.StandardButton.Save:
            return self.save() is not None
        return ans == QMessageBox.StandardButton.Discard

    # ------------------------------------------------------------------ service actions
    def _default_name(self, ctx: dict) -> str:
        sample = (self._prov.get("file_sample") if self._prov.get("origin") == "fit" else None) \
            or ctx.get("sample_id") or "sample"
        return f"{sample} λ/2 {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    def save(self, as_new: bool = False, name: str | None = None) -> dict | None:
        """Store the editor values as a record.  On failure the values stay in the editor."""
        ctx = self._ctx()
        c = self._client(ctx)
        if c is None:
            return None
        vals = self.get_calibration()
        if not any(vals):
            self.set_status("All zeros means no calibration — nothing to save.", "warning")
            return None
        base = self._base if (not as_new and self._base and self._base.get("setup") == ctx.get("setup")) else None
        if base is not None and not self.is_dirty():
            self.set_status(f"No changes since {describe_record(base, short=True)}.")
            return base
        if as_new and name is None:
            name, ok = QInputDialog.getText(self, "Save calibration as", "Name:", text=self._default_name(ctx))
            if not ok:
                return None
        fit = self._prov.get("origin") == "fit"
        rec = {"setup": ctx.get("setup"), "values": vals, "unit": "mV", "origin": "fit" if fit else "manual",
               "sample_id": (self._prov.get("file_sample") if fit else (ctx.get("sample_id") or None)),
               "mode": self._prov.get("mode") if fit else None,
               "source_file": self._prov.get("source_file") if fit else None,
               "fit": dict(self._prov.get("fit") or {}, edited_after_fit=bool(self._prov.get("edited")))
               if fit else None,
               "name": (name or (base or {}).get("name") or self._default_name(ctx)).strip()}
        try:
            saved = c.call("calibration.save", {"record": rec, "base": base["id"] if base else None}, 10.0)
        except ServiceError as exc:
            self.set_status(f"Not saved: {error_text(exc)} — the values are still in the editor.", "error")
            self.message.emit("error", f"calibration not saved: {error_text(exc)}")
            return None
        self.load_record(saved)
        active_id = ((self._active or {}).get("record") or {}).get("id")
        self.set_status(f"Saved {describe_record(saved, short=True)}."
                        + ("" if active_id == saved["id"] else " Activate it to use it for this experiment."), "ok")
        self.message.emit("info", f"Calibration saved: {describe_record(saved)}")
        return saved

    def activate(self, interactive: bool = True) -> bool:
        ctx = self._ctx()
        c = self._client(ctx)
        if c is None:
            return False
        rec = self._base
        if rec is None or self.is_dirty():
            self.set_status("Save the editor's values first; only saved calibrations can be activated.", "warning")
            return False
        if rec.get("setup") != ctx.get("setup"):
            self.set_status(f"This calibration belongs to the {rec.get('setup')} setup.", "error")
            return False
        problems = []
        sample = ctx.get("sample_id") or ""
        if rec.get("sample_id") and sample and rec["sample_id"] != sample:
            problems.append(f"it was measured on sample {rec['sample_id']!r}; the current sample is {sample!r}")
        if rec.get("mode") and ctx.get("mode") and rec["mode"] != ctx["mode"]:
            problems.append(f"it is a {rec['mode']} calibration; the service runs {ctx['mode']} instruments")
        if problems:
            if not interactive:
                self.set_status("Not activated: " + "; ".join(problems) + ".", "warning")
                return False
            ans = QMessageBox.question(self, "Activate calibration",
                                       "Activate this calibration anyway?\n\n• " + "\n• ".join(problems),
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                       QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return False
        try:
            act = c.call("calibration.activate", {"setup": ctx.get("setup"), "id": rec["id"],
                                                  "sample_id": sample or None}, 10.0)
        except ServiceError as exc:
            self.set_status(f"Not activated: {error_text(exc)}", "error")
            return False
        self._set_active(act)
        self.set_status(f"Active for this experiment: {describe_record(rec, short=True)}"
                        + (" (the running measurement keeps the calibration it started with)"
                           if self._running else "") + ".", "ok")
        self.message.emit("info", f"Calibration activated for the {ctx.get('setup')} setup"
                          f"{' and sample ' + repr(sample) if sample else ''}: {describe_record(rec)}")
        return True

    def deactivate(self) -> bool:
        ctx = self._ctx()
        c = self._client(ctx)
        if c is None:
            return False
        try:
            act = c.call("calibration.activate", {"setup": ctx.get("setup"), "id": None}, 10.0)
        except ServiceError as exc:
            self.set_status(f"Not deactivated: {error_text(exc)}", "error")
            return False
        self._set_active(act)
        self.set_status("No calibration active: new files record none.", "ok")
        self.message.emit("info", f"Calibration deactivated for the {ctx.get('setup')} setup")
        return True

    def refresh_active(self) -> dict | None:
        """Read the active calibration of the current setup from the service (not a cache)."""
        ctx = self._ctx()
        c = ctx.get("client")
        if c is None or not c.session:
            self._set_active(None)
            return None
        if not c.has_feature(FEATURE):
            self._set_active(None, "the running device service is older and has no calibration records")
            return None
        try:
            act = c.call("calibration.active", {"setup": ctx.get("setup")}, 10.0)
        except ServiceError as exc:
            self._set_active(None, f"could not be read: {error_text(exc)}")
            return None
        self._set_active(act)
        return act

    def load_saved(self, record_id: str | None = None) -> bool:
        """Fetch the saved calibrations of this setup from the service and open one in the editor."""
        ctx = self._ctx()
        c = self._client(ctx)
        if c is None:
            return False
        try:
            recs = c.call("calibration.list", {"setup": ctx.get("setup")}, 10.0)
        except ServiceError as exc:
            self.set_status(f"Saved calibrations could not be read: {error_text(exc)}", "error")
            return False
        act = self.refresh_active()
        if not recs:
            self.set_status(f"No calibration saved for the {SETUP_LABEL.get(ctx.get('setup'), '')} setup yet.",
                            "warning")
            return False
        active_id = ((act or {}).get("record") or {}).get("id")
        if record_id is None:
            record_id = self._choose_record(recs, active_id)
        rec = next((r for r in recs if r["id"] == record_id), None)
        if rec is None:
            return False
        if not self.confirm_discard("opening another calibration"):
            return False
        self.load_record(rec)
        self.set_status(f"Opened {describe_record(rec, short=True)} from the service.")
        return True

    def _choose_record(self, recs: list[dict], active_id: str | None) -> str | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Saved calibrations")
        v = QVBoxLayout(dlg)
        lw = QListWidget()
        for r in recs:
            it = QListWidgetItem(("★ " if r["id"] == active_id else "   ") + describe_record(r))
            it.setData(Qt.ItemDataRole.UserRole, r["id"])
            lw.addItem(it)
            if r["id"] == active_id or lw.currentRow() < 0:
                lw.setCurrentItem(it)
        v.addWidget(QLabel("★ = active for this experiment"))
        v.addWidget(lw)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lw.itemDoubleClicked.connect(lambda _i: dlg.accept())
        v.addWidget(bb)
        dlg.resize(760, 320)
        if dlg.exec() != QDialog.DialogCode.Accepted or lw.currentItem() is None:
            return None
        return lw.currentItem().data(Qt.ItemDataRole.UserRole)

    # ------------------------------------------------------------------ fit & import
    def _folders(self) -> list[str]:
        base = os.path.expanduser((self._dir() if self._dir else "") or "").strip()
        if not base:
            return []
        day = os.path.join(base, datetime.now().strftime("%Y%m%d"))
        return [d for d in (day, base) if os.path.isdir(d)]

    def fit_import(self) -> bool:
        """Button: choose a calibration scan explicitly (best matches first), then fit it."""
        ctx = self._ctx()
        infos = []
        for p in candidate_files(self._folders()):
            info = read_file_info(p)
            if info.get("error") or info.get("scan_type") not in (None, "TIME"):
                continue
            if info.get("calibration_scan") or bd_fit.is_time_calibration(p):
                infos.append(info)
        infos.sort(key=lambda i: (len(file_problems(i, ctx)), -(i.get("mtime") or 0)))
        path = self._choose_file(infos, ctx)
        if not path:
            return False
        return self.import_file(path)

    def _choose_file(self, infos: list[dict], ctx: dict) -> str | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Choose a calibration scan")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"Calibration time scans in {' and '.join(self._folders()) or '(no data folder)'} — "
                           f"files that match the {ctx.get('setup')} setup, sample "
                           f"{ctx.get('sample_id') or '(none)'!r} and {ctx.get('mode') or 'current'} mode first."))
        cols = ["File", "Recorded", "Setup", "Sample", "Mode", "Status", "Check"]
        t = QTableWidget(len(infos), len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for r, i in enumerate(infos):
            probs = file_problems(i, ctx)
            vals = [i["name"], i.get("timestamp") or _fmt_time(i.get("mtime")), i.get("setup") or "?",
                    i.get("sample_id") if i.get("sample_id") is not None else "?", i.get("mode") or "?",
                    i.get("status") or "?", "matches" if not probs else f"{len(probs)} mismatch(es)"]
            for col, val in enumerate(vals):
                it = QTableWidgetItem(str(val))
                it.setToolTip("\n".join(probs) or "matches the current experiment")
                t.setItem(r, col, it)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        if infos:
            t.selectRow(0)
        v.addWidget(t)
        chosen: dict = {"path": None}
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        browse = bb.addButton("Browse…", QDialogButtonBox.ButtonRole.ActionRole)

        def do_browse():
            start = (self._folders() or [""])[0]
            path, _ = QFileDialog.getOpenFileName(dlg, "Choose a calibration scan", start, "HDF5 scans (*.h5)")
            if path:
                chosen["path"] = path
                dlg.accept()
        browse.clicked.connect(do_browse)

        def accept():
            r = t.currentRow()
            if r >= 0:
                chosen["path"] = infos[r]["path"]
                dlg.accept()
        bb.accepted.connect(accept)
        bb.rejected.connect(dlg.reject)
        t.cellDoubleClicked.connect(lambda _r, _c: accept())
        v.addWidget(bb)
        dlg.resize(900, 360)
        dlg.exec()
        return chosen["path"]

    def import_file(self, path: str, auto: bool = False) -> bool:
        """Check *path* against the current experiment, fit it and put the six values into the
        editor as an unsaved draft.  ``auto``: called for the scan just acquired -- no dialogs
        except the unsaved-edits question; a mismatch or a failed fit is only reported."""
        ctx = self._ctx()
        info = read_file_info(path)
        if info.get("sample_id") == "sample" and not ctx.get("sample_id"):
            info["sample_id"] = ""               # the engine's file-name placeholder for "no sample set"
        problems = file_problems(info, ctx)
        name = os.path.basename(path)
        if problems:
            if auto:
                self.set_status(f"{name} was not fitted automatically: " + "; ".join(problems) + ".", "warning")
                return False
            ans = QMessageBox.question(
                self, "Calibration file does not match",
                f"{name}\n\n• " + "\n• ".join(problems) + "\n\nUse this file for the current experiment anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                self.set_status(f"{name} not used.")
                return False
        if not self.confirm_discard(f"putting the fit of {name} into the editor"):
            return False
        try:
            result = bd_fit.fit_file(path)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.set_status(f"Could not read {name}: {exc}", "error")
            if not auto:
                QMessageBox.warning(self, "Could not read file", f"{name}\n\n{exc}")
            return False
        if not result.ok:
            self.set_status(f"Fit failed for {name} — {result.reason}. The editor was left unchanged.", "error")
            if not auto:
                QMessageBox.warning(self, "Could not fit this file",
                                    f"{name} could not be fitted.\n\n{result.reason}.\n\n"
                                    "The calibration values were left unchanged.")
            return False
        fit = {"n_plateaus": len(result.all_plateaus), "n_candidates": int(result.n_candidates),
               "spacing_cv": float(result.spacing_cv), "mean_step_mV": float(result.mean_step_V) * 1000.0,
               "midpoint_mV": float(result.midpoint_mV),
               "accepted_mismatches": problems}
        self._base = None                        # a new fit is a new calibration (Save -> version 1)
        self._prov = {"origin": "fit", "source_file": os.path.abspath(path), "mode": info.get("mode"),
                      "file_sample": info.get("sample_id") or None, "file_setup": info.get("setup"), "fit": fit}
        self._prov_changed = True
        self._set_values([round(v, 2) for v in result.levels_mV])
        extra = ""
        if result.n_candidates > 1:
            extra = (f" {result.n_candidates} evenly spaced runs of 6 were possible; took the one centred on "
                     f"zero (centre {result.midpoint_mV:+.2f} mV).")
        self.set_status(f"Fitted {name} — {len(result.all_plateaus)} plateau(s) found, 6 imported.{extra} "
                        "Review the values, then Save and Activate.", "ok")
        self._refresh()
        return True

    def fit_path(self, path: str) -> bool:
        """Compatibility: fit a file chosen elsewhere (with the usual checks)."""
        return self.import_file(path)

    # ------------------------------------------------------------------ display
    def set_status(self, text: str, level: str = "") -> None:
        color = {"error": "#f38ba8", "warning": "#fab387", "ok": "#a6e3a1"}.get(level, "#6c7086")
        self._status_lbl.setStyleSheet(f"color:{color};font-size:10px;")
        self._status_lbl.setText(text)

    def status_text(self) -> str:
        return self._status_lbl.text()

    def _set_active(self, active: dict | None, error: str = "") -> None:
        self._active = active if (active or {}).get("record") else None
        self._active_error = error
        self._refresh()
        self.active_changed.emit(self._active)

    def set_running(self, running: bool, snap: dict | None = None) -> None:
        """A run started (with its frozen calibration) or ended."""
        self._running = running
        self._run_snapshot = snap if running else None
        if running:
            self.run_lbl.setText("The running measurement uses " + (describe_record(snap) if snap else
                                 "no λ/2 calibration") + " (frozen at its start).")
        self.run_lbl.setVisible(running)

    def context_changed(self) -> None:
        """The sample, setup or mode changed: refresh the warnings."""
        self._refresh()

    def _refresh(self) -> None:
        ctx = self._ctx()
        dirty = self.is_dirty()
        if self._base:
            title = f"Editor — {describe_record(self._base, short=True)}"
        else:
            title = "Editor — new calibration"
        self.cal_grp.setTitle(title + ("  • unsaved" if dirty else ""))
        p = self._prov
        if p.get("origin") == "fit":
            prov = (f"From the fit of {os.path.basename(p.get('source_file') or '?')} "
                    f"(setup {p.get('file_setup') or (self._base or {}).get('setup') or '?'}, sample "
                    f"{p.get('file_sample') if p.get('file_sample') is not None else 'unknown'}, "
                    f"{p.get('mode') or 'mode unknown'})" + (" — edited by hand after the fit" if p.get("edited")
                                                             else ""))
        elif p.get("origin") == "migrated":
            prov = "Migrated from an earlier Samba_mini: sample, acquisition mode and source are unknown."
        else:
            prov = "Entered by hand."
        if self._base and not dirty:
            prov += f"  Saved by {self._base.get('creator') or 'unknown'}, {_fmt_time(self._base.get('created'))}."
        self.prov_lbl.setText(prov)
        active_rec = (self._active or {}).get("record")
        if active_rec:
            warn = []
            sample = ctx.get("sample_id") or ""
            for_sample = (self._active or {}).get("sample_id")
            if (for_sample or "") != sample:
                warn.append(f"activated for sample {for_sample!r}" if for_sample else "activated without a sample")
                warn[-1] += f"; the current sample is {sample!r}" if sample else "; no sample is set now"
            if active_rec.get("sample_id") is None:
                warn.append("the sample it was measured on is unknown")
            if active_rec.get("mode") and ctx.get("mode") and active_rec["mode"] != ctx["mode"]:
                warn.append(f"{active_rec['mode']} calibration on {ctx['mode']} instruments")
            text = (f"<b style='color:#89b4fa'>Active for this experiment</b> (written into new files): "
                    f"{describe_record(active_rec)}")
            if warn:
                text += "<br><span style='color:#fab387'>⚠ " + "; ".join(warn) + "</span>"
        elif self._active_error:
            text = f"<span style='color:#f38ba8'>Active calibration {self._active_error}.</span>"
        else:
            text = ("<b style='color:#89b4fa'>No calibration active</b> for the "
                    f"{SETUP_LABEL.get(ctx.get('setup'), '')} setup: new files record no λ/2 calibration.")
        self.active_lbl.setText(text)
        logged_in = bool(ctx.get("client") is not None and getattr(ctx.get("client"), "session", None))
        active_id = (active_rec or {}).get("id")
        self.save_btn.setEnabled(logged_in and any(self.get_calibration()) and (dirty or self._base is None))
        self.save_as_btn.setEnabled(logged_in and any(self.get_calibration()))
        self.activate_btn.setEnabled(logged_in and self._base is not None and not dirty
                                     and self._base.get("id") != active_id)
        self.deactivate_btn.setEnabled(logged_in and active_rec is not None)
        self._load_btn.setEnabled(logged_in)
        step = ("②" if dirty and p.get("origin") == "fit" else "③" if dirty else
                "④" if self._base is not None and self._base.get("id") != active_id else "")
        parts = ["① Acquire (Calibration tab, Start)", "② Fit & review", "③ Save", "④ Activate"]
        self.steps.setText("   →   ".join(f"<b style='color:#cdd6f4'>{s}</b>" if step and s.startswith(step)
                                          else s for s in parts))
        self.editor_changed.emit()
