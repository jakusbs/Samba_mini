"""Hall probe (Magnet-Physik USB Teslameter) -- shared logic and the real driver.

From MiniMoke ``hallsensor_class.py``: pythonnet loads the 64-bit
``MagnetPhysik.Usb.dll`` (``clr.AddReference``), ``MP.HallProbe()``,
``Speed = 1`` (470 Hz), integration set through ``Filter`` = number of filter
points (0 = off, 2..255), ``get_Tesla()`` returns tesla.

Changes: the DLL path comes from the machine configuration (no user name in
code); a missing probe raises instead of reading 0.0 mT; zeroing is bounded.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

from core.model import ArgInfo, AttributeInfo, CommandInfo
from core.paths import has_download_mark
from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.base import CallContext, Device

log = logging.getLogger(__name__)

# MiniMoke get_sampling_rate(): speed byte -> samples per second
SPEED_TO_RATE = {15: 4.17, 14: 6.25, 13: 8.33, 12: 10, 11: 12.5, 10: 16.7, 9: 16.7, 8: 19.6,
                 7: 33.2, 6: 39, 5: 50, 4: 62, 3: 123, 2: 242, 1: 470}
MAX_FILTER = 255



class HallBase(Device):
    KIND = "hall"
    MONITORED = ["field_mT"]
    MONITOR_INTERVAL_S = 1.0
    ATTRIBUTES = [
        AttributeInfo("field_mT", "float", "mT", "r", description="Measured field (integrated)",
                      label="Hall field"),
        AttributeInfo("integration_time_s", "float", "s", "rw", 0.0, 0.6,
                      "Integration time = filter points / sampling rate (max 255 points)",
                      "Integration time"),
        AttributeInfo("speed", "int", "", "r", description="Speed byte (1 = 470 Hz)"),
        AttributeInfo("sampling_rate_Hz", "float", "Hz", "r"),
        AttributeInfo("filter_points", "int", "", "r"),
    ]
    COMMANDS = [
        CommandInfo("zero", [ArgInfo("timeout_s", "float", "s", 1.0, 120.0, default=30.0, required=False)],
                    timeout_s=125.0, description="Zero the probe (must be at zero field)"),
        CommandInfo("read_field", [], mutating=False, description="One field reading"),
    ]

    def __init__(self, name, config=None, simulated=False):
        super().__init__(name, config, simulated)
        self.speed = int(self.config.get("speed", 1))
        self.integration_s = float(self.config.get("default_integration_s", 0.5))
        self.filter_points = 0

    @property
    def rate(self) -> float:
        return float(SPEED_TO_RATE.get(self.speed, 1.0))

    # primitives
    def hw_set_filter(self, points: int) -> None: ...
    def hw_read_tesla(self) -> float: ...
    def hw_zero(self, timeout_s: float, ctx: CallContext) -> None: ...

    def set_integration(self, seconds: float) -> float:
        """MiniMoke: Filter = clamp(round(t*rate), 0, 255); actual t = Filter/rate."""
        points = max(min(MAX_FILTER, round(float(seconds) * self.rate)), 0)
        self.hw_set_filter(points)
        self.filter_points = points
        self.integration_s = points / self.rate if points else 0.0
        return self.integration_s

    def read_mT(self, ctx: CallContext | None = None) -> dict:
        """Wait one integration window, then read (MiniMoke ``read_mT``)."""
        t0 = time.time()
        self.sleep(self.integration_s, ctx)
        tesla = self.hw_read_tesla()
        t1 = time.time()
        if tesla is None or not math.isfinite(float(tesla)):
            raise ServiceError(ErrorCode.DEVICE_FAULT, "Hall probe returned no value")
        value = float(tesla) * 1000.0
        self.store("field_mT", self.make_reading("field_mT", value, "device", t1))
        return {"field_mT": value, "t_start": t0, "t_end": t1, "integration_s": self.integration_s}

    def _read_attribute(self, name: str) -> Any:
        if name == "field_mT":
            return self.read_mT()["field_mT"]
        if name == "integration_time_s":
            return self.integration_s
        if name == "speed":
            return self.speed
        if name == "sampling_rate_Hz":
            return self.rate
        if name == "filter_points":
            return self.filter_points
        raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, name)

    def _write_attribute(self, name, value):
        if name == "integration_time_s":
            return self.set_integration(value)
        return super()._write_attribute(name, value)

    def cmd_zero(self, ctx: CallContext, timeout_s: float = 30.0):
        self.hw_zero(timeout_s, ctx)
        return {"zeroed": True}

    def cmd_read_field(self, ctx: CallContext):
        return self.read_mT(ctx)


class MagnetPhysikHall(HallBase):
    def __init__(self, name, config=None, simulated=False):
        super().__init__(name, config, simulated=False)
        self.dll_path = os.path.expandvars(str(self.config.get("dll_path", "")))
        self.identity.update({"model": "Magnet-Physik USB Teslameter", "resource": self.dll_path,
                              "driver": "pythonnet"})
        self._probe = None

    def _connect(self) -> None:
        if not self.dll_path or not os.path.isfile(self.dll_path):
            raise ServiceError(ErrorCode.NOT_SUPPORTED,
                               f"MagnetPhysik.Usb.dll not found at {self.dll_path!r}; set "
                               "[hall] dll_path in the machine configuration")
        if has_download_mark(self.dll_path):
            # .NET Framework refuses to load such files ("network location",
            # HRESULT 0x80131515).  This is why MiniMoke hard-coded the copy in
            # Documents\minimoke\libs.  We do not unblock files ourselves.
            raise ServiceError(ErrorCode.NOT_SUPPORTED,
                               f"{self.dll_path} is marked as downloaded from the internet (Zone.Identifier), "
                               "so .NET will not load it. Point [hall] dll_path to an unmarked copy (MiniMoke's "
                               "Documents\\minimoke\\libs) or, after checking its origin, unblock it "
                               "(file Properties > Unblock, or Unblock-File).")
        try:
            import clr  # pythonnet
        except ImportError as exc:
            raise ServiceError(ErrorCode.NOT_SUPPORTED, f"pythonnet not installed: {exc}") from exc
        dll_dir = os.path.dirname(self.dll_path)
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(dll_dir)
            except OSError:
                pass
        clr.AddReference(self.dll_path)
        import MagnetPhysik as MP  # noqa: N814 - .NET namespace
        try:
            probe = MP.HallProbe()
            if self.config.get("read_only"):
                self.speed = int(probe.Speed)          # read, not written
            else:
                probe.Speed = self.speed
        except Exception as exc:
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, f"Hall probe not found: {exc}") from exc
        self._probe = probe
        if self.config.get("read_only"):
            self.filter_points = int(probe.Filter)
            self.integration_s = self.filter_points / self.rate if self.filter_points else 0.0
        else:
            self.set_integration(self.integration_s)

    def _disconnect(self) -> None:
        self._probe = None

    def hw_set_filter(self, points: int) -> None:
        if self.config.get("read_only"):
            raise ServiceError(ErrorCode.FORBIDDEN, "read-only mode: Hall filter not changed")
        self._probe.Filter = points

    def hw_read_tesla(self) -> float:
        return float(self._probe.get_Tesla())

    def hw_zero(self, timeout_s: float, ctx: CallContext) -> None:
        self._probe.Zero = True
        deadline = time.monotonic() + timeout_s
        while self._probe.Zero:
            if time.monotonic() > deadline:
                raise ServiceError(ErrorCode.TIMEOUT, f"Hall zeroing not finished after {timeout_s} s")
            self.sleep(0.05, ctx)
