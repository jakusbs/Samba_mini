"""Local accounts, sessions, preferences and presets.

Accounts live in SQLite (``%LOCALAPPDATA%\\SambaMini\\data\\accounts.sqlite3``).
Passwords are hashed with Argon2id via ``argon2-cffi`` (random salt per hash,
library-default work factors, which are re-checked at every login and upgraded
transparently).  Plain or reversibly encrypted passwords are never stored.

The schema is versioned in ``schema_version``; migrations run in order inside
a transaction after a file copy of the database is made next to it.

These accounts organise who controls the shared setup and attribute data to an
operator.  They do not isolate files from a Windows administrator or from
other processes of the same Windows user.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from core.protocol import ErrorCode, ServiceError

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
MIN_PASSWORD_LEN = 8
ROLES = ("admin", "operator")

SCHEMA_VERSION = 1

_MIGRATIONS = {
    1: [
        """CREATE TABLE users (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               username TEXT NOT NULL UNIQUE COLLATE NOCASE,
               pw_hash TEXT NOT NULL,
               role TEXT NOT NULL CHECK (role IN ('admin','operator')),
               full_name TEXT NOT NULL DEFAULT '',
               disabled INTEGER NOT NULL DEFAULT 0,
               created REAL NOT NULL,
               last_login REAL
           )""",
        """CREATE TABLE prefs (
               user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               key TEXT NOT NULL,
               value TEXT NOT NULL,
               updated REAL NOT NULL,
               PRIMARY KEY (user_id, key)
           )""",
        """CREATE TABLE presets (
               user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               workflow TEXT NOT NULL,
               name TEXT NOT NULL,
               value TEXT NOT NULL,
               updated REAL NOT NULL,
               PRIMARY KEY (user_id, workflow, name)
           )""",
    ],
}


def make_hasher() -> PasswordHasher:
    """Argon2id hasher.  ``SAMBA_MINI_FAST_HASH=1`` lowers the cost for tests only."""
    if os.environ.get("SAMBA_MINI_FAST_HASH") == "1":
        return PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
    return PasswordHasher()


class AccountStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._hasher = make_hasher()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ------------------------------------------------------------ schema --
    def schema_version(self) -> int:
        cur = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
        if cur.fetchone() is None:
            return 0
        row = self._db.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0] or 0)

    def _migrate(self) -> None:
        with self._lock:
            current = self.schema_version()
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"accounts database schema {current} is newer than this "
                                   f"software ({SCHEMA_VERSION}); refusing to modify it")
            if current == SCHEMA_VERSION:
                return
            if current > 0:
                backup = self.path.with_name(f"{self.path.name}.v{current}.{int(time.time())}.bak")
                self._db.execute("PRAGMA wal_checkpoint(FULL)")
                shutil.copy2(self.path, backup)
                log.info("accounts database backed up to %s before migration", backup)
            self._db.execute("BEGIN")
            try:
                if current == 0:
                    self._db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied REAL NOT NULL)")
                for version in range(current + 1, SCHEMA_VERSION + 1):
                    for stmt in _MIGRATIONS[version]:
                        self._db.execute(stmt)
                    self._db.execute("INSERT INTO schema_version VALUES (?, ?)", (version, time.time()))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------- users --
    def user_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(self, username: str, password: str, role: str = "operator",
                    full_name: str = "") -> dict:
        if not isinstance(username, str) or not USERNAME_RE.match(username):
            raise ServiceError(ErrorCode.INVALID_VALUE,
                               "user name: 2-32 characters, letters, digits, '.', '_' or '-'")
        self._check_password(password)
        if role not in ROLES:
            raise ServiceError(ErrorCode.INVALID_VALUE, f"role must be one of {ROLES}")
        pw_hash = self._hasher.hash(password)
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO users (username, pw_hash, role, full_name, created) VALUES (?,?,?,?,?)",
                    (username, pw_hash, role, full_name or "", time.time()))
            except sqlite3.IntegrityError as exc:
                raise ServiceError(ErrorCode.INVALID_VALUE, f"user {username!r} already exists") from exc
            return self.get_user(cur.lastrowid)

    def get_user(self, user_id: int) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ServiceError(ErrorCode.INVALID_VALUE, "unknown user")
        return _public_user(row)

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [_public_user(r) for r in rows]

    def verify(self, username: str, password: str) -> dict:
        """Return the user on success; raise AUTH_FAILED otherwise (same message for all causes)."""
        failure = ServiceError(ErrorCode.AUTH_FAILED, "unknown user name or wrong password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise failure
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            # Spend comparable time so user names cannot be probed by timing.
            try:
                self._hasher.verify(self._hasher.hash("x" * 12), password + "\0")
            except VerificationError:
                pass
            raise failure
        try:
            self._hasher.verify(row["pw_hash"], password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            raise failure from None
        if row["disabled"]:
            raise ServiceError(ErrorCode.AUTH_FAILED, "this account is disabled")
        with self._lock:
            if self._hasher.check_needs_rehash(row["pw_hash"]):
                self._db.execute("UPDATE users SET pw_hash=? WHERE id=?",
                                 (self._hasher.hash(password), row["id"]))
            self._db.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), row["id"]))
        return _public_user(row)

    def change_password(self, user_id: int, old: str, new: str) -> None:
        user = self.get_user(user_id)
        self.verify(user["username"], old)
        self.set_password(user_id, new)

    def set_password(self, user_id: int, new: str) -> None:
        self._check_password(new)
        with self._lock:
            self._db.execute("UPDATE users SET pw_hash=? WHERE id=?", (self._hasher.hash(new), user_id))

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ROLES:
            raise ServiceError(ErrorCode.INVALID_VALUE, f"role must be one of {ROLES}")
        with self._lock:
            if role != "admin" and self._is_last_admin(user_id):
                raise ServiceError(ErrorCode.INVALID_STATE, "cannot remove the last administrator")
            self._db.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        with self._lock:
            if disabled and self._is_last_admin(user_id):
                raise ServiceError(ErrorCode.INVALID_STATE, "cannot disable the last administrator")
            self._db.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))

    def _is_last_admin(self, user_id: int) -> bool:
        row = self._db.execute("SELECT role, disabled FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None or row["role"] != "admin" or row["disabled"]:
            return False
        n = self._db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0]
        return n <= 1

    @staticmethod
    def _check_password(password: str) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
            raise ServiceError(ErrorCode.INVALID_VALUE,
                               f"password must have at least {MIN_PASSWORD_LEN} characters")
        if len(password) > 256:
            raise ServiceError(ErrorCode.INVALID_VALUE, "password too long")

    # ------------------------------------------------------- preferences --
    def get_prefs(self, user_id: int) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM prefs WHERE user_id=?", (user_id,)).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def set_prefs(self, user_id: int, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ServiceError(ErrorCode.INVALID_VALUE, "preferences must be an object")
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for key, value in values.items():
                    if not isinstance(key, str) or not key or len(key) > 64:
                        raise ServiceError(ErrorCode.INVALID_VALUE, f"bad preference key {key!r}")
                    if value is None:
                        self._db.execute("DELETE FROM prefs WHERE user_id=? AND key=?", (user_id, key))
                    else:
                        self._db.execute(
                            "INSERT INTO prefs (user_id, key, value, updated) VALUES (?,?,?,?) "
                            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                            (user_id, key, json.dumps(value), time.time()))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get_prefs(user_id)

    def list_presets(self, user_id: int, workflow: str | None = None) -> list[dict]:
        with self._lock:
            if workflow:
                rows = self._db.execute(
                    "SELECT workflow, name, value, updated FROM presets WHERE user_id=? AND workflow=? "
                    "ORDER BY name", (user_id, workflow)).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT workflow, name, value, updated FROM presets WHERE user_id=? "
                    "ORDER BY workflow, name", (user_id,)).fetchall()
        return [{"workflow": r["workflow"], "name": r["name"], "value": json.loads(r["value"]),
                 "updated": r["updated"]} for r in rows]

    def save_preset(self, user_id: int, workflow: str, name: str, value: dict) -> None:
        if not workflow or not isinstance(workflow, str) or len(workflow) > 64:
            raise ServiceError(ErrorCode.INVALID_VALUE, "bad workflow name")
        if not name or not isinstance(name, str) or len(name) > 80:
            raise ServiceError(ErrorCode.INVALID_VALUE, "preset name must be 1-80 characters")
        if not isinstance(value, dict):
            raise ServiceError(ErrorCode.INVALID_VALUE, "preset value must be an object")
        with self._lock:
            self._db.execute(
                "INSERT INTO presets (user_id, workflow, name, value, updated) VALUES (?,?,?,?,?) "
                "ON CONFLICT(user_id, workflow, name) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (user_id, workflow, name, json.dumps(value), time.time()))

    def delete_preset(self, user_id: int, workflow: str, name: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM presets WHERE user_id=? AND workflow=? AND name=?",
                                   (user_id, workflow, name))
            return cur.rowcount > 0


def _public_user(row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "full_name": row["full_name"], "disabled": bool(row["disabled"]),
            "created": row["created"], "last_login": row["last_login"]}


# ---------------------------------------------------------------------------
# Sessions (in memory; a backend restart requires a new login)
# ---------------------------------------------------------------------------

@dataclass
class Session:
    token: str
    user: dict
    client: str
    created: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def user_id(self) -> int:
        return self.user["id"]

    @property
    def is_admin(self) -> bool:
        return self.user.get("role") == "admin"

    def public(self) -> dict:
        return {"user": self.user["username"], "role": self.user["role"], "client": self.client,
                "session_id": self.token[:8]}


class SessionManager:
    def __init__(self, idle_timeout_s: float = 12 * 3600):
        self.idle_timeout_s = idle_timeout_s
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, user: dict, client: str) -> Session:
        token = secrets.token_urlsafe(32)
        s = Session(token=token, user=user, client=str(client or "client")[:40])
        with self._lock:
            self._sessions[token] = s
        return s

    def get(self, token: str | None) -> Session:
        if not token:
            raise ServiceError(ErrorCode.AUTH_REQUIRED, "log in first")
        with self._lock:
            s = self._sessions.get(token)
            if s is None:
                raise ServiceError(ErrorCode.AUTH_REQUIRED, "session expired or unknown; log in again")
            now = time.monotonic()
            if now - s.last_seen > self.idle_timeout_s:
                del self._sessions[token]
                raise ServiceError(ErrorCode.AUTH_REQUIRED, "session timed out; log in again")
            s.last_seen = now
            return s

    def drop(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def drop_user(self, user_id: int) -> None:
        with self._lock:
            for t in [t for t, s in self._sessions.items() if s.user_id == user_id]:
                del self._sessions[t]

    def update_user(self, user: dict) -> None:
        with self._lock:
            for s in self._sessions.values():
                if s.user_id == user["id"]:
                    s.user = user

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(s.public(), idle_s=round(time.monotonic() - s.last_seen, 1))
                    for s in self._sessions.values()]
