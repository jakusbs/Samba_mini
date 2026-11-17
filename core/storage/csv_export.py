"""CSV export of a Samba_mini HDF5 file in MiniMoke's column order.

Columns keep MiniMoke's names where the physics matches and fix the ones that
were mislabelled (coil command in V instead of "Magnetic Field (A)", Hall field
in mT instead of a "Magnetic Field (T)" column that sometimes held a synthetic
value, θ in degrees).  Comment lines at the top (``#``) carry the metadata.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

from core.storage.hdf5_writer import read_file

# (CSV column, dataset name candidates; "@<id>" = the dataset of that channel, found by its
# ``channel`` attribute -- files save channels under their names, as in Samba -- or by the id
# itself in files written before channels had names)
COLUMNS = [
    ("Time (s)", ["time"]),
    ("Iteration", ["iteration", "index"]),
    ("Loop", ["loop", "cycle"]),
    ("Branch (0 fwd, 1 bwd)", ["branch"]),
    ("X Position (um)", ["actuator_x", "position_x"]),
    ("Y Position (um)", ["actuator_y", "position_y"]),
    ("Coil command (V) [MiniMoke: Magnetic Field (A)]", ["actuator_coil", "coil_setpoint"]),
    ("Hall field (mT)", ["@hall_field"]),
    ("Voltage X 1f (V)", ["@lockin_x"]),
    ("Voltage Y 1f (V)", ["@lockin_y"]),
    ("Voltage R 1f (V)", ["@lockin_r"]),
    ("Phase 1f (deg)", ["@lockin_theta"]),
    ("Voltage DC (V)", ["@dc_signal"]),
    ("Intensity (V)", ["@intensity"]),
    ("Acquisition id", ["acq_id"]),
    ("Trigger time (Unix s)", ["t_trigger"]),
]


def _channel_key(data: dict, cid: str) -> str | None:
    for key, d in data.items():
        if str(d.get("attrs", {}).get("channel", "")) == cid:
            return key
    return cid if cid in data else None


def _table(data: dict, prefix: str) -> tuple[list[str], list[np.ndarray]]:
    names, cols = [], []
    for col, candidates in COLUMNS:
        for c in candidates:
            key = _channel_key(data, c[1:]) if c.startswith("@") else f"{prefix}{c}"
            if key is not None and key in data:
                names.append(col)
                cols.append(np.asarray(data[key]["values"], dtype=float).ravel())
                break
    return names, cols


def export_csv(h5_path: Path | str, csv_path: Path | str | None = None) -> Path:
    h5_path = Path(h5_path)
    csv_path = Path(csv_path) if csv_path else h5_path.with_suffix(".csv")
    content = read_file(h5_path)
    data = {k: v for k, v in content["data"].items() if "/" not in k}
    names, cols = _table(data, "")
    n = max((len(c) for c in cols), default=0)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# Samba_mini export of {h5_path.name}\n")
        for k in ("scan_type", "scan_status", "mode", "timestamp", "timestamp_end"):
            if k in content["attrs"]:
                f.write(f"# {k}: {content['attrs'][k]}\n")
        meta = content["metadata"]
        for k in ("workflow_title", "sample_id", "operator", "user", "setup", "geometry", "software_version"):
            if k in meta:
                f.write(f"# {k}: {meta[k]}\n")
        if "params_json" in meta:
            try:
                for pk, pv in json.loads(meta["params_json"]).items():
                    f.write(f"# param {pk}: {pv}\n")
            except (TypeError, ValueError):
                pass
        if content["attrs"].get("mode") == "simulation":
            f.write("# SIMULATED DATA - not a measurement\n")
        w = csv.writer(f)
        w.writerow(names)
        for i in range(n):
            row = []
            for c in cols:
                v = c[i] if i < len(c) else np.nan
                row.append("" if not np.isfinite(v) else repr(float(v)))
            if any(row):
                w.writerow(row)
        # Averaged loop (B-sweeps) as a second table.
        avg = {k[len("averaged/"):]: v for k, v in content["data"].items() if k.startswith("averaged/")}
        if avg:
            f.write("\n# Averaged loop (forward and backward branches averaged separately)\n")
            a_names, a_cols = _table(avg, "")
            if "n_sweeps" in avg:
                a_names.append("Sweeps averaged")
                a_cols.append(np.asarray(avg["n_sweeps"]["values"], dtype=float))
            w.writerow([f"{name} [mean]" if name not in ("Branch (0 fwd, 1 bwd)", "Sweeps averaged") else name
                        for name in a_names])
            m = max((len(c) for c in a_cols), default=0)
            for i in range(m):
                w.writerow(["" if not np.isfinite(c[i]) else repr(float(c[i])) for c in a_cols])
    return csv_path


def main(argv=None) -> int:
    """``python -m core.storage.csv_export FILE.h5 [OUT.csv]``"""
    import sys
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(main.__doc__)
        return 2
    out = export_csv(args[0], args[1] if len(args) > 1 else None)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
