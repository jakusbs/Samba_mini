"""The setup lease: exclusive control of every conflicting mutation.

States::

    FREE ──acquire──▶ HELD ──release / expiry / force──▶ RECONCILING ──▶ FREE
                       ▲  │ heartbeat renews
                       └──┘

* Only one session holds the lease.  Every mutating request (attribute write,
  mutating command, operation start/pause/resume/abort, connect/disconnect,
  run begin/end) must come from the holder.
* The holder renews it with heartbeats; without them it expires after
  ``ttl_s``.
* Leaving HELD always goes through RECONCILING: outstanding operations are
  cancelled and waited for, then the configured cleanup runs.  Only after the
  operations have stopped is the lease FREE again.  If an operation does not
  stop in time the lease stays RECONCILING (new owners are refused) until an
  administrator forces it free after checking the setup.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from core.protocol import ErrorCode, ServiceError

log = logging.getLogger(__name__)


class LeaseManager:
    FREE, HELD, RECONCILING = "FREE", "HELD", "RECONCILING"

    def __init__(self, ttl_s: float, reconcile: Callable[[str], dict],
                 on_change: Callable[[dict], None] | None = None):
        self.ttl_s = float(ttl_s)
        self._reconcile = reconcile
        self._on_change = on_change
        self._lock = threading.RLock()
        self.state = self.FREE
        self.holder_token: str | None = None
        self.holder: dict | None = None        # public session info
        self.since: float | None = None
        self.expires: float | None = None       # monotonic
        self.last_reconcile: dict | None = None
        self._reconcile_thread: threading.Thread | None = None

    # -------------------------------------------------------------- query --
    def status(self) -> dict:
        with self._lock:
            remaining = None
            if self.state == self.HELD and self.expires is not None:
                remaining = round(max(0.0, self.expires - time.monotonic()), 2)
            return {"state": self.state, "holder": self.holder, "since": self.since,
                    "expires_in_s": remaining, "ttl_s": self.ttl_s,
                    "last_reconcile": self.last_reconcile}

    def is_holder(self, token: str | None) -> bool:
        with self._lock:
            return self.state == self.HELD and token is not None and token == self.holder_token

    def require(self, token: str | None) -> None:
        with self._lock:
            if self.state == self.HELD and token == self.holder_token:
                return
            if self.state == self.RECONCILING:
                raise ServiceError(ErrorCode.LEASE_RECONCILING,
                                   "the previous owner's work is being stopped and cleaned up",
                                   self.status())
            if self.state == self.HELD:
                raise ServiceError(ErrorCode.LEASE_HELD,
                                   f"the setup is controlled by {self.holder['user']} "
                                   f"({self.holder['client']})", self.status())
            raise ServiceError(ErrorCode.LEASE_REQUIRED, "take control of the setup first",
                               self.status())

    # ------------------------------------------------------------ changes --
    def acquire(self, token: str, session_public: dict) -> dict:
        with self._lock:
            if self.state == self.HELD and self.holder_token == token:
                self.expires = time.monotonic() + self.ttl_s
                return self.status()
            if self.state == self.RECONCILING:
                raise ServiceError(ErrorCode.LEASE_RECONCILING,
                                   "the previous owner's work is still being stopped", self.status())
            if self.state == self.HELD:
                raise ServiceError(ErrorCode.LEASE_HELD,
                                   f"the setup is controlled by {self.holder['user']} "
                                   f"({self.holder['client']}) since "
                                   f"{time.strftime('%H:%M:%S', time.localtime(self.since))}",
                                   self.status())
            self.state = self.HELD
            self.holder_token = token
            self.holder = dict(session_public)
            self.since = time.time()
            self.expires = time.monotonic() + self.ttl_s
            st = self.status()
        log.info("lease granted to %s (%s)", session_public.get("user"), session_public.get("client"))
        self._notify(st)
        return st

    def heartbeat(self, token: str) -> bool:
        with self._lock:
            if self.state == self.HELD and self.holder_token == token:
                self.expires = time.monotonic() + self.ttl_s
                return True
            return False

    def release(self, token: str, reason: str = "released", wait: bool = True) -> dict:
        with self._lock:
            if not (self.state == self.HELD and self.holder_token == token):
                raise ServiceError(ErrorCode.LEASE_REQUIRED, "you do not hold the lease", self.status())
        self._begin_reconcile(reason, wait)
        return self.status()

    def force(self, reason: str) -> dict:
        """Admin action: reconcile now even if held by someone else or stuck."""
        with self._lock:
            state = self.state
        if state == self.HELD:
            self._begin_reconcile(reason, wait=True)
        elif state == self.RECONCILING:
            with self._lock:
                self.last_reconcile = dict(self.last_reconcile or {}, forced=True,
                                           note="lease forced free by an administrator; "
                                                "hardware state not verified by software")
                self._free()
            self._notify(self.status())
        return self.status()

    def check_expiry(self) -> None:
        with self._lock:
            expired = (self.state == self.HELD and self.expires is not None
                       and time.monotonic() > self.expires)
            holder = self.holder
        if expired:
            log.warning("lease of %s expired (no heartbeat for %.1f s); stopping its work",
                        holder and holder.get("user"), self.ttl_s)
            self._begin_reconcile("heartbeat lost", wait=False)

    # ----------------------------------------------------------- internals --
    def _begin_reconcile(self, reason: str, wait: bool) -> None:
        with self._lock:
            if self.state != self.HELD:
                return
            self.state = self.RECONCILING
            holder = self.holder
        self._notify(self.status())

        def work():
            try:
                report = self._reconcile(reason)
            except Exception as exc:  # noqa: BLE001
                log.exception("reconcile failed")
                report = {"ok": False, "stopped": False, "problems": [f"reconcile crashed: {exc}"]}
            report = dict(report, reason=reason, previous_holder=holder, t=time.time())
            with self._lock:
                self.last_reconcile = report
                if report.get("stopped", True):
                    self._free()
                else:
                    log.error("lease stays RECONCILING: operations did not stop (%s)", report)
            self._notify(self.status())

        if wait:
            work()
        else:
            self._reconcile_thread = threading.Thread(target=work, name="lease-reconcile", daemon=True)
            self._reconcile_thread.start()

    def _free(self) -> None:
        self.state = self.FREE
        self.holder_token = None
        self.holder = None
        self.since = None
        self.expires = None

    def _notify(self, status: dict) -> None:
        if self._on_change:
            try:
                self._on_change(status)
            except Exception:  # noqa: BLE001
                log.exception("lease notification failed")
