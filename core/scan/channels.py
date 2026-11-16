"""Recorded channels: dataset name, role, label, unit and the record key they
come from.  Shared by the HDF5 layout, live plots and CSV export."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Channel:
    name: str          # dataset name
    key: str           # key in the backend point record
    label: str
    unit: str
    role: str = "sensor"
    dtype: str = "f8"
    lockin: bool = False


SENSORS = [
    Channel("dc_signal", "balanced_V", "Balanced diodes DC", "V"),
    Channel("dc_std", "balanced_std_V", "Balanced diodes std", "V"),
    Channel("intensity", "intensity_V", "Intensity", "V"),
    Channel("intensity_std", "intensity_std_V", "Intensity std", "V"),
    Channel("hall_field", "hall_field_mT", "Hall field", "mT"),
    Channel("lockin_x", "lockin_x_V", "Lock-in X", "V", lockin=True),
    Channel("lockin_y", "lockin_y_V", "Lock-in Y", "V", lockin=True),
    Channel("lockin_r", "lockin_r_V", "Lock-in R", "V", lockin=True),
    Channel("lockin_theta", "lockin_theta_deg", "Lock-in θ", "deg", lockin=True),
]

BOOKKEEPING = [
    Channel("acq_id", "acq_id", "Acquisition id", "", "index", "i8"),
    Channel("t_trigger", "t_trigger", "Trigger time (Unix)", "s", "timestamp"),
    Channel("hall_integration", "hall_integration_s", "Hall integration time", "s", "readback"),
]


def sensors_for(uses_lockin: bool) -> list[Channel]:
    return [c for c in SENSORS if uses_lockin or not c.lockin]


def by_name(name: str) -> Channel | None:
    for c in SENSORS + BOOKKEEPING:
        if c.name == name:
            return c
    return None
