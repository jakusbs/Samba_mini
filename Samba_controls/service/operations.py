"""Asynchronous operations (operation handles).

Long work -- a field sweep, a time trace, a stage move -- runs as an
:class:`Operation` in its own thread.  The client gets an id immediately and
pulls output with ``op.wait(op_id, since_seq, timeout_s)``: every record has a
sequence number, so nothing is lost even if PUB/SUB notifications are dropped,
and a record can never be attributed to the wrong operation.

Pause holds the operation at its next point boundary (``checkpoint``) with all
outputs unchanged.  Abort sets the cancel token, which every wait and every
queued instrument call observes.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable

from core.protocol import ErrorCode, ServiceError, new_id
from Samba_controls.service.worker import CancelToken

log = logging.getLogger(__name__)

RUNNING, PAUSED, COMPLETED, ABORTED, FAILED = "RUNNING", "PAUSED", "COMPLETED", "ABORTED", "FAILED"
FINAL_STATES = {COMPLETED, ABORTED, FAILED}

#: finished operations are kept this long for clients to collect their output
RETENTION_S = 600.0
MAX_RECORDS_PER_WAIT = 2000


class Operation:
    def __init__(self, device: str, name: str, args: dict, owner_token: str, owner_user: str):
        self.id = new_id()
        self.device = device
        self.name = name
        self.args = args
        self.owner_token = owner_token
        self.owner_user = owner_user
        self.state = RUNNING
        self.created = time.time()
        self.finished: float | None = None
        self.progress = 0.0
        self.message = ""
        self.error: dict | None = None
        self.result: Any = None
        self.cancel = CancelToken()
        self._resume = threading.Event()
        self._resume.set()
        self.pause_requested = False
        self.paused_total_s = 0.0
        self._records: list[tuple[int, dict]] = []
        self._next_seq = 1
        self._cond = threading.Condition()
        self.thread: threading.Thread | None = None
        self.on_update: Callable[["Operation"], None] | None = None

    # ------------------------------------------------------ worker side --
    def emit(self, record: dict) -> int:
        with self._cond:
            seq = self._next_seq
            self._next_seq += 1
            self._records.append((seq, record))
            self._cond.notify_all()
        self._updated()
        return seq

    def set_progress(self, fraction: float, message: str | None = None) -> None:
        with self._cond:
            self.progress = max(0.0, min(1.0, float(fraction)))
            if message is not None:
                self.message = message
            self._cond.notify_all()
        self._updated()

    def checkpoint(self) -> float:
        """Call between points.  Blocks while paused; raises CANCELLED on abort.

        Returns the seconds spent paused so the caller can defer its schedule.
        """
        self.cancel.raise_if_cancelled()
        if not self.pause_requested:
            return 0.0
        t0 = time.perf_counter()
        with self._cond:
            self.state = PAUSED
            self._cond.notify_all()
        self._updated()
        while not self._resume.wait(0.1):
            self.cancel.raise_if_cancelled()
        self.cancel.raise_if_cancelled()
        held = time.perf_counter() - t0
        with self._cond:
            self.paused_total_s += held
            if self.state == PAUSED:
                self.state = RUNNING
            self._cond.notify_all()
        self._updated()
        return held

    def auto_pause(self, message: str) -> None:
        """Request a pause from inside the operation (e.g. hardware error)."""
        self.pause_requested = True
        self._resume.clear()
        with self._cond:
            self.message = message
        self._updated()

    # ------------------------------------------------------ control side --
    def pause(self) -> None:
        if self.state in FINAL_STATES:
            raise ServiceError(ErrorCode.INVALID_STATE, f"operation already {self.state}")
        self.pause_requested = True
        self._resume.clear()
        self._updated()

    def resume(self) -> None:
        if self.state in FINAL_STATES:
            raise ServiceError(ErrorCode.INVALID_STATE, f"operation already {self.state}")
        self.pause_requested = False
        self._resume.set()
        self._updated()

    def abort(self, reason: str = "aborted by user") -> None:
        self.cancel.cancel(reason)
        self._resume.set()
        self._updated()

    def wait(self, since_seq: int = 0, timeout_s: float = 1.0, known_state: str | None = None) -> dict:
        """Long-poll: return as soon as there are records after *since_seq*,
        the operation finished, or its state differs from *known_state*."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._cond:
            while True:
                new = [r for r in self._records if r[0] > since_seq]
                if new or self.state in FINAL_STATES or (known_state is not None
                                                          and self.state != known_state):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            new = new[:MAX_RECORDS_PER_WAIT]
            return {
                "op_id": self.id,
                "state": self.state,
                "progress": self.progress,
                "message": self.message,
                "error": self.error,
                "result": self.result if self.state in FINAL_STATES else None,
                "pause_requested": self.pause_requested,
                "records": [{"seq": s, **r} for s, r in new],
                "last_seq": new[-1][0] if new else since_seq,
                "total_records": self._next_seq - 1,
            }

    def summary(self) -> dict:
        return {"op_id": self.id, "device": self.device, "name": self.name, "state": self.state,
                "progress": self.progress, "message": self.message, "owner": self.owner_user,
                "created": self.created, "finished": self.finished, "error": self.error,
                "pause_requested": self.pause_requested}

    def _finish(self, state: str, result: Any = None, error: dict | None = None) -> None:
        with self._cond:
            self.state = state
            self.result = result
            self.error = error
            self.finished = time.time()
            self._cond.notify_all()
        self._updated()

    def _updated(self) -> None:
        if self.on_update:
            try:
                self.on_update(self)
            except Exception:  # noqa: BLE001
                log.exception("operation update callback failed")


class OperationManager:
    """Runs at most one operation at a time (the setup is a single resource)."""

    def __init__(self, on_update: Callable[[Operation], None] | None = None):
        self._ops: dict[str, Operation] = {}
        self._lock = threading.Lock()
        self._on_update = on_update

    def active(self) -> Operation | None:
        with self._lock:
            for op in self._ops.values():
                if op.state not in FINAL_STATES:
                    return op
        return None

    def start(self, op: Operation, target: Callable[[Operation], Any]) -> Operation:
        with self._lock:
            self._purge()
            for other in self._ops.values():
                if other.state not in FINAL_STATES:
                    raise ServiceError(ErrorCode.BUSY,
                                       f"operation {other.device}.{other.name} is still {other.state}",
                                       {"op_id": other.id})
            self._ops[op.id] = op
        op.on_update = self._on_update

        def run():
            try:
                result = target(op)
            except ServiceError as exc:
                if exc.code == ErrorCode.CANCELLED or op.cancel.cancelled:
                    op._finish(ABORTED, error={"code": ErrorCode.CANCELLED,
                                               "message": op.cancel.reason or exc.message})
                else:
                    op._finish(FAILED, error=exc.to_dict())
            except Exception as exc:  # noqa: BLE001
                log.error("operation %s.%s crashed:\n%s", op.device, op.name, traceback.format_exc())
                op._finish(FAILED, error={"code": ErrorCode.INTERNAL, "message": str(exc)})
            else:
                if op.cancel.cancelled:
                    op._finish(ABORTED, result=result,
                               error={"code": ErrorCode.CANCELLED, "message": op.cancel.reason})
                else:
                    op._finish(COMPLETED, result=result)

        op.thread = threading.Thread(target=run, name=f"op-{op.name}", daemon=True)
        op.thread.start()
        return op

    def get(self, op_id: str) -> Operation:
        with self._lock:
            op = self._ops.get(op_id)
        if op is None:
            raise ServiceError(ErrorCode.UNKNOWN_OPERATION, f"no operation {op_id}")
        return op

    def list(self) -> list[dict]:
        with self._lock:
            return [op.summary() for op in self._ops.values()]

    def abort_all(self, reason: str, wait_s: float) -> bool:
        """Abort every running operation and wait; return True if all stopped."""
        with self._lock:
            running = [op for op in self._ops.values() if op.state not in FINAL_STATES]
        for op in running:
            op.abort(reason)
        deadline = time.monotonic() + wait_s
        for op in running:
            if op.thread is not None:
                op.thread.join(max(0.0, deadline - time.monotonic()))
        return all(op.state in FINAL_STATES for op in running)

    def _purge(self) -> None:
        now = time.time()
        for op_id in [k for k, op in self._ops.items()
                      if op.state in FINAL_STATES and op.finished and now - op.finished > RETENTION_S]:
            del self._ops[op_id]
