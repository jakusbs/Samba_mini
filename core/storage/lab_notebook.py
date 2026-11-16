"""Lab-notebook CSV: one row per run (adapted from MiniMoke ``data_archive.py``).

Rows are aligned to the header already in the file, so the column set can
evolve without shifting old rows (MiniMoke behaviour).
"""

from __future__ import annotations

import csv
import os
import time

PARAM_TO_COLUMN = {
    "b": "Field (V coil cmd)", "b_min": "Field start (V coil cmd)", "b_max": "Field stop (V coil cmd)",
    "b_step": "Field step (V coil cmd)", "sweep_freq": "Sweep frequency (Hz)", "num_sweeps": "Num sweeps",
    "x": "X (um)", "x_min": "X start (um)", "x_max": "X stop (um)", "x_step": "X step (um)",
    "y": "Y (um)", "y_min": "Y start (um)", "y_max": "Y stop (um)", "y_step": "Y step (um)",
    "repeat_num": "Repeat number", "volt": "Lockin voltage (V)", "sensi": "Lockin sensitivity (V)",
    "lockin_freq": "Lockin frequency (Hz)", "time_const": "Lockin time constant (s)",
    "phase": "Lockin phase (deg)", "acq_time": "Acquisition time (s)", "duration": "Duration (s)",
    "interval": "Sampling interval (s)",
}

COLUMNS = [
    "Date", "Time", "Scan type", "Sample ID", "Operator", "User", "Mode", "Status",
    "Setup", "Geometry", "Stage type",
    *PARAM_TO_COLUMN.values(),
    "Total points", "Points acquired", "Measurement duration (s)", "File path",
]


def append_row(path: str, row: dict) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    is_new = not os.path.exists(path) or os.path.getsize(path) == 0
    fieldnames = COLUMNS
    if not is_new:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), None)
            if header:
                fieldnames = header
        except OSError:
            pass
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow(row)


def append_run(path: str, engine, status: str) -> None:
    now = time.localtime()
    req = engine.req
    row = {
        "Date": time.strftime("%Y-%m-%d", now), "Time": time.strftime("%H:%M:%S", now),
        "Scan type": engine.wf.title, "Sample ID": req.sample, "Operator": req.operator,
        "User": (engine.client.user or {}).get("username", ""), "Mode": engine.mode, "Status": status,
        "Setup": req.setup, "Geometry": "PMOKE" if req.setup == "polar" else req.geometry,
        "Stage type": "Thorlabs" if req.setup == "longitudinal" else "Trinamic",
        "Total points": engine.points_total, "Points acquired": engine.points_done,
        "Measurement duration (s)": round(time.time() - engine._t_start, 1),
        "File path": os.path.normpath(str(engine.path)),
    }
    for name, col in PARAM_TO_COLUMN.items():
        if name in engine.params:
            row[col] = engine.params[name]
    append_row(path, row)
