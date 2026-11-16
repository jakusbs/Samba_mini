"""Windows-appropriate locations for configuration, state, accounts and logs.

Everything machine-specific lives under one per-machine root that is *outside*
the source tree, so a ``git pull`` can never overwrite rig settings:

    %LOCALAPPDATA%\\SambaMini\\
        config\\machine.toml      machine wiring, addresses, limits, calibration
        config\\backups\\         automatic copies made before every migration/save
        state\\                   runtime state (stage offsets, backend lock file)
        data\\accounts.sqlite3    local accounts, preferences and presets
        logs\\                    rotating service / application logs

``SAMBA_MINI_HOME`` overrides the root (used by the tests and for portable
installs).  Measurement data go to the per-user preferred data folder, not here.
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("SAMBA_MINI_HOME")
    if override:
        root = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        root = Path(base) / "SambaMini"
    return root


def config_dir() -> Path:
    return _ensure(home() / "config")


def backups_dir() -> Path:
    return _ensure(home() / "config" / "backups")


def state_dir() -> Path:
    return _ensure(home() / "state")


def data_dir() -> Path:
    return _ensure(home() / "data")


def logs_dir() -> Path:
    return _ensure(home() / "logs")


def machine_config_path() -> Path:
    return config_dir() / "machine.toml"


def accounts_db_path() -> Path:
    return data_dir() / "accounts.sqlite3"


def default_measurement_dir() -> Path:
    """Fallback data folder when a user has not chosen one."""
    docs = Path(os.path.expanduser("~")) / "Documents"
    return docs / "SambaMini Data"


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
