"""What Samba_mini remembers, for whom, and how it is saved.

==========================  =====================================  ==========================
State                       Scope / storage                        Saving
==========================  =====================================  ==========================
User preferences            per account: service ``prefs``         autosaved (PrefsStore)
  (USER_PREF_KEYS)
Working state               per account *and* setup: service        autosaved (PrefsStore)
  (workflow draft, selected  ``prefs`` key ``working.<setup>``
  preset, experiment/sample,
  calibration-scan settings)
Measurement presets         per account and setup: service          explicit Save / Save as
  (workflow, params, optics) ``presets``                            (+ / S / C / R buttons)
Calibration records         per account: service ``calibrations``   explicit Save, then
  (immutable versions)      + active one per setup                  Activate
Machine configuration       shared: machine.toml (Samba_controls)   administrators only
Live device state           the device service (authoritative)      never stored by the GUI
==========================  =====================================  ==========================

A preset is a reusable *recipe*: it never carries the sample identity or the operator
(EXPERIMENT_KEYS).  Loading a preset, logging in or switching setup only fills widgets;
it never moves a stage, sets an output or configures an instrument.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.gui.bridge import SerialCaller

log = logging.getLogger(__name__)

#: Ordinary per-account preferences (autosaved).
USER_PREF_KEYS = ("data_dir", "default_operator", "lab_notebook", "export_csv", "kerr_display", "setup")
#: Sample identity and attribution of the current experiment (working state, never in presets).
EXPERIMENT_KEYS = ("operator", "sample_id", "device_id", "notes", "r_4wire_ohm", "r_2wire_ohm",
                   "fm_thickness_nm", "t_stack_nm")
#: Optical configuration of a measurement (part of a preset).
OPTICS_KEYS = ("incidence", "mirror_shift", "mirror_shift_by_incidence", "polarization", "lam2", "lam4",
               "noDC")
#: Legacy metadata keys of presets saved before this split (identity, never applied silently).
LEGACY_IDENTITY_KEYS = EXPERIMENT_KEYS + ("sample",)

WORKING_VERSION = 1
PRESET_SCHEMA = 2


def working_key(setup: str) -> str:
    return f"working.{setup}"


def _rounded(obj: Any) -> Any:
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return str(obj)
        return float(f"{obj:.10g}")
    if isinstance(obj, dict):
        return {str(k): _rounded(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_rounded(v) for v in obj]
    return obj


def canonical(obj: Any) -> str:
    """Stable text form for "has this changed?" comparisons (floats to 10 significant digits)."""
    return json.dumps(_rounded(obj), sort_keys=True, ensure_ascii=False)


def split_metadata(meta: dict) -> tuple[dict, dict]:
    """(experiment, optics) parts of the metadata box's values."""
    exp = {k: meta[k] for k in EXPERIMENT_KEYS if k in meta}
    opt = {k: meta[k] for k in OPTICS_KEYS if k in meta}
    return exp, opt


class PrefsStore(QObject):
    """The current account's preferences, autosaved to the service.

    ``set`` updates the local value at once and schedules a save; saves are sent one after
    another (``SerialCaller``) so an older value can never overtake a newer one.  A failed save
    keeps the unsaved values (``pending()``), reports the error through ``status_changed`` and is
    retried with the next change or ``retry()``.  ``reset`` starts a new session: replies to
    saves of the previous session are ignored.  Without a session (``enabled=False``) changes stay
    local -- they belong to nobody and are replaced at the next login.
    """

    status_changed = pyqtSignal(str, str)    # state: offline | saved | pending | saving | error ; text
    changed = pyqtSignal(str, object)        # key, value (local changes)

    def __init__(self, client_getter: Callable[[], Any], delay_ms: int = 400, parent=None):
        super().__init__(parent)
        self._client = client_getter
        self._values: dict = {}
        self._pending: dict = {}
        self._inflight: dict = {}
        self._epoch = 0
        self._enabled = False
        self.state = "offline"
        self.error = ""
        self._caller = SerialCaller(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._flush_async)

    # ------------------------------------------------------------------ session
    def reset(self, values: dict | None, enabled: bool, error: str = "") -> None:
        self._epoch += 1
        self._timer.stop()
        self._values = dict(values or {})
        self._pending.clear()
        self._inflight.clear()
        self._enabled = enabled
        if error:
            self._set_state("error", error)
        else:
            self._set_state("saved" if enabled else "offline", "")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ values
    def get(self, key: str, default: Any = None) -> Any:
        v = self._values.get(key)
        return default if v is None else v

    def values(self) -> dict:
        return dict(self._values)

    def set(self, key: str, value: Any, immediate: bool = False) -> None:
        if canonical(self._values.get(key)) == canonical(value) and key not in self._pending:
            return
        self._values[key] = value
        self.changed.emit(key, value)
        if not self._enabled:
            return
        self._pending[key] = value
        if self.state != "saving":
            self._set_state("pending", "")
        self._timer.start(0 if immediate else self._timer.interval())

    def update(self, values: dict) -> None:
        for k, v in values.items():
            self.set(k, v)

    def pending(self) -> dict:
        """Values not yet confirmed saved (in flight or waiting)."""
        return dict(self._inflight, **self._pending)

    # ------------------------------------------------------------------ saving
    def _set_state(self, state: str, text: str) -> None:
        self.state, self.error = state, (text if state == "error" else "")
        self.status_changed.emit(state, text)

    def retry(self) -> None:
        if self._enabled and (self._pending or self._inflight):
            self._pending = self.pending()
            self._inflight.clear()
            self._timer.start(0)

    def _flush_async(self) -> None:
        if not self._enabled or not self._pending or self._inflight:
            return                              # a save is in flight: its reply schedules the next
        c = self._client()
        if c is None or not c.session:
            self._set_state("error", "not saved: no session")
            return
        vals, self._pending = self._pending, {}
        self._inflight = vals
        epoch = self._epoch
        self._set_state("saving", "")

        def done(_res, exc):
            if epoch != self._epoch:
                return                          # a reply for an earlier session
            sent, self._inflight = self._inflight, {}
            if exc is not None:
                for k, v in sent.items():
                    self._pending.setdefault(k, v)          # newer edits win, nothing is lost
                from core.gui.bridge import error_text
                self._set_state("error", f"not saved: {error_text(exc)}")
                log.warning("preferences not saved: %s", exc)
                return
            if self._pending:
                self._timer.start(0)
            else:
                self._set_state("saved", "")
        self._caller.submit(lambda: c.call("prefs.set", {"values": vals}, 10.0), done)

    def shutdown(self) -> None:
        """Window closing: stop the save worker (after flush_sync)."""
        self._timer.stop()
        self._enabled = False
        self._caller.shutdown()

    def flush_sync(self, timeout_s: float = 10.0) -> str | None:
        """Save everything now (logout, window close).  Returns None or the error text."""
        if not self._enabled:
            return None
        self._timer.stop()
        vals = self.pending()
        if not vals:
            return None
        c = self._client()
        if c is None or not c.session:
            return "no session"
        epoch = self._epoch
        try:
            self._caller.call_sync(lambda: c.call("prefs.set", {"values": vals}, timeout_s), timeout_s + 2)
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            from core.gui.bridge import error_text
            self._set_state("error", f"not saved: {error_text(exc)}")
            return error_text(exc)
        if epoch == self._epoch:
            for k, v in vals.items():
                if canonical(self._pending.get(k, v)) == canonical(v):
                    self._pending.pop(k, None)
            self._inflight.clear()
            self._set_state("saved" if not self._pending else "pending", "")
        return None
