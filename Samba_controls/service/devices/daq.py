"""Acquisition/output card (NI-DAQ in the rig) -- shared logic.

Mirrors MiniMoke's ``dac_class.py`` structure: one finite AO task (two
modulation outputs + coil command) and one finite AI task (balanced diodes,
intensity diode), both start-triggered by one rising edge on the trigger
terminal, ``round(rate*t)+1`` samples with the extra last AO sample holding the
output (and the extra AI sample discarded).

Change (finding F1): the new coil value is the *first* sample of the buffer
and ``settle_s`` worth of samples are generated before the acquisition window.
The AI samples recorded during settling are discarded.  The coil therefore
steps at the trigger and the reported mean only covers the settled window, all
on the card's sample clock.

Hardware primitives are implemented by subclasses (``NiDaq``, ``SimDaq``).
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import numpy as np

from core.model import ArgInfo, AttributeInfo, CommandInfo, DevState
from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.base import CallContext, Device

MONITOR_WINDOW_S = 0.1
STATIC_RATE_HZ = 10_000.0     # MiniMoke set_outputs_and_reset timing
STATIC_SAMPLES = 2


class DaqBase(Device):
    KIND = "daq"
    MONITORED = ["balanced_V", "intensity_V"]
    MONITOR_INTERVAL_S = 1.0

    def __init__(self, name: str, config: dict | None = None, simulated: bool = False):
        config = dict(config or {})
        self.rate = float(config.get("sampling_rate_Hz", 50_000.0))
        self.coil_min = float(config.get("coil_min_V", -6.0))
        self.coil_max = float(config.get("coil_max_V", 6.0))
        self.ATTRIBUTES = [
            AttributeInfo("coil_command_V", "float", "V", "rw", self.coil_min, self.coil_max,
                          "Coil amplifier command written on the coil AO channel. MiniMoke "
                          "labels this number 'A'; the amplifier transfer is not known to software.",
                          "Coil command"),
            AttributeInfo("ao1_V", "float", "V", "r", description="AC_Output1 constant level", label="AO1"),
            AttributeInfo("ao2_V", "float", "V", "r", description="AC_Output2 constant level", label="AO2"),
            AttributeInfo("balanced_V", "float", "V", "r",
                          description=f"Balanced-diode mean over a {MONITOR_WINDOW_S} s window",
                          label="Balanced diodes"),
            AttributeInfo("intensity_V", "float", "V", "r",
                          description=f"Intensity-diode mean over a {MONITOR_WINDOW_S} s window",
                          label="Intensity diode"),
            AttributeInfo("sampling_rate_Hz", "float", "Hz", "r", label="Sampling rate"),
            AttributeInfo("last_acq_id", "int", "", "r", description="id of the last acquisition"),
        ]
        self.COMMANDS = [
            CommandInfo("set_outputs", [
                ArgInfo("coil_V", "float", "V", self.coil_min, self.coil_max),
                ArgInfo("ao1_V", "float", "V", -10.0, 10.0, default=0.0, required=False),
                ArgInfo("ao2_V", "float", "V", -10.0, 10.0, default=0.0, required=False),
            ], description="Write constant outputs immediately (static generation)."),
            CommandInfo("zero_outputs", [], description="Set coil and AO1/AO2 to 0 V and stop tasks."),
        ]
        super().__init__(name, config, simulated)
        self.coil_V = 0.0
        self.ao_V = [0.0, 0.0]
        self.outputs_known = False      # False until we have written the outputs ourselves
        self._timing: tuple[float, int] | None = None
        self._acq_cfg: dict | None = None
        self._acq_seq = 0
        self._pending: dict | None = None
        self._seq_lock = threading.Lock()

    # ------------------------------------------------------------ primitives
    def _hw_open(self) -> None: ...
    def _hw_close(self) -> None: ...
    def _hw_timing(self, rate: float, n: int) -> None: ...
    def _hw_start(self, buffer: np.ndarray) -> None: ...
    def _hw_wait_read(self, n: int, timeout_s: float) -> tuple[np.ndarray, np.ndarray]: ...
    def _hw_static(self, values: list[float], timeout_s: float) -> None: ...
    def _hw_stop(self) -> None: ...

    # ------------------------------------------------------------ lifecycle
    def _connect(self) -> None:
        self._hw_open()
        self._timing = None
        self._pending = None

    def _disconnect(self) -> None:
        try:
            self._hw_stop()
        finally:
            self._hw_close()

    def cleanup(self, ctx: CallContext) -> list[str]:
        if not self.usable:
            return [f"{self.name}: not connected; coil output state unknown"]
        try:
            self.static_outputs(0.0, 0.0, 0.0)
            return []
        except Exception as exc:  # noqa: BLE001
            self.set_fault(exc, "cleanup")
            return [f"{self.name}: could not zero outputs: {exc}"]

    # ------------------------------------------------------------ checks
    def check_coil(self, coil_V: float) -> float:
        coil_V = float(coil_V)
        if not math.isfinite(coil_V) or not (self.coil_min <= coil_V <= self.coil_max):
            raise ServiceError(ErrorCode.OUT_OF_RANGE,
                               f"coil command {coil_V} V outside [{self.coil_min}, {self.coil_max}] V",
                               {"min": self.coil_min, "max": self.coil_max})
        return coil_V

    # ------------------------------------------------------------ static outputs
    def static_outputs(self, coil_V: float, ao1_V: float = 0.0, ao2_V: float = 0.0) -> None:
        coil_V = self.check_coil(coil_V)
        self._pending = None
        self._hw_static([float(ao1_V), float(ao2_V), coil_V], timeout_s=2.0)
        self._timing = None             # AO task re-timed for the static write
        self.coil_V, self.ao_V = coil_V, [float(ao1_V), float(ao2_V)]
        self.outputs_known = True
        self.store("coil_command_V", self.make_reading("coil_command_V", coil_V, "write"))

    # ------------------------------------------------------------ acquisition
    def configure_acquisition(self, acq_time_s: float, settle_s: float = 0.0) -> dict:
        if not (acq_time_s > 0) or acq_time_s > 600:
            raise ServiceError(ErrorCode.OUT_OF_RANGE, f"acquisition time {acq_time_s} s")
        if settle_s < 0 or settle_s > 600:
            raise ServiceError(ErrorCode.OUT_OF_RANGE, f"settle time {settle_s} s")
        n_acq = max(1, round(self.rate * acq_time_s))
        n_settle = max(0, round(self.rate * settle_s))
        self._acq_cfg = {"n_acq": n_acq, "n_settle": n_settle, "n_total": n_settle + n_acq + 1,
                         "acq_time_s": n_acq / self.rate, "settle_s": n_settle / self.rate,
                         "rate": self.rate}
        return dict(self._acq_cfg)

    def start_acquisition(self, coil_V: float) -> dict:
        self.require_usable()
        if self._acq_cfg is None:
            raise ServiceError(ErrorCode.INVALID_STATE, "acquisition not configured")
        coil_V = self.check_coil(coil_V)
        cfg = self._acq_cfg
        n = cfg["n_total"]
        if self._timing != (self.rate, n):
            self._hw_timing(self.rate, n)
            self._timing = (self.rate, n)
        buffer = np.empty((3, n))
        buffer[0, :] = self.ao_V[0]
        buffer[1, :] = self.ao_V[1]
        buffer[2, :] = coil_V
        with self._seq_lock:
            self._acq_seq += 1
            acq_id = self._acq_seq
        self._pending = None
        t0 = time.time()
        self._hw_start(buffer)
        # From here on the coil holds coil_V (last sample is held after the run).
        self.coil_V = coil_V
        self.outputs_known = True
        self._pending = {"acq_id": acq_id, "t_trigger": t0, "coil_V": coil_V, **cfg}
        return {"acq_id": acq_id, "t_trigger": t0, "coil_V": coil_V,
                "settle_s": cfg["settle_s"], "acq_time_s": cfg["acq_time_s"]}

    def read_acquisition(self, acq_id: int, timeout_s: float | None = None) -> dict:
        pending = self._pending
        if pending is None or pending["acq_id"] != acq_id:
            raise ServiceError(ErrorCode.INVALID_STATE,
                               f"acquisition {acq_id} is not the pending one "
                               f"({pending and pending['acq_id']}); refusing to return stale data",
                               {"expected": pending and pending["acq_id"], "requested": acq_id})
        n = pending["n_total"]
        timeout = timeout_s if timeout_s is not None else pending["settle_s"] + pending["acq_time_s"] + 10.0
        try:
            ai0, ai1 = self._hw_wait_read(n, timeout)
        finally:
            self._pending = None
        ai0 = np.asarray(ai0, dtype=float)
        ai1 = np.asarray(ai1, dtype=float)
        if ai0.size != n or ai1.size != n:
            raise ServiceError(ErrorCode.DEVICE_FAULT, f"expected {n} samples, got {ai0.size}/{ai1.size}")
        s, e = pending["n_settle"], pending["n_settle"] + pending["n_acq"]
        bd, it = ai0[s:e], ai1[s:e]
        t_end = time.time()
        result = {
            "acq_id": acq_id,
            "coil_V": pending["coil_V"],
            "t_trigger": pending["t_trigger"],
            "t_window_start": pending["t_trigger"] + pending["settle_s"],
            "t_end": t_end,
            "settle_s": pending["settle_s"],
            "acq_time_s": pending["acq_time_s"],
            "n_samples": int(bd.size),
            "balanced_V": float(np.mean(bd)),
            "intensity_V": float(np.mean(it)),
        }
        self.store("balanced_V", self.make_reading("balanced_V", result["balanced_V"], "acquisition", t_end))
        self.store("intensity_V", self.make_reading("intensity_V", result["intensity_V"], "acquisition", t_end))
        self.store("last_acq_id", self.make_reading("last_acq_id", acq_id, "acquisition", t_end))
        return result

    def acquire(self, coil_V: float, timeout_s: float | None = None) -> dict:
        info = self.start_acquisition(coil_V)
        return self.read_acquisition(info["acq_id"], timeout_s)

    # ------------------------------------------------------------ attributes
    def _read_attribute(self, name: str) -> Any:
        if name == "coil_command_V":
            return self.coil_V if self.outputs_known else math.nan
        if name == "ao1_V":
            return self.ao_V[0] if self.outputs_known else math.nan
        if name == "ao2_V":
            return self.ao_V[1] if self.outputs_known else math.nan
        if name == "sampling_rate_Hz":
            return self.rate
        if name == "last_acq_id":
            return self._acq_seq
        if name in ("balanced_V", "intensity_V"):
            if not self.outputs_known:
                # A monitor acquisition rewrites the outputs with the current
                # values; if we never wrote them, we do not know what they are.
                raise ServiceError(ErrorCode.INVALID_STATE,
                                   "outputs not yet initialised by the service; set the coil "
                                   "command first (e.g. zero outputs)")
            saved = self._acq_cfg
            try:
                self.configure_acquisition(MONITOR_WINDOW_S, 0.0)
                result = self.acquire(self.coil_V)
            finally:
                self._acq_cfg = saved
            return result[name]
        raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, name)

    def _write_attribute(self, name: str, value: Any) -> Any:
        if name == "coil_command_V":
            self.static_outputs(value, *self.ao_V)
            return self.coil_V
        return super()._write_attribute(name, value)

    # ------------------------------------------------------------ commands
    def cmd_set_outputs(self, ctx: CallContext, coil_V: float, ao1_V: float = 0.0, ao2_V: float = 0.0):
        self.static_outputs(coil_V, ao1_V, ao2_V)
        return {"coil_V": self.coil_V, "ao1_V": self.ao_V[0], "ao2_V": self.ao_V[1]}

    def cmd_zero_outputs(self, ctx: CallContext):
        self.static_outputs(0.0, 0.0, 0.0)
        return {"coil_V": 0.0}

    def public_config(self) -> dict:
        return dict(self.config, sampling_rate_Hz=self.rate, coil_min_V=self.coil_min,
                    coil_max_V=self.coil_max)
