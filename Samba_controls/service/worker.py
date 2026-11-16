"""Per-instrument serialised execution.

Every physical instrument gets one :class:`DeviceWorker` thread.  All driver
calls for that instrument run there, in submission order, so two requests can
never talk to the same instrument at once (and .NET / NI-DAQ objects are only
touched from the thread that created them).

A job carries a deadline and an optional :class:`CancelToken`.  When the worker
picks a job up it first checks both: a job whose caller already gave up, or
whose operation was aborted, is *dropped without touching the hardware*.  This
is what prevents a timed-out request from issuing a late write after an abort.
A job that has already started cannot be interrupted from outside (driver calls
are blocking); it runs to completion and its result is discarded if nobody
waits for it any more.  Long device-side waits poll the cancel token.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

from core.protocol import ErrorCode, ServiceError

log = logging.getLogger(__name__)


class CancelToken:
    """Thread-safe cancellation flag with a reason."""

    def __init__(self):
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled") -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise ServiceError(ErrorCode.CANCELLED, self.reason or "cancelled")

    def wait(self, timeout: float) -> bool:
        """Sleep up to *timeout* s; return True if cancelled meanwhile."""
        return self._event.wait(max(0.0, timeout))


NEVER_CANCELLED = CancelToken()


class _Job:
    __slots__ = ("fn", "deadline", "cancel", "future", "label", "abandoned")

    def __init__(self, fn, deadline, cancel, label):
        self.fn = fn
        self.deadline = deadline
        self.cancel = cancel or NEVER_CANCELLED
        self.future: Future = Future()
        self.label = label
        self.abandoned = False


class DeviceWorker:
    def __init__(self, name: str):
        self.name = name
        self._queue: "queue.Queue[_Job | None]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"dev-{name}", daemon=True)
        self._lock = threading.Lock()
        self.current_label: str | None = None
        self.current_since: float | None = None
        self._stopped = False
        self._thread.start()

    # -- public ---------------------------------------------------------------
    def submit(self, fn: Callable[[], Any], timeout_s: float, cancel: CancelToken | None = None,
               label: str = "") -> _Job:
        if self._stopped:
            raise ServiceError(ErrorCode.DEVICE_UNAVAILABLE, f"{self.name}: worker stopped")
        job = _Job(fn, time.monotonic() + timeout_s, cancel, label)
        self._queue.put(job)
        return job

    def call(self, fn: Callable[[], Any], timeout_s: float, cancel: CancelToken | None = None,
             label: str = "") -> Any:
        """Run *fn* on the worker and wait for it (bounded)."""
        job = self.submit(fn, timeout_s, cancel, label)
        return self.wait(job, timeout_s)

    def wait(self, job: _Job, timeout_s: float) -> Any:
        try:
            return job.future.result(timeout=max(0.0, job.deadline - time.monotonic()) + 0.05
                                     if timeout_s is not None else None)
        except TimeoutError:
            job.abandoned = True
            started = job.future.running()
            raise ServiceError(
                ErrorCode.TIMEOUT,
                f"{self.name}: '{job.label or 'call'}' did not finish within {timeout_s:.3g} s"
                + (" (it is still running on the instrument; its outcome is unknown)" if started
                   else " (it was not started and will not run)"),
                {"device": self.name, "started": started, "label": job.label},
            ) from None

    @property
    def busy(self) -> bool:
        return self.current_label is not None

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    def stop(self) -> None:
        self._stopped = True
        self._queue.put(None)

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout)

    # -- thread ---------------------------------------------------------------
    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                break
            if job.abandoned or time.monotonic() > job.deadline:
                job.future.set_exception(ServiceError(
                    ErrorCode.TIMEOUT, f"{self.name}: '{job.label}' expired before it started",
                    {"device": self.name, "started": False}))
                continue
            if job.cancel.cancelled:
                job.future.set_exception(ServiceError(
                    ErrorCode.CANCELLED, f"{self.name}: '{job.label}' cancelled before it started "
                    f"({job.cancel.reason})", {"device": self.name, "started": False}))
                continue
            if not job.future.set_running_or_notify_cancel():
                continue
            self.current_label, self.current_since = job.label, time.monotonic()
            try:
                result = job.fn()
            except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
                job.future.set_exception(exc)
            else:
                job.future.set_result(result)
            finally:
                self.current_label = self.current_since = None
        # Fail anything still queued.
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            if job is not None and not job.future.done():
                job.future.set_exception(ServiceError(ErrorCode.DEVICE_UNAVAILABLE,
                                                      f"{self.name}: worker stopped"))
