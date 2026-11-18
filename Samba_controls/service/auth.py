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

import hashlib
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

#: Setups a calibration record can belong to (Samba_mini's two stages).
CAL_SETUPS = ("longitudinal", "polar")
CAL_MODES = ("real", "simulation")
CAL_ORIGINS = ("manual", "fit", "migrated")
CAL_N_VALUES = 6

SCHEMA_VERSION = 2


def _migrate_bd_prefs_to_records(db: sqlite3.Connection) -> None:
    """Schema 2: turn the old per-user preference ``bd_calibration_<setup>`` (six mV values plus a
    ``bd_calibration_date_<setup>`` string) into calibration records, active for that user and setup
    as before.  Only what was stored is carried over: the creator is the account that held the
    preference and the date is the saved date; sample, acquisition mode, source file and fit are
    recorded as unknown (NULL), never guessed.  The preferences themselves are left in place."""
    users = {r[0]: r[1] for r in db.execute("SELECT id, username FROM users")}
    rows = db.execute("SELECT user_id, key, value FROM prefs WHERE key LIKE 'bd_calibration_%'").fetchall()
    prefs: dict[tuple[int, str], object] = {}
    for uid, key, value in rows:
        try:
            prefs[(uid, key)] = json.loads(value)
        except (TypeError, ValueError):
            prefs[(uid, key)] = None
    now = time.time()
    for (uid, key), vals in sorted(prefs.items()):
        setup = key[len("bd_calibration_"):]
        if setup not in CAL_SETUPS:
            continue                                  # bd_calibration_date_* and anything unknown
        try:
            values = [float(v) for v in vals]         # type: ignore[union-attr]
        except (TypeError, ValueError):
            log.warning("migration: preference %s of user %s is not a calibration; left as is", key, uid)
            continue
        if len(values) != CAL_N_VALUES or not all(v == v for v in values) or not any(values):
            continue                                  # all zeros meant "no calibration"
        created = None
        date = prefs.get((uid, f"bd_calibration_date_{setup}"))
        if isinstance(date, str) and date.strip():
            try:
                created = time.mktime(time.strptime(date.strip(), "%Y-%m-%d %H:%M"))
            except ValueError:
                created = None
        rec_id = _new_cal_id()
        db.execute(
            "INSERT INTO calibrations (uid, series, version, user_id, creator, created, recorded, setup, "
            "sample_id, name, values_json, unit, mode, service_mode, source_file, fit_json, origin, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec_id, rec_id, 1, uid, users.get(uid, ""), created, now, setup, None,
             f"Saved calibration ({setup})", json.dumps(values), "mV", None, None, None, None, "migrated",
             f"Migrated from the preference '{key}'"
             + (f" saved {date}" if isinstance(date, str) and date else "")
             + "; sample, acquisition mode and source file were not recorded (unknown)."))
        db.execute("INSERT OR REPLACE INTO calibration_active (user_id, setup, uid, sample_id, activated) "
                   "VALUES (?,?,?,?,?)", (uid, setup, rec_id, None, now))
        log.info("migration: %s calibration of user %s -> record %s", setup, users.get(uid, uid), rec_id)


def _new_cal_id() -> str:
    return "cal-" + secrets.token_hex(6)


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
    2: [
        # One row per immutable calibration version.  ``series`` groups the versions of one
        # calibration; ``created`` is when it was made (NULL = unknown, migrated records),
        # ``recorded`` when this row was written.  NULL sample/mode/source = unknown.
        """CREATE TABLE calibrations (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               uid TEXT NOT NULL UNIQUE,
               series TEXT NOT NULL,
               version INTEGER NOT NULL,
               user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               creator TEXT NOT NULL,
               created REAL,
               recorded REAL NOT NULL,
               setup TEXT NOT NULL,
               sample_id TEXT,
               name TEXT NOT NULL DEFAULT '',
               values_json TEXT NOT NULL,
               unit TEXT NOT NULL DEFAULT 'mV',
               mode TEXT,
               service_mode TEXT,
               source_file TEXT,
               fit_json TEXT,
               origin TEXT NOT NULL,
               note TEXT NOT NULL DEFAULT '',
               UNIQUE (series, version)
           )""",
        "CREATE INDEX calibrations_owner ON calibrations (user_id, setup, recorded)",
        # The calibration a user has activated for the experiment on a setup.
        """CREATE TABLE calibration_active (
               user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               setup TEXT NOT NULL,
               uid TEXT NOT NULL REFERENCES calibrations(uid) ON DELETE CASCADE,
               sample_id TEXT,
               activated REAL NOT NULL,
               PRIMARY KEY (user_id, setup)
           )""",
        _migrate_bd_prefs_to_records,
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
                        if callable(stmt):
                            stmt(self._db)
                        else:
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

    @staticmethod
    def _check_preset_key(workflow, name) -> None:
        if not workflow or not isinstance(workflow, str) or len(workflow) > 64:
            raise ServiceError(ErrorCode.INVALID_VALUE, "bad workflow name")
        if not name or not isinstance(name, str) or len(name) > 80 or name != name.strip():
            raise ServiceError(ErrorCode.INVALID_VALUE, "preset name must be 1-80 characters "
                               "without leading or trailing spaces")

    def get_preset(self, user_id: int, workflow: str, name: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT workflow, name, value, updated FROM presets WHERE user_id=? AND "
                                   "workflow=? AND name=?", (user_id, workflow, name)).fetchone()
        if row is None:
            return None
        return {"workflow": row["workflow"], "name": row["name"], "value": json.loads(row["value"]),
                "updated": row["updated"]}

    def save_preset(self, user_id: int, workflow: str, name: str, value: dict, mode: str = "overwrite") -> dict:
        """Save a preset.  ``mode``: "overwrite" (create or replace, the original behaviour),
        "create" (refuse when the name exists) or "update" (refuse when it does not)."""
        self._check_preset_key(workflow, name)
        if not isinstance(value, dict):
            raise ServiceError(ErrorCode.INVALID_VALUE, "preset value must be an object")
        if mode not in ("overwrite", "create", "update"):
            raise ServiceError(ErrorCode.BAD_REQUEST, f"unknown save mode {mode!r}")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                exists = self._db.execute("SELECT 1 FROM presets WHERE user_id=? AND workflow=? AND name=?",
                                          (user_id, workflow, name)).fetchone() is not None
                if mode == "create" and exists:
                    raise ServiceError(ErrorCode.CONFLICT, f"a configuration named {name!r} already exists",
                                       {"name": name})
                if mode == "update" and not exists:
                    raise ServiceError(ErrorCode.CONFLICT, f"configuration {name!r} no longer exists",
                                       {"name": name})
                self._db.execute(
                    "INSERT INTO presets (user_id, workflow, name, value, updated) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(user_id, workflow, name) DO UPDATE SET value=excluded.value, "
                    "updated=excluded.updated", (user_id, workflow, name, json.dumps(value), time.time()))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get_preset(user_id, workflow, name)

    def rename_preset(self, user_id: int, workflow: str, old: str, new: str) -> dict:
        """Rename in one transaction; refuses (CONFLICT) when *new* exists, so it never overwrites."""
        self._check_preset_key(workflow, old)
        self._check_preset_key(workflow, new)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if self._db.execute("SELECT 1 FROM presets WHERE user_id=? AND workflow=? AND name=?",
                                    (user_id, workflow, old)).fetchone() is None:
                    raise ServiceError(ErrorCode.CONFLICT, f"configuration {old!r} no longer exists",
                                       {"name": old})
                if old != new and self._db.execute(
                        "SELECT 1 FROM presets WHERE user_id=? AND workflow=? AND name=?",
                        (user_id, workflow, new)).fetchone() is not None:
                    raise ServiceError(ErrorCode.CONFLICT, f"a configuration named {new!r} already exists",
                                       {"name": new})
                self._db.execute("UPDATE presets SET name=?, updated=? WHERE user_id=? AND workflow=? AND name=?",
                                 (new, time.time(), user_id, workflow, old))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get_preset(user_id, workflow, new)

    def copy_preset(self, user_id: int, workflow: str, src: str, dst: str) -> dict:
        """Copy the *stored* preset *src* to the new name *dst* (CONFLICT when *dst* exists)."""
        self._check_preset_key(workflow, src)
        self._check_preset_key(workflow, dst)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT value FROM presets WHERE user_id=? AND workflow=? AND name=?",
                                       (user_id, workflow, src)).fetchone()
                if row is None:
                    raise ServiceError(ErrorCode.CONFLICT, f"configuration {src!r} no longer exists",
                                       {"name": src})
                if self._db.execute("SELECT 1 FROM presets WHERE user_id=? AND workflow=? AND name=?",
                                    (user_id, workflow, dst)).fetchone() is not None:
                    raise ServiceError(ErrorCode.CONFLICT, f"a configuration named {dst!r} already exists",
                                       {"name": dst})
                self._db.execute("INSERT INTO presets (user_id, workflow, name, value, updated) VALUES (?,?,?,?,?)",
                                 (user_id, workflow, dst, row["value"], time.time()))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get_preset(user_id, workflow, dst)

    def delete_preset(self, user_id: int, workflow: str, name: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM presets WHERE user_id=? AND workflow=? AND name=?",
                                   (user_id, workflow, name))
            return cur.rowcount > 0

    # ------------------------------------------------------- calibrations --
    def save_calibration(self, user: dict, rec: dict, base: str | None = None,
                         service_mode: str | None = None) -> dict:
        """Store a new immutable calibration version.  With *base* (an existing record id of
        this user) it becomes the next version of that calibration, otherwise version 1 of a
        new one.  Creator and timestamps are set here, never taken from the client."""
        if not isinstance(rec, dict):
            raise ServiceError(ErrorCode.INVALID_VALUE, "calibration must be an object")
        setup = rec.get("setup")
        if setup not in CAL_SETUPS:
            raise ServiceError(ErrorCode.INVALID_VALUE, f"setup must be one of {CAL_SETUPS}")
        vals = rec.get("values")
        try:
            values = [float(v) for v in vals]
        except (TypeError, ValueError):
            raise ServiceError(ErrorCode.INVALID_VALUE, "values must be six numbers") from None
        if len(values) != CAL_N_VALUES or not all(v == v and abs(v) != float("inf") for v in values):
            raise ServiceError(ErrorCode.INVALID_VALUE, "values must be six finite numbers")
        unit = rec.get("unit", "mV")
        if unit != "mV":
            raise ServiceError(ErrorCode.INVALID_VALUE, "calibration values are stored in mV")
        mode = rec.get("mode")
        if mode is not None and mode not in CAL_MODES:
            raise ServiceError(ErrorCode.INVALID_VALUE, f"mode must be one of {CAL_MODES} or null (unknown)")
        origin = rec.get("origin", "manual")
        if origin not in ("manual", "fit"):
            raise ServiceError(ErrorCode.INVALID_VALUE, "origin must be 'manual' or 'fit'")
        sample = rec.get("sample_id")
        if sample is not None and (not isinstance(sample, str) or len(sample) > 120):
            raise ServiceError(ErrorCode.INVALID_VALUE, "sample_id must be text (or null = unknown)")
        sample = (sample or "").strip() or None
        name = str(rec.get("name") or "").strip()[:80]
        note = str(rec.get("note") or "")[:1000]
        source = rec.get("source_file")
        source = str(source)[:1000] if source else None
        fit = rec.get("fit")
        if fit is not None and not isinstance(fit, dict):
            raise ServiceError(ErrorCode.INVALID_VALUE, "fit must be an object")
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if base:
                    row = self._db.execute("SELECT series, setup FROM calibrations WHERE uid=? AND user_id=?",
                                           (base, user["id"])).fetchone()
                    if row is None:
                        raise ServiceError(ErrorCode.CONFLICT, f"calibration {base!r} not found", {"id": base})
                    if row["setup"] != setup:
                        raise ServiceError(ErrorCode.INVALID_VALUE, "a new version must keep the setup "
                                           f"({row['setup']}); use Save as new for another setup")
                    series = row["series"]
                    version = int(self._db.execute("SELECT MAX(version) FROM calibrations WHERE series=?",
                                                   (series,)).fetchone()[0]) + 1
                else:
                    series, version = None, 1
                uid = _new_cal_id()
                series = series or uid
                self._db.execute(
                    "INSERT INTO calibrations (uid, series, version, user_id, creator, created, recorded, setup, "
                    "sample_id, name, values_json, unit, mode, service_mode, source_file, fit_json, origin, note) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, series, version, user["id"], user["username"], now, now, setup, sample, name,
                     json.dumps(values), unit, mode, service_mode, source,
                     json.dumps(fit, default=str) if fit is not None else None, origin, note))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get_calibration(user["id"], uid)

    def get_calibration(self, user_id: int, uid: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM calibrations WHERE uid=? AND user_id=?", (uid, user_id)).fetchone()
        if row is None:
            raise ServiceError(ErrorCode.CONFLICT, f"calibration {uid!r} not found", {"id": uid})
        return _public_calibration(row)

    def list_calibrations(self, user_id: int, setup: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT * FROM calibrations WHERE user_id=?"
        args: list = [user_id]
        if setup:
            q += " AND setup=?"
            args.append(setup)
        q += " ORDER BY recorded DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [_public_calibration(r) for r in rows]

    def active_calibration(self, user_id: int, setup: str) -> dict:
        if setup not in CAL_SETUPS:
            raise ServiceError(ErrorCode.INVALID_VALUE, f"setup must be one of {CAL_SETUPS}")
        with self._lock:
            row = self._db.execute("SELECT uid, sample_id, activated FROM calibration_active WHERE user_id=? "
                                   "AND setup=?", (user_id, setup)).fetchone()
        if row is None:
            return {"setup": setup, "record": None, "sample_id": None, "activated": None}
        return {"setup": setup, "record": self.get_calibration(user_id, row["uid"]),
                "sample_id": row["sample_id"], "activated": row["activated"]}

    def activate_calibration(self, user_id: int, setup: str, uid: str | None, sample_id: str | None) -> dict:
        """Make *uid* the active calibration of this user's experiment on *setup* (None = none).
        ``sample_id`` is the experiment's sample it was activated for."""
        if setup not in CAL_SETUPS:
            raise ServiceError(ErrorCode.INVALID_VALUE, f"setup must be one of {CAL_SETUPS}")
        with self._lock:
            if uid is None:
                self._db.execute("DELETE FROM calibration_active WHERE user_id=? AND setup=?", (user_id, setup))
            else:
                rec = self.get_calibration(user_id, uid)
                if rec["setup"] != setup:
                    raise ServiceError(ErrorCode.INVALID_VALUE, f"calibration {uid} belongs to the "
                                       f"{rec['setup']} setup, not {setup}")
                sample = (sample_id or "").strip() or None if isinstance(sample_id, (str, type(None))) else None
                self._db.execute("INSERT OR REPLACE INTO calibration_active (user_id, setup, uid, sample_id, "
                                 "activated) VALUES (?,?,?,?,?)", (user_id, setup, uid, sample, time.time()))
        return self.active_calibration(user_id, setup)


def _public_user(row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "full_name": row["full_name"], "disabled": bool(row["disabled"]),
            "created": row["created"], "last_login": row["last_login"]}


def _public_calibration(row) -> dict:
    """A calibration record as sent to clients and written into measurement files.
    ``None`` means unknown (e.g. the sample of a migrated record)."""
    return {"id": row["uid"], "series": row["series"], "version": row["version"], "name": row["name"],
            "creator": row["creator"], "created": row["created"], "recorded": row["recorded"],
            "setup": row["setup"], "sample_id": row["sample_id"], "values": json.loads(row["values_json"]),
            "unit": row["unit"], "mode": row["mode"], "service_mode": row["service_mode"],
            "source_file": row["source_file"],
            "fit": json.loads(row["fit_json"]) if row["fit_json"] else None,
            "origin": row["origin"], "note": row["note"]}


# ---------------------------------------------------------------------------
# Sessions (kept in a file next to the service state: a service restart keeps everybody logged in)
# ---------------------------------------------------------------------------

SAVE_LAST_SEEN_EVERY_S = 60.0   # how stale a session's saved "last seen" may get (idle timeout: hours)


@dataclass
class Session:
    token: str
    user: dict
    client: str
    created: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    saved_seen: float = field(default_factory=time.time)     # wall clock of the saved "last seen"

    @property
    def user_id(self) -> int:
        return self.user["id"]

    @property
    def is_admin(self) -> bool:
        return self.user.get("role") == "admin"

    def public(self) -> dict:
        return {"user": self.user["username"], "role": self.user["role"], "client": self.client,
                "session_id": self.token[:8]}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionManager:
    """Login sessions.  With a *path* they survive a service restart: the file keeps a hash of
    each token (never the token), its user, client and when it was last used, and a client that
    kept its token goes on after the restart without logging in again (17 Nov: stopping the
    service to recover a blocked device logged everybody out).  Logout, the idle timeout and
    disabling the account end a session as before; the user record is read again on restore."""

    def __init__(self, idle_timeout_s: float = 12 * 3600, path: Path | None = None,
                 get_user=None):
        self.idle_timeout_s = idle_timeout_s
        self._sessions: dict[str, Session] = {}
        self._saved: dict[str, dict] = {}            # token hash -> user_id, client, created, last_seen
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        self._get_user = get_user
        if self._path is not None:
            self._load()

    # -------------------------------------------------------------- file --
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            saved = data.get("sessions", {}) if isinstance(data, dict) else {}
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("sessions file %s not readable (%s); everybody logs in again", self._path, exc)
            return
        now = time.time()
        self._saved = {h: v for h, v in saved.items()
                       if isinstance(v, dict) and now - float(v.get("last_seen", 0)) <= self.idle_timeout_s}
        if len(self._saved) != len(saved):
            self._write()

    def _write(self) -> None:
        """Write the saved sessions (with the lock held, or before anybody can use them)."""
        if self._path is None:
            return
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"version": 1, "sessions": self._saved}), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("sessions not saved to %s (%s): a service restart will need a new login",
                        self._path, exc)

    def _remember(self, s: Session) -> None:
        if self._path is None:
            return
        h = _token_hash(s.token)
        s.saved_seen = time.time()
        created = self._saved.get(h, {}).get("created", s.saved_seen)
        self._saved[h] = {"user_id": s.user_id, "client": s.client, "created": created,
                          "last_seen": s.saved_seen}
        self._write()

    def _forget(self, tokens: list[str]) -> None:
        if self._path is None:
            return
        changed = False
        for t in tokens:
            changed |= self._saved.pop(_token_hash(t), None) is not None
        if changed:
            self._write()

    def _restore(self, token: str) -> Session | None:
        """A session saved by an earlier run of the service (lock held)."""
        saved = self._saved.get(_token_hash(token))
        if saved is None or self._get_user is None:
            return None
        if time.time() - float(saved.get("last_seen", 0)) > self.idle_timeout_s:
            self._forget([token])
            return None
        try:
            user = self._get_user(int(saved["user_id"]))
        except (ServiceError, KeyError, ValueError, TypeError):
            user = None
        if not user or user.get("disabled"):
            self._forget([token])
            return None
        s = Session(token=token, user=user, client=str(saved.get("client", "client")))
        self._sessions[token] = s
        log.info("session of %s (%s) continues after a service restart", user["username"], s.client)
        return s

    # --------------------------------------------------------------- API --
    def create(self, user: dict, client: str) -> Session:
        token = secrets.token_urlsafe(32)
        s = Session(token=token, user=user, client=str(client or "client")[:40])
        with self._lock:
            self._sessions[token] = s
            self._remember(s)
        return s

    def get(self, token: str | None) -> Session:
        if not token:
            raise ServiceError(ErrorCode.AUTH_REQUIRED, "log in first")
        with self._lock:
            s = self._sessions.get(token) or self._restore(token)
            if s is None:
                raise ServiceError(ErrorCode.AUTH_REQUIRED, "session expired or unknown; log in again")
            now = time.monotonic()
            if now - s.last_seen > self.idle_timeout_s:
                del self._sessions[token]
                self._forget([token])
                raise ServiceError(ErrorCode.AUTH_REQUIRED, "session timed out; log in again")
            s.last_seen = now
            if time.time() - s.saved_seen > SAVE_LAST_SEEN_EVERY_S:
                self._remember(s)
            return s

    def drop(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)
            self._forget([token])

    def drop_user(self, user_id: int) -> None:
        with self._lock:
            tokens = [t for t, s in self._sessions.items() if s.user_id == user_id]
            for t in tokens:
                del self._sessions[t]
            self._forget(tokens)
            stale = [h for h, v in self._saved.items() if v.get("user_id") == user_id]
            for h in stale:
                del self._saved[h]
            if stale:
                self._write()

    def update_user(self, user: dict) -> None:
        with self._lock:
            for s in self._sessions.values():
                if s.user_id == user["id"]:
                    s.user = user

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(s.public(), idle_s=round(time.monotonic() - s.last_seen, 1))
                    for s in self._sessions.values()]
