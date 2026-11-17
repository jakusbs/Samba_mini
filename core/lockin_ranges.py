"""Ametek / Signal Recovery 7270 settings tables, shared by the service (which sets the lock-in)
and Samba_mini (whose Hardware box offers the sensitivity ranges in a list)."""

from __future__ import annotations

# From MiniMoke's Ametek7270 driver (derived from PyMeasure, MIT licence).  Voltage mode, in V;
# the list index is the 7270's SEN code (0 is unused).
SENSITIVITIES = [
    0.0, 2.0e-9, 5.0e-9, 10.0e-9, 20.0e-9, 50.0e-9, 100.0e-9,
    200.0e-9, 500.0e-9, 1.0e-6, 2.0e-6, 5.0e-6, 10.0e-6,
    20.0e-6, 50.0e-6, 100.0e-6, 200.0e-6, 500.0e-6, 1.0e-3,
    2.0e-3, 5.0e-3, 10.0e-3, 20.0e-3, 50.0e-3, 100.0e-3,
    200.0e-3, 500.0e-3, 1.0,
]
TIME_CONSTANTS = [
    10.0e-6, 20.0e-6, 50.0e-6, 100.0e-6, 200.0e-6, 500.0e-6,
    1.0e-3, 2.0e-3, 5.0e-3, 10.0e-3, 20.0e-3, 50.0e-3, 100.0e-3,
    200.0e-3, 500.0e-3, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0,
    100.0, 200.0, 500.0, 1.0e3, 2.0e3, 5.0e3, 10.0e3,
    20.0e3, 50.0e3, 100.0e3,
]


def truncated_discrete(value: float, values: list[float]) -> float:
    """PyMeasure ``truncated_discrete_set``: the smallest allowed value >= *value*
    (the largest one if *value* is above all)."""
    for v in sorted(values):
        if value <= v:
            return v
    return max(values)


def sensitivity_ranges() -> list[float]:
    """The ranges an operator can pick (SEN 1 … 27), smallest first, in V."""
    return [v for v in SENSITIVITIES if v > 0]


def mv_text(volts: float) -> str:
    """A sensitivity in mV without trailing zeros: 500e-6 -> '0.5 mV', 1.0 -> '1000 mV'."""
    text = f"{volts * 1e3:.6f}".rstrip("0").rstrip(".")
    return f"{text} mV"
