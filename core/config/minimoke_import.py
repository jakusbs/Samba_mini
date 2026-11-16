"""Import settings from an existing MiniMoke installation (read-only).

Reads ``<minimoke>/configs/*.ini`` and the ``libs/MagnetPhysik.Usb.dll``
location; never writes into the MiniMoke folder.  Produces:

* machine configuration values (DAQ channels, lock-in resource, stage serials,
  conversion constants, Hall DLL path) merged over the current/default
  ``machine.toml``;
* stage software-zero offsets for ``state\\stage_offsets.json``;
* named presets from ``procedures_config.ini`` (MiniMoke's "remember last
  settings"), to be stored in a user's account.

Usage::

    python -m core.config.minimoke_import --from "C:\\Users\\...\\Minimoke-bugfixed" [--write] [--mode real]
"""

from __future__ import annotations

import argparse
import configparser
import copy
import json
import sys
from pathlib import Path

from core import paths
from core.config import machine

# MiniMoke parameter name -> Samba_mini parameter name (same unless listed)
PARAM_RENAMES: dict[str, str] = {}


def _read_ini(path: Path) -> configparser.ConfigParser | None:
    cp = configparser.ConfigParser()
    try:
        if cp.read(path, encoding="utf-8"):
            return cp
    except (configparser.Error, UnicodeDecodeError):
        try:
            if cp.read(path, encoding="latin-1"):
                return cp
        except configparser.Error:
            return None
    return None


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def read_minimoke(src: Path | str) -> dict:
    """Return {"machine": partial machine config, "offsets": {...}, "presets": {...}, "notes": [...]}."""
    src = Path(src)
    cfg_dir = src / "configs"
    notes: list[str] = []
    if not cfg_dir.is_dir():
        raise FileNotFoundError(f"{cfg_dir} not found (expected a MiniMoke folder with configs/)")
    out_machine: dict = {"daq": {}, "lockin": {}, "hall": {}, "stages": {"longitudinal": {}, "polar": {}}}
    offsets: dict = {}

    dac = _read_ini(cfg_dir / "dac_config.ini")
    if dac and dac.has_section("IO ports"):
        io = dac["IO ports"]
        mapping = {"AC_Output1": "ao_mod1", "AC_Output2": "ao_mod2", "Coils_Output": "ao_coil",
                   "Balanced_diodes_Input": "ai_balanced", "Intensity_diode_Input": "ai_intensity"}
        for k, v in mapping.items():
            if io.get(k):
                out_machine["daq"][v] = io.get(k).strip()
        dev = out_machine["daq"].get("ao_coil", "Dev1/ao3").split("/")[0]
        out_machine["daq"]["device"] = dev
        out_machine["daq"]["trigger_terminal"] = f"/{dev}/PFI0"
        notes.append("DAQ channels from dac_config.ini (trigger terminal /<dev>/PFI0 as in dac_class.py)")

    resource = None
    for name in ("instruments_config.local.ini", "instruments_config.ini"):
        ic = _read_ini(cfg_dir / name)
        if ic and ic.has_option("LockIn", "resource"):
            model = ic.get("LockIn", "model", fallback="ametek7270")
            if model and model.strip().lower() not in ("", "ametek7270"):
                notes.append(f"{name}: lock-in model {model!r} is not supported (Ametek 7270 only); ignored")
                continue
            resource = ic.get("LockIn", "resource").strip()
            notes.append(f"lock-in resource from {name}")
            break
    if resource:
        out_machine["lockin"]["resource"] = resource

    ls = _read_ini(cfg_dir / "longitudinal_stage_config.ini")
    if ls:
        st = out_machine["stages"]["longitudinal"]
        if ls.has_section("Serials"):
            for a in ("x", "y", "z"):
                if ls["Serials"].get(a):
                    st[f"serial_{a}"] = ls["Serials"].get(a).strip()
        if ls.has_section("Constants"):
            if ls["Constants"].get("mm2steps"):
                st["mm2steps"] = _num(ls["Constants"].get("mm2steps"), 2184533.0)
            if ls["Constants"].get("backlash_mm"):
                st["backlash_mm"] = _num(ls["Constants"].get("backlash_mm"), 0.02)
        if ls.has_section("Offsets"):
            offsets["longitudinal"] = {a: _num(ls["Offsets"].get(f"offset_{a}"), 0.0) for a in ("x", "y", "z")}
    ps = _read_ini(cfg_dir / "polar_stage_config.ini")
    if ps:
        st = out_machine["stages"]["polar"]
        if ps.has_option("Connection", "port"):
            st["port"] = ps.get("Connection", "port").strip()
        if ps.has_section("Constants"):
            for a in ("x", "y", "z"):
                v = _num(ps["Constants"].get(f"mm2steps_{a}"))
                if v:
                    st[f"mm2steps_{a}"] = v
        if ps.has_section("Offsets"):
            offsets["polar"] = {a: _num(ps["Offsets"].get(f"offset_{a}"), 0.0) for a in ("x", "y", "z")}

    dll = src / "libs" / "MagnetPhysik.Usb.dll"
    if dll.is_file():
        out_machine["hall"]["dll_path"] = str(dll)
        notes.append(f"Hall DLL found at {dll} (not copied; vendor file)")

    presets = {}
    pc = _read_ini(cfg_dir / "procedures_config.ini")
    if pc:
        from core.scan.workflows import MINIMOKE_SECTIONS, WORKFLOWS
        for section in pc.sections():
            key = MINIMOKE_SECTIONS.get(section)
            if key is None:
                notes.append(f"procedures_config.ini [{section}] has no Samba_mini workflow; skipped")
                continue
            wf = WORKFLOWS[key]
            known = {p.name for p in wf.params}
            values = {}
            for k, v in pc[section].items():
                name = PARAM_RENAMES.get(k, k)
                if name in known:
                    num = _num(v)
                    if num is not None:
                        values[name] = num
            try:
                values = wf.coerce(values)
            except ValueError as exc:
                notes.append(f"[{section}] values outside Samba_mini limits ({exc}); defaults used where invalid")
                values = {k: v for k, v in values.items() if _valid(wf, k, v)}
            presets[key] = values
    return {"machine": out_machine, "offsets": offsets, "presets": presets, "notes": notes}


def _valid(wf, name, value) -> bool:
    try:
        wf.coerce({name: value})
        return True
    except ValueError:
        return False


def apply(result: dict, mode: str | None = None, config_path: Path | None = None,
          state_dir: Path | None = None) -> dict:
    """Merge into machine.toml (a backup copy is made first) and write stage offsets."""
    config_path = Path(config_path) if config_path else paths.machine_config_path()
    current = machine.load(config_path)
    base = current.data if current.status != "invalid" else copy.deepcopy(machine.DEFAULTS)
    merged = machine.deep_merge(base, result["machine"])
    if mode:
        merged["backend"]["mode"] = mode
    machine.save(merged, config_path, backup=True)
    out = {"config": str(config_path)}
    if result.get("offsets"):
        out["offsets"] = str(write_offsets(result["offsets"], state_dir))
    return out


def write_offsets(offsets: dict, state_dir: Path | None = None) -> Path:
    state_dir = Path(state_dir) if state_dir else paths.state_dir()
    path = state_dir / "stage_offsets.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            backup = path.with_suffix(".json.bad")
            path.replace(backup)
    for stage, offs in offsets.items():
        data[stage] = offs
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="core.config.minimoke_import")
    ap.add_argument("--from", dest="src", required=True, help="MiniMoke folder (contains configs/)")
    ap.add_argument("--write", action="store_true", help="write machine.toml and stage offsets")
    ap.add_argument("--mode", choices=["real", "simulation"], help="set backend.mode when writing")
    ap.add_argument("--config", help="machine.toml to write (default: per-machine location)")
    args = ap.parse_args(argv)
    res = read_minimoke(args.src)
    print(json.dumps({k: v for k, v in res.items() if k != "presets"}, indent=2))
    print(f"presets found: {sorted(res['presets'])}")
    if args.write:
        written = apply(res, args.mode, Path(args.config) if args.config else None)
        for what, where in written.items():
            print(f"wrote {what}: {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
