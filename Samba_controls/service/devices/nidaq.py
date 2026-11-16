"""NI-DAQmx implementation of the acquisition card (re-implemented from
MiniMoke ``src/classes/dac_class.py``; same channels, trigger and task
structure).

Only this module imports ``nidaqmx``, and only when the device connects.
"""

from __future__ import annotations

import logging

import numpy as np

from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.daq import STATIC_RATE_HZ, STATIC_SAMPLES, DaqBase

log = logging.getLogger(__name__)


class NiDaq(DaqBase):
    def __init__(self, name, config=None, simulated=False):
        super().__init__(name, config, simulated=False)
        c = self.config
        self.ch_ao1 = c.get("ao_mod1", "Dev1/ao1")
        self.ch_ao2 = c.get("ao_mod2", "Dev1/ao2")
        self.ch_coil = c.get("ao_coil", "Dev1/ao3")
        self.ch_bd = c.get("ai_balanced", "Dev1/ai0")
        self.ch_int = c.get("ai_intensity", "Dev1/ai1")
        self.trigger = c.get("trigger_terminal", "/Dev1/PFI0")
        self.identity.update({"model": "NI-DAQmx device", "resource": c.get("device", "Dev1"),
                              "driver": "nidaqmx"})
        self._out = self._in = self._trig = None

    def _hw_open(self) -> None:
        try:
            import nidaqmx
            import nidaqmx.system
            from nidaqmx.constants import Edge
        except ImportError as exc:
            raise ServiceError(ErrorCode.NOT_SUPPORTED, f"nidaqmx not installed: {exc}") from exc
        self._hw_close()
        try:
            out = nidaqmx.Task("SambaMiniOutput")
            inp = nidaqmx.Task("SambaMiniInput")
            trig = nidaqmx.Task("SambaMiniTrigger")
        except Exception as exc:
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE,
                               f"NI-DAQmx task creation failed (driver missing?): {exc}") from exc
        self._out, self._in, self._trig = out, inp, trig
        try:
            # Same channel order as MiniMoke: ao1, ao2, coils; ai0, ai1.
            out.ao_channels.add_ao_voltage_chan(self.ch_ao1)
            out.ao_channels.add_ao_voltage_chan(self.ch_ao2)
            out.ao_channels.add_ao_voltage_chan(self.ch_coil)
            inp.ai_channels.add_ai_voltage_chan(self.ch_bd)
            inp.ai_channels.add_ai_voltage_chan(self.ch_int)
            out.triggers.start_trigger.cfg_dig_edge_start_trig(self.trigger, trigger_edge=Edge.RISING)
            inp.triggers.start_trigger.cfg_dig_edge_start_trig(self.trigger, trigger_edge=Edge.RISING)
            trig.do_channels.add_do_chan(self.trigger)
            try:
                dev = nidaqmx.system.Device(str(self.config.get("device", "Dev1")))
                self.identity.update({"model": dev.product_type, "serial": str(dev.dev_serial_num)})
            except Exception:  # noqa: BLE001 - identity is informational
                pass
        except Exception:
            self._hw_close()
            raise

    def _hw_close(self) -> None:
        for task in (self._trig, self._out, self._in):
            if task is not None:
                try:
                    task.stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    task.close()
                except Exception:  # noqa: BLE001
                    pass
        self._out = self._in = self._trig = None

    def _fire_trigger(self) -> None:
        self._trig.write([True, False], auto_start=True)
        self._trig.stop()

    def _stop_io(self) -> None:
        for task in (self._out, self._in):
            try:
                task.stop()
            except Exception:  # noqa: BLE001 - stopping an idle task may raise
                pass

    def _hw_timing(self, rate: float, n: int) -> None:
        self._stop_io()
        self._out.timing.cfg_samp_clk_timing(rate, samps_per_chan=n)
        self._in.timing.cfg_samp_clk_timing(rate, samps_per_chan=n)

    def _hw_start(self, buffer: np.ndarray) -> None:
        # Finite tasks must be stopped before they can be rewritten
        # (DaqWriteError -200288 otherwise); arm the input first.
        self._stop_io()
        self._in.start()
        self._out.write(np.ascontiguousarray(buffer), auto_start=False)
        self._out.start()
        self._fire_trigger()

    def _hw_wait_read(self, n: int, timeout_s: float):
        try:
            self._out.wait_until_done(timeout=timeout_s)
            self._in.wait_until_done(timeout=timeout_s)
            data = self._in.read(number_of_samples_per_channel=n, timeout=timeout_s)
        finally:
            self._stop_io()
        return np.asarray(data[0], dtype=float), np.asarray(data[1], dtype=float)

    def _hw_static(self, values, timeout_s: float) -> None:
        self._stop_io()
        self._out.timing.cfg_samp_clk_timing(STATIC_RATE_HZ, samps_per_chan=STATIC_SAMPLES)
        data = np.tile(np.asarray(values, dtype=float), (STATIC_SAMPLES, 1)).T
        self._out.write(np.ascontiguousarray(data), auto_start=True)
        self._fire_trigger()
        # Unlike MiniMoke we wait for the two samples to be generated before
        # returning, so a following stop() cannot cut the write short.
        try:
            self._out.wait_until_done(timeout=timeout_s)
        finally:
            self._stop_io()

    def _hw_stop(self) -> None:
        if self._out is not None:
            self._stop_io()
