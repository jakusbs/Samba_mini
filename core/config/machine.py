"""Machine-wide configuration: wiring, instrument addresses, limits, calibration.

Stored as TOML at ``%LOCALAPPDATA%\\SambaMini\\config\\machine.toml`` (see
``config/examples/machine.example.toml``).  This is *not* per-user data: it is
edited by an administrator and shared by everyone using the PC.

* ``schema_version`` is checked; older files are migrated step by step after a
  timestamped copy is written to ``config\\backups``.
* A file that cannot be parsed is never overwritten: it is copied to
  ``machine.toml.bad-<timestamp>`` for diagnosis and the service starts in a
  safe state (``mode = "simulation"`` is NOT assumed -- the load error is
  reported and real devices stay disabled).
* Saves are atomic (temporary file + ``os.replace``).
"""

from __future__ import annotations

import copy
import logging
import os
import shutil
import time
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from core import paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "backend": {
        "mode": "simulation",          # "real" or "simulation"; never mixed
        "rpc_port": 47811,
        "event_port": 47812,
        "lease_ttl_s": 15.0,
        "session_idle_timeout_s": 43200.0,
        "reconcile_wait_s": 60.0,
    },
    "daq": {
        "enabled": True,
        "device": "Dev1",
        "ao_mod1": "Dev1/ao1",
        "ao_mod2": "Dev1/ao2",
        "ao_coil": "Dev1/ao3",
        "ai_balanced": "Dev1/ai0",
        "ai_intensity": "Dev1/ai1",
        "trigger_terminal": "/Dev1/PFI0",
        "sampling_rate_Hz": 50000.0,
    },
    "coil": {
        "min_V": -6.0,                 # MiniMoke parameter limits (labelled 'A' there)
        "max_V": 6.0,
        # "amps_per_volt": unset -> no coil current is derived
    },
    "field_calibration": {
        # "mT_per_V": unset -> no field estimate; Hall readings only
    },
    "lockin": {
        "enabled": True,
        "model": "ametek7270",
        "resource": "USB0::0x0A2D::0x001B::15342534::RAW",
        "timeout_ms": 2000,
        "reconnect_attempts": 6,
        "allow_usb_reset": False,
        "max_oscillator_V": 5.0,
    },
    "hall": {
        "enabled": True,
        "dll_path": "",
        "speed": 1,
        "default_integration_s": 0.5,
    },
    "stages": {
        "longitudinal": {
            "enabled": True,
            "driver": "kinesis",
            "serial_x": "",
            "serial_y": "",
            "serial_z": "",
            "mm2steps": 2184533.0,
            "backlash_mm": 0.02,
            "move_timeout_s": 120.0,
        },
        "polar": {
            "enabled": True,
            "driver": "trinamic",
            "port": "auto",
            "module_id": 1,
            "mm2steps_x": 51200.0,
            "mm2steps_y": 51200.0,
            "mm2steps_z": 51200.0,
            "move_timeout_s": 120.0,
        },
    },
    "cleanup": {
        "zero_coil": True,
        "lockin_output_off": True,
        "stop_stages": True,
    },
}


class ConfigError(Exception):
    pass


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# Migrations: function(old_dict) -> new_dict for version n -> n+1.
_MIGRATIONS: dict[int, Any] = {}


def migrate(data: dict, path: Path | None = None) -> tuple[dict, bool]:
    version = int(data.get("schema_version", 0) or 0)
    if version > SCHEMA_VERSION:
        raise ConfigError(f"configuration schema {version} is newer than this software ({SCHEMA_VERSION})")
    changed = False
    if version < SCHEMA_VERSION and path is not None and path.exists():
        backup = paths.backups_dir() / f"{path.stem}.v{version}.{time.strftime('%Y%m%d-%H%M%S')}.toml"
        shutil.copy2(path, backup)
        log.info("configuration backed up to %s before migration", backup)
    while version < SCHEMA_VERSION:
        step = _MIGRATIONS.get(version)
        data = step(data) if step else dict(data)
        version += 1
        data["schema_version"] = version
        changed = True
    return data, changed


def validate(cfg: dict) -> list[str]:
    """Return a list of problems (empty if fine)."""
    problems = []
    mode = cfg["backend"].get("mode")
    if mode not in ("real", "simulation"):
        problems.append(f"backend.mode must be 'real' or 'simulation', not {mode!r}")
    for key in ("rpc_port", "event_port"):
        port = cfg["backend"].get(key)
        if not isinstance(port, int) or not (1024 <= port <= 65535):
            problems.append(f"backend.{key} must be an integer port 1024-65535")
    if cfg["backend"]["rpc_port"] == cfg["backend"]["event_port"]:
        problems.append("rpc_port and event_port must differ")
    coil = cfg["coil"]
    try:
        if not float(coil["min_V"]) < float(coil["max_V"]):
            problems.append("coil.min_V must be below coil.max_V")
        if max(abs(float(coil["min_V"])), abs(float(coil["max_V"]))) > 10.0:
            problems.append("coil limits exceed the +/-10 V analogue output range")
    except (TypeError, ValueError, KeyError):
        problems.append("coil.min_V/max_V must be numbers")
    rate = cfg["daq"].get("sampling_rate_Hz")
    if not isinstance(rate, (int, float)) or rate <= 0:
        problems.append("daq.sampling_rate_Hz must be positive")
    for name, st in cfg.get("stages", {}).items():
        lim = st.get("limits_mm")
        if lim is not None:
            if not isinstance(lim, dict):
                problems.append(f"stages.{name}.limits_mm must be a table")
            else:
                for axis, pair in lim.items():
                    if (axis not in ("x", "y", "z") or not isinstance(pair, list) or len(pair) != 2
                            or not pair[0] < pair[1]):
                        problems.append(f"stages.{name}.limits_mm.{axis} must be [min, max]")
    return problems


class MachineConfig:
    """Loaded configuration plus how it was obtained."""

    def __init__(self, data: dict, path: Path | None, status: str, problems: list[str]):
        self.data = data
        self.path = path
        self.status = status            # "loaded", "defaults", "invalid", "migrated"
        self.problems = problems

    @property
    def mode(self) -> str:
        return self.data["backend"]["mode"]

    def __getitem__(self, key):
        return self.data[key]

    def snapshot(self) -> dict:
        return copy.deepcopy(self.data)


def load(path: Path | str | None = None) -> MachineConfig:
    path = Path(path) if path else paths.machine_config_path()
    if not path.exists():
        data = copy.deepcopy(DEFAULTS)
        return MachineConfig(data, path, "defaults",
                             [f"no configuration at {path}; built-in defaults (simulation mode) in use"])
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        bad = path.with_name(f"{path.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            shutil.copy2(path, bad)
        except OSError:
            bad = None
        data = copy.deepcopy(DEFAULTS)
        data["backend"]["mode"] = "real"   # do not silently fall back to simulation
        for dev in ("daq", "lockin", "hall"):
            data[dev]["enabled"] = False
        for st in data["stages"].values():
            st["enabled"] = False
        return MachineConfig(data, path, "invalid",
                             [f"cannot parse {path}: {exc}" + (f" (copy kept as {bad.name})" if bad else ""),
                              "all hardware disabled until the file is fixed"])
    try:
        migrated, changed = migrate(raw, path)
    except ConfigError as exc:
        data = copy.deepcopy(DEFAULTS)
        data["backend"]["mode"] = "real"
        for dev in ("daq", "lockin", "hall"):
            data[dev]["enabled"] = False
        for st in data["stages"].values():
            st["enabled"] = False
        return MachineConfig(data, path, "invalid", [str(exc), "all hardware disabled"])
    data = deep_merge(DEFAULTS, migrated)
    problems = validate(data)
    status = "migrated" if changed else "loaded"
    if changed and not problems:
        save(data, path, backup=False)
    if problems:
        status = "invalid"
        for dev in ("daq", "lockin", "hall"):
            data[dev]["enabled"] = False
        for st in data["stages"].values():
            st["enabled"] = False
        problems.append("all hardware disabled until the configuration is fixed")
    return MachineConfig(data, path, status, problems)


def save(data: dict, path: Path | str | None = None, backup: bool = True) -> Path:
    path = Path(path) if path else paths.machine_config_path()
    problems = validate(deep_merge(DEFAULTS, data))
    if problems:
        raise ConfigError("; ".join(problems))
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, paths.backups_dir() / f"{path.stem}.{time.strftime('%Y%m%d-%H%M%S')}.toml")
    tmp = path.with_suffix(".toml.tmp")
    with open(tmp, "wb") as f:
        tomli_w.dump(_strip_none(data), f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def _strip_none(d):
    if isinstance(d, dict):
        return {k: _strip_none(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_strip_none(v) for v in d]
    return d
