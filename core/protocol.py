"""Wire protocol shared by the device service (server) and its clients.

Transport
---------
ZeroMQ over TCP, bound to ``127.0.0.1`` only:

* **RPC** – server ``ROUTER`` / client ``DEALER``.  One JSON object per
  message.  Every request carries a unique ``id`` that the reply echoes, so a
  client can match replies to requests and discard late replies to requests it
  already gave up on (a client timeout never makes it accept a stale answer).
* **Events** – server ``PUB`` / client ``SUB``.  Two frames: topic, JSON body.
  Events are *notifications only* (state changes, readbacks, operation
  progress).  PUB/SUB may drop messages, so measurement data are never taken
  from events: clients pull operation output with ``op.wait`` (sequence
  numbered, loss-free).

Only JSON is exchanged.  There is no pickle and no remotely callable
evaluation: the server dispatches ``method`` names through an explicit table and
device commands through each device's declared command list.

Request::

    {"proto": "samba-mini/1", "id": "<hex>", "method": "device.read",
     "session": "<token or null>", "params": {...}, "timeout_s": 5.0}

Reply::

    {"proto": "samba-mini/1", "id": "<hex>", "ok": true, "result": ...}
    {"proto": "samba-mini/1", "id": "<hex>", "ok": false,
     "error": {"code": "LEASE_REQUIRED", "message": "...", "details": {...}}}
"""

from __future__ import annotations

import base64
import json
import math
import uuid
from typing import Any

import numpy as np

from core.version import PROTOCOL, PROTOCOL_MAJOR

DEFAULT_RPC_PORT = 47811
DEFAULT_EVENT_PORT = 47812
LOOPBACK = "127.0.0.1"

#: Upper bound accepted for a request's own timeout (seconds).  Long-running
#: work must use an operation handle instead of a long blocking call.
MAX_REQUEST_TIMEOUT_S = 120.0
DEFAULT_REQUEST_TIMEOUT_S = 10.0


class ErrorCode:
    """Structured error codes (stable strings, part of the protocol)."""

    BAD_REQUEST = "BAD_REQUEST"
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    UNKNOWN_METHOD = "UNKNOWN_METHOD"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_FAILED = "AUTH_FAILED"
    FORBIDDEN = "FORBIDDEN"
    LEASE_REQUIRED = "LEASE_REQUIRED"
    LEASE_HELD = "LEASE_HELD"
    LEASE_RECONCILING = "LEASE_RECONCILING"
    BUSY = "BUSY"
    UNKNOWN_DEVICE = "UNKNOWN_DEVICE"
    UNKNOWN_ATTRIBUTE = "UNKNOWN_ATTRIBUTE"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    READ_ONLY = "READ_ONLY"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    INVALID_VALUE = "INVALID_VALUE"
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
    DEVICE_FAULT = "DEVICE_FAULT"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    INVALID_STATE = "INVALID_STATE"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    INTERNAL = "INTERNAL"


class ServiceError(Exception):
    """An error with a protocol error code.  Raised on both sides."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": to_jsonable(self.details)}

    @classmethod
    def from_dict(cls, data: dict) -> "ServiceError":
        return cls(data.get("code", ErrorCode.INTERNAL), data.get("message", ""), data.get("details") or {})


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# JSON encoding with explicit, safe handling of numbers and arrays
# ---------------------------------------------------------------------------
#
# Non-finite floats are encoded as {"__float__": "nan"|"inf"|"-inf"} so that a
# missing reading (NaN) survives the round trip unambiguously and is never
# turned into 0 or null.  Numpy arrays are sent as base64 of little-endian raw
# bytes with dtype/shape; only numeric dtypes are accepted on decode.

_ALLOWED_DTYPES = {"<f8", "<f4", "<i8", "<i4", "<i2", "<u1", "|u1", "|b1", "<u4", "<u8"}


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, (int, np.integer)) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        if math.isnan(value):
            return {"__float__": "nan"}
        if math.isinf(value):
            return {"__float__": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        arr = np.ascontiguousarray(obj)
        if arr.dtype.kind not in "fiub":
            raise TypeError(f"array dtype {arr.dtype} not transferable")
        arr = arr.astype(arr.dtype.newbyteorder("<"), copy=False)
        return {
            "__ndarray__": base64.b64encode(arr.tobytes()).decode("ascii"),
            "dtype": arr.dtype.str,
            "shape": list(arr.shape),
        }
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "to_dict"):
        return to_jsonable(obj.to_dict())
    raise TypeError(f"Object of type {type(obj).__name__} is not transferable")


def from_jsonable(obj: Any) -> Any:
    if isinstance(obj, list):
        return [from_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        if "__float__" in obj and len(obj) == 1:
            token = obj["__float__"]
            return {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}[token]
        if "__ndarray__" in obj:
            dtype = obj.get("dtype")
            if dtype not in _ALLOWED_DTYPES:
                raise ServiceError(ErrorCode.BAD_REQUEST, f"array dtype {dtype!r} not allowed")
            raw = base64.b64decode(obj["__ndarray__"])
            shape = tuple(int(n) for n in obj.get("shape", []))
            return np.frombuffer(raw, dtype=np.dtype(dtype)).reshape(shape).copy()
        return {k: from_jsonable(v) for k, v in obj.items()}
    return obj


def dumps(obj: Any) -> bytes:
    return json.dumps(to_jsonable(obj), allow_nan=False, separators=(",", ":")).encode("utf-8")


def loads(data: bytes) -> Any:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError(ErrorCode.BAD_REQUEST, f"malformed message: {exc}") from exc
    return from_jsonable(raw)


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------

def make_request(method: str, params: dict | None = None, session: str | None = None,
                 timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S, request_id: str | None = None) -> dict:
    return {
        "proto": PROTOCOL,
        "id": request_id or new_id(),
        "method": method,
        "session": session,
        "params": params or {},
        "timeout_s": float(timeout_s),
    }


def make_reply(request_id: str, result: Any = None) -> dict:
    return {"proto": PROTOCOL, "id": request_id, "ok": True, "result": result}


def make_error(request_id: str | None, error: ServiceError) -> dict:
    return {"proto": PROTOCOL, "id": request_id, "ok": False, "error": error.to_dict()}


def check_protocol(proto: str | None) -> None:
    """Raise PROTOCOL_MISMATCH unless *proto* has our major version."""
    if not isinstance(proto, str) or not proto.startswith("samba-mini/"):
        raise ServiceError(ErrorCode.PROTOCOL_MISMATCH, f"unsupported protocol {proto!r}")
    try:
        major = int(proto.split("/", 1)[1].split(".")[0])
    except ValueError as exc:
        raise ServiceError(ErrorCode.PROTOCOL_MISMATCH, f"unsupported protocol {proto!r}") from exc
    if major != PROTOCOL_MAJOR:
        raise ServiceError(ErrorCode.PROTOCOL_MISMATCH,
                           f"server speaks {PROTOCOL}, client sent {proto}")


def validate_request(msg: Any) -> dict:
    if not isinstance(msg, dict):
        raise ServiceError(ErrorCode.BAD_REQUEST, "request must be a JSON object")
    check_protocol(msg.get("proto"))
    rid = msg.get("id")
    if not isinstance(rid, str) or not (8 <= len(rid) <= 64):
        raise ServiceError(ErrorCode.BAD_REQUEST, "request id missing or invalid")
    method = msg.get("method")
    if not isinstance(method, str) or not method:
        raise ServiceError(ErrorCode.BAD_REQUEST, "method missing")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        raise ServiceError(ErrorCode.BAD_REQUEST, "params must be an object")
    session = msg.get("session")
    if session is not None and not isinstance(session, str):
        raise ServiceError(ErrorCode.BAD_REQUEST, "session must be a string")
    try:
        timeout_s = float(msg.get("timeout_s", DEFAULT_REQUEST_TIMEOUT_S))
    except (TypeError, ValueError) as exc:
        raise ServiceError(ErrorCode.BAD_REQUEST, "timeout_s must be a number") from exc
    if not (0 < timeout_s <= MAX_REQUEST_TIMEOUT_S):
        timeout_s = min(max(timeout_s, 0.1), MAX_REQUEST_TIMEOUT_S)
    return {"id": rid, "method": method, "params": params, "session": session, "timeout_s": timeout_s}
