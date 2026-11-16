"""Incremental HDF5 writer in Samba's layout.

Layout (compatible with Samba's ``core/data_browser.py``)::

    /                     attrs: scan_status, scan_type, timestamp, timestamp_end,
                                 duration_seconds, _x_key, _is_2d, _is_time, _n_x,
                                 mode ("real"|"simulation"), file_format
    /metadata             attrs: scan_name, n_x, n_y, points_planned, points_acquired,
                                 operator, sample_id, user, workflow, params_json, ...
    /data/<name>          datasets with attrs role (x, x_setpoint, y, y_setpoint, time,
                                 sensor, index, timestamp, readback), label, unit
    /data/<group>/<name>  extra groups (e.g. the averaged B-sweep loop)

Recovery guarantees (documented, tested):

* datasets are created at full size and filled with NaN, so unmeasured points
  are NaN, never 0;
* ``scan_status`` is "running" until :meth:`finalize`; a file left "running"
  after a crash is recognisable as partial;
* the file is flushed every ``flush_every`` points, at every ``checkpoint()``
  (end of a sweep/row) and at finalisation.  After a process crash the data up
  to the last flush are readable; points after it may be missing.  A power
  loss can additionally lose what the OS had not yet written to disk.  The
  HDF5 file itself can be damaged if the process dies inside a write; in that
  case ``h5py`` may refuse to open it (``h5clear``/re-acquisition needed).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

log = logging.getLogger(__name__)

FILE_FORMAT = "samba-mini-h5/1"


class StorageError(Exception):
    pass


@dataclass
class DatasetSpec:
    name: str
    role: str
    label: str
    unit: str
    group: str = "data"
    dtype: str = "f8"
    shape: tuple | None = None         # defaults to the writer's main shape


def _attr(value: Any) -> Any:
    """Make a value storable as an HDF5 attribute (strings via string dtype)."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    return value


class ScanWriter:
    def __init__(self, path: Path | str, shape: tuple, specs: list[DatasetSpec], root_attrs: dict,
                 metadata: dict, flush_every: int = 10):
        self.path = Path(path)
        self.shape = tuple(int(n) for n in shape)
        self.specs = {(s.group, s.name): s for s in specs}
        self.root_attrs = root_attrs
        self.metadata = metadata
        self.flush_every = max(1, int(flush_every))
        self.points_acquired = 0
        self._since_flush = 0
        self._t0 = time.time()
        self._f: h5py.File | None = None
        self.finalized = False

    # ------------------------------------------------------------ create
    def create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise StorageError(f"refusing to overwrite existing file {self.path}")
        try:
            f = h5py.File(self.path, "w")
        except OSError as exc:
            raise StorageError(f"cannot create {self.path}: {exc}") from exc
        self._f = f
        try:
            f.attrs["file_format"] = FILE_FORMAT
            f.attrs["scan_status"] = "running"
            f.attrs["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._t0))
            for k, v in self.root_attrs.items():
                f.attrs[k] = _attr(v)
            meta = f.create_group("metadata")
            for k, v in self.metadata.items():
                meta.attrs[k] = _attr(v)
            meta.attrs["points_acquired"] = 0
            for (group, name), spec in self.specs.items():
                g = f.require_group(group)
                shape = spec.shape or self.shape
                fill = -1 if spec.dtype.startswith("i") else np.nan
                ds = g.create_dataset(name, shape=shape, dtype=spec.dtype, fillvalue=fill)
                ds.attrs["role"] = spec.role
                ds.attrs["label"] = spec.label
                ds.attrs["unit"] = spec.unit
            f.flush()
        except Exception as exc:
            self._close_quietly()
            raise StorageError(f"cannot initialise {self.path}: {exc}") from exc

    # ------------------------------------------------------------ write
    def write(self, index: tuple, values: dict, group: str = "data", count_point: bool = True) -> None:
        """Write one point's values at *index* into datasets of *group*."""
        if self._f is None:
            raise StorageError("file not open")
        try:
            for name, value in values.items():
                spec = self.specs.get((group, name))
                if spec is None:
                    continue
                ds = self._f[group][name]
                ds[index] = np.nan if value is None else value
            if count_point:
                self.points_acquired += 1
                self._f["metadata"].attrs["points_acquired"] = self.points_acquired
            self._since_flush += 1
            if self._since_flush >= self.flush_every:
                self.flush()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StorageError(f"write failed ({self.path.name}): {exc}") from exc

    def set_metadata(self, **values) -> None:
        if self._f is None:
            return
        try:
            for k, v in values.items():
                self._f["metadata"].attrs[k] = _attr(v)
        except OSError as exc:
            raise StorageError(f"metadata write failed: {exc}") from exc

    def checkpoint(self) -> None:
        self.flush()

    def flush(self) -> None:
        if self._f is None:
            return
        try:
            self._f.flush()
            self._since_flush = 0
        except OSError as exc:
            raise StorageError(f"flush failed: {exc}") from exc

    # ------------------------------------------------------------ finish
    def finalize(self, status: str, extra_metadata: dict | None = None) -> None:
        """Set the final status ('completed', 'aborted', 'failed', 'interrupted')."""
        if self._f is None or self.finalized:
            return
        try:
            if extra_metadata:
                self.set_metadata(**extra_metadata)
            self._f.attrs["scan_status"] = status
            t1 = time.time()
            self._f.attrs["timestamp_end"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t1))
            self._f.attrs["duration_seconds"] = round(t1 - self._t0, 3)
            self._f["metadata"].attrs["points_acquired"] = self.points_acquired
            self._f.flush()
        finally:
            self.finalized = True
            self._close_quietly()

    def _close_quietly(self) -> None:
        try:
            if self._f is not None:
                self._f.close()
        except Exception:  # noqa: BLE001
            pass
        self._f = None


def make_filename(save_dir: Path | str, scan_type: str, sample: str, config_name: str,
                  simulated: bool, when: float | None = None) -> Path:
    """Samba: ``<save_dir>/<YYYYMMDD>/<HHMMSS>_<SCANTYPE>_<sample>_<config>.h5``
    (``_SIM`` appended in simulation mode)."""
    when = when or time.time()
    lt = time.localtime(when)
    day = time.strftime("%Y%m%d", lt)
    stem = f"{time.strftime('%H%M%S', lt)}_{scan_type}_{_safe(sample) or 'sample'}"
    if config_name:
        stem += f"_{_safe(config_name)}"
    if simulated:
        stem += "_SIM"
    path = Path(save_dir) / day / f"{stem}.h5"
    n = 2
    while path.exists():
        path = Path(save_dir) / day / f"{stem}_{n}.h5"
        n += 1
    return path


def _safe(name: str) -> str:
    name = (name or "").strip()
    for bad in '\\/:*?"<>| ':
        name = name.replace(bad, "_")
    return name.strip("._")[:60]


def read_file(path: Path | str) -> dict:
    """Read a whole file into plain Python structures (for tests, CSV export, analysis)."""
    out: dict[str, Any] = {"attrs": {}, "metadata": {}, "data": {}}
    with h5py.File(path, "r") as f:
        out["attrs"] = {k: _decode(v) for k, v in f.attrs.items()}
        if "metadata" in f:
            out["metadata"] = {k: _decode(v) for k, v in f["metadata"].attrs.items()}

        def visit(name, obj):
            if isinstance(obj, h5py.Dataset):
                out["data"][name] = {"values": obj[()],
                                     "attrs": {k: _decode(v) for k, v in obj.attrs.items()}}
        if "data" in f:
            f["data"].visititems(visit)
    return out


def _decode(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, np.generic):
        return v.item()
    return v
