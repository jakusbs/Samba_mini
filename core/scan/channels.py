"""Channels: what the instruments measure (sensors) and what they set (outputs).

Every channel has a fixed **id** (``dc_signal``, ``lockin_x``, ``coil`` ...) that the
software uses, and a **name** the lab gives it in Samba_controls (machine.toml,
``[<device>.channels]``): e.g. "BD" for the balanced diodes, "Focus line" for the
intensity diode.  As in Samba, a measured channel is saved under its name
(``/data/<name>``, see ``dataset_key``) with the id, device, unit and physical line
as attributes, so renaming never confuses the BD fit, the θ conversion or the Data
Browser, which find channels by id.

Sensors are read-only and are what a measurement records (only the ticked ones).
Outputs are read-write; measurements set them (the coil command is the field
sweep's own axis) and they are recorded as set-points, not as sensors.

Shared by the service (channel list), the engine (HDF5 layout), the live plots and
the CSV export.  Qt-free.
"""

from __future__ import annotations


from dataclasses import dataclass


@dataclass(frozen=True)
class Channel:
    name: str          # fixed channel id (the dataset name of files written without a channel selection)
    key: str           # key in the backend point record
    label: str         # default display name
    unit: str
    role: str = "sensor"
    dtype: str = "f8"
    lockin: bool = False
    device: str = ""   # device id that measures it
    wiring: str = ""   # config key of the device section holding the physical line (DAQ channels)
    access: str = "r"  # "r" = sensor, "rw" = output

    @property
    def id(self) -> str:
        return self.name


SENSORS = [
    Channel("dc_signal", "balanced_V", "BD", "V", device="daq", wiring="ai_balanced"),
    Channel("intensity", "intensity_V", "Intensity", "V", device="daq", wiring="ai_intensity"),
    Channel("hall_field", "hall_field_mT", "Hall field", "mT", device="hall"),
    Channel("lockin_x", "lockin_x_V", "Lock-in X", "V", lockin=True, device="lockin"),
    Channel("lockin_y", "lockin_y_V", "Lock-in Y", "V", lockin=True, device="lockin"),
    Channel("lockin_r", "lockin_r_V", "Lock-in R", "V", lockin=True, device="lockin"),
    Channel("lockin_theta", "lockin_theta_deg", "Lock-in θ", "deg", lockin=True, device="lockin"),
]

#: Read-write channels (set by measurements / the Hardware box; recorded as set-points).
OUTPUTS = [
    Channel("coil", "coil_command_V", "Magnet", "V", role="output", device="daq", wiring="ao_coil", access="rw"),
    Channel("ao1", "ao1_V", "AO1", "V", role="output", device="daq", wiring="ao_mod1", access="rw"),
    Channel("ao2", "ao2_V", "AO2", "V", role="output", device="daq", wiring="ao_mod2", access="rw"),
]

BOOKKEEPING = [
    Channel("acq_id", "acq_id", "Acquisition id", "", "index", "i8"),
    Channel("t_trigger", "t_trigger", "Trigger time (Unix)", "s", "timestamp"),
    Channel("hall_integration", "hall_integration_s", "Hall integration time", "s", "readback"),
]

#: Channels whose mean over the sweeps of a B-sweep is saved in /data/averaged.
AVERAGED = ("dc_signal", "intensity", "hall_field", "lockin_x", "lockin_r")

#: Dataset names the engine uses itself; a channel name may not turn into one of these.
RESERVED = {"time", "acq_id", "t_trigger", "hall_integration", "calibration", "cycle", "branch", "loop",
            "iteration", "n_sweeps", "coil_setpoint", "averaged", "sensor"}
RESERVED_PREFIXES = ("actuator_", "position_", "repeat_")
NAME_MAX = 40

ALL = SENSORS + OUTPUTS
BY_ID = {c.name: c for c in ALL}
DEVICE_CHANNELS = {dev: [c.name for c in ALL if c.device == dev] for dev in ("daq", "lockin", "hall")}


def sensors_for(uses_lockin: bool) -> list[Channel]:
    return [c for c in SENSORS if uses_lockin or not c.lockin]


def by_name(name: str) -> Channel | None:
    for c in SENSORS + OUTPUTS + BOOKKEEPING:
        if c.name == name:
            return c
    return None


def dataset_key(name: str) -> str:
    """HDF5 dataset name for a channel name -- exactly Samba's ``ScanRunner._hdf5_key``:
    stripped, with '/', ' ' and '\\' replaced by '_' ("sensor" when empty)."""
    key = str(name or "").strip().replace("/", "_").replace(" ", "_").replace("\\", "_")
    return key if key not in ("", ".", "..") else "sensor"


def default_names() -> dict[str, dict[str, str]]:
    """``{device: {channel id: default name}}`` for the machine-configuration defaults."""
    out: dict[str, dict[str, str]] = {}
    for c in ALL:
        out.setdefault(c.device, {})[c.name] = c.label
    return out


def channel_table(cfg: dict) -> list[dict]:
    """Every channel with the name, physical line and device name of this machine configuration."""
    rows = []
    for c in ALL:
        sec = cfg.get(c.device) or {}
        names = sec.get("channels") or {}
        name = str(names.get(c.name) or c.label)
        rows.append({"id": c.name, "device": c.device, "device_label": str(sec.get("label") or c.device),
                     "name": name, "dataset": dataset_key(name), "unit": c.unit, "access": c.access,
                     "kind": "sensor" if c.access == "r" else "output", "lockin": c.lockin,
                     "line": str(sec.get(c.wiring, "")) if c.wiring else "", "wiring": c.wiring,
                     "record_key": c.key, "enabled": bool(sec.get("enabled", True))})
    return rows


def validate_names(cfg: dict) -> list[str]:
    """Problems with the channel names of a machine configuration (empty = fine)."""
    problems = []
    seen: dict[str, str] = {}
    for dev, ids in DEVICE_CHANNELS.items():
        names = (cfg.get(dev) or {}).get("channels")
        if names is None:
            continue
        if not isinstance(names, dict):
            problems.append(f"{dev}.channels must be a table of channel id = name")
            continue
        for cid in names:
            if cid not in ids:
                problems.append(f"{dev}.channels.{cid}: no such channel on {dev} ({', '.join(ids)})")
    for row in channel_table(cfg):
        name, key = row["name"], row["dataset"]
        where = f"{row['device']}.channels.{row['id']}"
        if not name.strip() or len(name) > NAME_MAX:
            problems.append(f"{where}: the name must be 1-{NAME_MAX} characters")
            continue
        if key.lower() in RESERVED or key.lower().startswith(RESERVED_PREFIXES):
            problems.append(f"{where}: {name!r} is used by the measurement files itself; choose another name")
        other = seen.get(key.lower())
        if other:
            problems.append(f"{where}: {name!r} is saved as {key!r}, like {other}; names must differ")
        seen[key.lower()] = where
    return problems
