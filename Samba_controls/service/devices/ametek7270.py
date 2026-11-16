"""Ametek / Signal Recovery DSP 7270 over PyVISA (USB or Ethernet socket).

Re-implemented from MiniMoke ``src/classes/ametek7270_class.py`` which was
derived from PyMeasure's Ametek7270 driver:

    Copyright (c) 2013-2023 PyMeasure Developers -- MIT licence.
    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software ... (full text: https://github.com/pymeasure/pymeasure/blob/master/LICENSE.txt)

Protocol details preserved from MiniMoke (verified on the rig):

* read and write termination are NUL (``\\x00``);
* every *set* command is followed by a read that must return an empty string
  (``check_set_errors``); a non-empty answer means the response stream is
  misaligned;
* the Ethernet socket interface sends one extra status-prompt chunk per
  response; it is counted at connect with an ``ID`` probe (after flushing stale
  responses) and drained after every transaction;
* on an I/O error: reopen the session, VISA device-clear, re-probe, retry the
  query, up to ``reconnect_attempts`` times with growing waits;
* a socket resource on port 50000 falls back to 50001 at connect (the 7270
  listens on both; a crashed client can keep 50000 occupied).

PyMeasure itself is not used, so the backend does not depend on it.
``pnputil`` USB re-enumeration (needs administrator rights) is disabled unless
``allow_usb_reset = true``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.lockin import (SENSITIVITIES, TIME_CONSTANTS, LockinBase,
                                                   truncated_discrete)

log = logging.getLogger(__name__)

PROTOCOL_PROBE_TIMEOUT_MS = 500
PROTOCOL_FLUSH_MAX = 32
PROTOCOL_PROBE_CHUNK_MAX = 4
RECONNECT_SETTLE_S = 1.0
# The Ethernet status chunk follows the reply within milliseconds on the lab
# network.  Waiting the full read timeout (2 s) for one that never comes was
# the "freeze" seen on 16 Nov: some points took +2.0 s, later almost all.
DRAIN_TIMEOUT_MS = 100
# A status chunk that turns up after the drain gave up is discarded before the
# next command, so it can never be read as that command's reply.
LATE_FLUSH_TIMEOUT_MS = 20
USB_RESET_FROM_ATTEMPT = 3
USB_RESET_SETTLE_S = 5.0


class Ametek7270(LockinBase):
    def __init__(self, name, config=None, simulated=False):
        super().__init__(name, config, simulated=False)
        c = self.config
        self.resource = str(c.get("resource", "USB0::0x0A2D::0x001B::15342534::RAW"))
        self.timeout_ms = int(c.get("timeout_ms", 2000))
        self.reconnect_attempts = int(c.get("reconnect_attempts", 6))
        self.allow_usb_reset = bool(c.get("allow_usb_reset", False))
        self.visa_library = c.get("visa_library", "")
        self.identity.update({"model": "Ametek 7270", "resource": self.resource, "driver": "pyvisa"})
        self._rm = None
        self._inst = None
        self._resource_in_use = None
        self._extra_chunks = 0
        self._maybe_late = False          # a status chunk may still arrive: flush before the next command
        self.missing_chunks = 0           # replies whose status chunk did not come (diagnostics)

    # ------------------------------------------------------------ session
    def _connect(self) -> None:
        try:
            import pyvisa
        except ImportError as exc:
            raise ServiceError(ErrorCode.NOT_SUPPORTED, f"pyvisa not installed: {exc}") from exc
        if self._rm is None:
            self._rm = pyvisa.ResourceManager(self.visa_library) if self.visa_library else pyvisa.ResourceManager()
        candidates = [self.resource]
        if "::SOCKET" in self.resource.upper() and "::50000::" in self.resource:
            candidates.append(self.resource.replace("::50000::", "::50001::"))
        last = None
        for res in candidates:
            try:
                self._open(res)
                ident = self._ask_once("ID")
                ver = self._ask_once("VER")
                self.identity.update({"serial": None, "resource": res, "firmware": ver, "id": ident})
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("7270 at %s: %s", res, exc)
                self._close()
        raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, f"7270 not reachable at {candidates}: {last}")

    def _open(self, resource: str) -> None:
        self._inst = self._rm.open_resource(resource, read_termination="\x00", write_termination="\x00")
        self._inst.timeout = self.timeout_ms
        self._resource_in_use = resource
        self._sync_protocol()

    def _close(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()      # sends the FIN the 7270 needs to free its socket slot
            except Exception:  # noqa: BLE001
                pass
        self._inst = None

    def _disconnect(self) -> None:
        self._close()

    def _sync_protocol(self) -> None:
        inst = self._inst
        old = inst.timeout
        try:
            inst.timeout = PROTOCOL_PROBE_TIMEOUT_MS
            flushed = 0
            while flushed < PROTOCOL_FLUSH_MAX:
                try:
                    inst.read()
                    flushed += 1
                except Exception:  # noqa: BLE001 - timeout: queue empty
                    break
            inst.write("ID")
            chunks = 0
            while chunks < PROTOCOL_PROBE_CHUNK_MAX:
                try:
                    inst.read()
                    chunks += 1
                except Exception:  # noqa: BLE001 - timeout: no more chunks
                    break
            if chunks == 0:
                raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, "no answer to ID probe")
            self._extra_chunks = max(chunks - 1, 0)
            self._maybe_late = False
            if flushed:
                log.info("7270: flushed %d stale response(s)", flushed)
            if self._extra_chunks:
                log.info("7270: %d extra status chunk(s) per response (socket framing)", self._extra_chunks)
        finally:
            inst.timeout = old

    def _drain(self) -> None:
        """Read the interface's extra status chunk(s) after a reply, waiting briefly only."""
        if not self._extra_chunks:
            return
        inst = self._inst
        old = inst.timeout
        inst.timeout = DRAIN_TIMEOUT_MS
        try:
            for _ in range(self._extra_chunks):
                try:
                    inst.read()
                except Exception:  # noqa: BLE001 - missing chunk: do not wait for it
                    self.missing_chunks += 1
                    self._maybe_late = True
                    n = self.missing_chunks
                    if n in (1, 10, 100) or n % 1000 == 0:
                        log.warning("7270: status chunk missing after a reply (%d so far); not waiting "
                                    "for it", n)
                    break
        finally:
            inst.timeout = old

    def _flush_late(self) -> None:
        """Discard a status chunk that arrived after _drain stopped waiting for it."""
        if not self._maybe_late:
            return
        self._maybe_late = False
        inst = self._inst
        old = inst.timeout
        inst.timeout = LATE_FLUSH_TIMEOUT_MS
        try:
            for _ in range(PROTOCOL_FLUSH_MAX):
                try:
                    inst.read()
                except Exception:  # noqa: BLE001 - nothing pending
                    break
        finally:
            inst.timeout = old

    # ------------------------------------------------------------ transactions
    def _ask_once(self, command: str) -> str:
        if self._inst is None:
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, "7270 session closed")
        self._flush_late()
        self._inst.write(command)
        result = self._inst.read().strip()
        self._drain()
        return result

    def ask(self, command: str, cancel=None) -> str:
        try:
            return self._ask_once(command)
        except Exception as exc:  # noqa: BLE001 - any VISA/socket failure
            log.warning("7270 I/O error on %r (%s); re-opening the link", command, exc)
        last = None
        for attempt in range(1, self.reconnect_attempts + 1):
            if cancel is not None and cancel.cancelled:
                raise ServiceError(ErrorCode.CANCELLED, "lock-in reconnect cancelled")
            if attempt >= USB_RESET_FROM_ATTEMPT and self.allow_usb_reset:
                self._reset_usb_device()
            self.hw_reconnect(quiet=True)
            time.sleep(RECONNECT_SETTLE_S * attempt)
            try:
                result = self._ask_once(command)
                log.info("7270 link recovered after %d attempt(s)", attempt)
                self.set_state(self.state, "connected")
                return result
            except Exception as exc:  # noqa: BLE001
                last = exc
        self.set_fault(f"no response after {self.reconnect_attempts} reconnect attempts: {last}", command)
        raise ServiceError(ErrorCode.DEVICE_FAULT, f"7270 not responding ({last}); power-cycle it "
                           "or check the cable/network", {"device": self.name})

    def _set(self, command: str) -> None:
        """Write a set command and check the (empty) acknowledgement."""
        if self._inst is None:
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, "7270 session closed")
        self._flush_late()
        self._inst.write(command)
        response = self._inst.read()
        self._drain()
        if response != "":
            # PyMeasure (and hence MiniMoke) only logs this.  We log it and
            # re-learn the framing so the following reads stay aligned.
            log.error("7270: unexpected reply %r to %r (response stream misaligned); resyncing",
                      response, command)
            try:
                self._sync_protocol()
            except Exception as exc:  # noqa: BLE001
                log.warning("7270 resync failed: %s", exc)

    def hw_reconnect(self, quiet: bool = False) -> None:
        res = self._resource_in_use or self.resource
        self._close()
        try:
            self._inst = self._rm.open_resource(res, read_termination="\x00", write_termination="\x00")
            self._inst.timeout = self.timeout_ms
            try:
                self._inst.clear()
            except Exception as exc:  # noqa: BLE001
                log.debug("7270 device clear skipped: %s", exc)
            try:
                self._sync_protocol()
            except Exception:  # noqa: BLE001
                pass
            log.info("7270 session reopened (device cleared)")
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                self.set_fault(exc, "reconnect")
                raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, f"7270 reconnect failed: {exc}") from exc
            log.warning("7270 reconnect failed: %s", exc)

    def _reset_usb_device(self) -> bool:
        if sys.platform != "win32":
            return False
        parts = str(self._resource_in_use or "").split("::")
        if len(parts) < 4 or not parts[0].upper().startswith("USB"):
            return False
        try:
            instance = f"USB\\VID_{int(parts[1], 0):04X}&PID_{int(parts[2], 0):04X}\\{parts[3]}"
            proc = subprocess.run(["pnputil", "/restart-device", instance], capture_output=True,
                                  text=True, timeout=30)
        except Exception as exc:  # noqa: BLE001
            log.warning("USB re-enumeration not possible: %s", exc)
            return False
        if proc.returncode == 0:
            time.sleep(USB_RESET_SETTLE_S)
            return True
        log.warning("pnputil refused (exit %s); administrator rights are needed", proc.returncode)
        return False

    # ------------------------------------------------------------ primitives
    def hw_read_xy(self) -> tuple[float, float]:
        reply = self.ask("XY.")
        if reply == "":
            raise ServiceError(ErrorCode.DEVICE_FAULT,
                               "7270 returned no XY data (reference mode not single?)")
        try:
            x_s, y_s = reply.split(",")[:2]
            return float(x_s), float(y_s)
        except ValueError as exc:
            raise ServiceError(ErrorCode.DEVICE_FAULT, f"7270: cannot parse XY reply {reply!r}") from exc

    def hw_reference_mode(self, mode: int) -> None:
        if mode not in (0, 1, 2):
            raise ServiceError(ErrorCode.INVALID_VALUE, "reference mode must be 0, 1 or 2")
        # MiniMoke sends REFMODE through ask() (reply expected empty).
        self.ask(f"REFMODE {mode}")

    def hw_set(self, what: str, value: float) -> float:
        if what == "oscillator_V":
            v = min(max(float(value), 0.0), 5.0)
            self._set(f"OA. {v:g}")
            return v
        if what == "sensitivity_V":
            v = truncated_discrete(float(value), SENSITIVITIES)
            self._set(f"SEN {SENSITIVITIES.index(v)}")
            return v
        if what == "frequency_Hz":
            v = min(max(float(value), 0.0), 2.5e5)
            self._set(f"OF. {v:g}")
            return v
        if what == "time_constant_s":
            v = truncated_discrete(float(value), TIME_CONSTANTS)
            self._set(f"TC {TIME_CONSTANTS.index(v)}")
            return v
        if what == "phase_deg":
            v = float(value) % 360.0
            self._set(f"REFP. {v:g}")
            return v
        raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, what)

    def hw_get(self, what: str):
        if what == "oscillator_V":
            return float(self.ask("OA."))
        if what == "sensitivity_V":
            return SENSITIVITIES[int(self.ask("SEN"))]
        if what == "frequency_Hz":
            return float(self.ask("OF."))
        if what == "time_constant_s":
            return TIME_CONSTANTS[int(self.ask("TC"))]
        if what == "phase_deg":
            return float(self.ask("REFP."))
        if what == "reference_mode":
            return int(self.ask("REFMODE"))
        if what == "identification":
            return f"{self.ask('ID')}/{self.ask('VER')}"
        raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, what)
