# How Samba_controls works, and how to add a device

## 1. The pieces

```
 Samba_controls window ──starts/stops──► device service  (python -m Samba_controls.service)
        │                                   │  owns every instrument; nothing else opens them
        │  ZeroMQ on 127.0.0.1               │
        ├──────────── RPC :47811 ───────────►│  login, control (lease), device calls, measurements
        ◄──────────── events :47812 ─────────┤  readbacks, state changes, lease changes
 Samba_mini window ──────── same ───────────►│
```

* **The service** is a separate, Qt-free process.  At start it reads
  `%LOCALAPPDATA%\SambaMini\config\machine.toml`, creates one Python object per
  instrument (`build_devices()` in `Samba_controls/service/host.py`) and
  connects them.  The Samba_controls window always starts it on the real
  instruments; simulated instruments exist only for development and tests
  (`scripts\service_console.bat --mode simulation`).
* **One worker thread per instrument** (`service/worker.py`): every call to an
  instrument is queued on its own thread, one at a time, with a timeout.  A
  call that times out or is cancelled is dropped before it reaches the driver,
  so a late write can never hit the hardware.
* **Monitor**: while no measurement runs, the service reads each device's
  `MONITORED` attributes about once a second and publishes them as
  `readback.<device>` events.  The Samba_controls device panels, the
  Hardware box and the Calibration plot of Samba_mini show these.
* **Accounts and control**: you log in (accounts live in the service); to
  change anything (set the coil, move a stage, run a measurement) you need
  *control of the setup* (the lease).  Only one person at a time has it;
  reading is always allowed.  When control is released or the service stops,
  the service brings the outputs to a safe state (coil 0 V, lock-in output
  off, stages stopped).
* **Measurements** run as *operations* of the logical device `moke`
  (`service/devices/logical.py`): Samba_mini's engine asks it for points and
  writes the HDF5 file.

### The device model (TANGO-like)

Every instrument is a subclass of `Device` (`service/devices/base.py`) and
describes itself:

| Part | What it is | Example (Hall probe) |
|---|---|---|
| state | OFF, INIT, ON, MOVING, RUNNING, FAULT, DISABLED, with a status text | ON, "connected" |
| attributes | named values with type, unit, read-only or read-write, limits | `field_mT` (r), `integration_time_s` (rw, 0–0.6 s) |
| commands | named actions with typed, range-checked arguments; *mutating* commands need control | `zero(timeout_s)`, `read_field()` |
| operations | long-running commands with progress, pause and abort | `moke.time_trace(...)` |

The Samba_controls window builds each device's panel **from that
description**: the attribute table, the write fields and one button per
command appear automatically.  Samba_mini uses the same calls through
`core/client.py`, e.g. `client.device("hall").read("field_mT")` or
`client.device("daq").command("set_outputs", coil_V=0.1)`.

## 2. Adding an instrument

Example: a power meter on a serial port that answers `POW?` with watts.
(Take the real commands from the vendor manual; never guess them.)

### 2.1 Driver — `Samba_controls/service/devices/powermeter.py`

```python
from core.model import ArgInfo, AttributeInfo, CommandInfo
from core.protocol import ErrorCode, ServiceError
from Samba_controls.service.devices.base import CallContext, Device


class PowerMeter(Device):
    KIND = "powermeter"
    MONITORED = ["power_W"]              # shown live while idle (about 1 Hz)
    MONITOR_INTERVAL_S = 1.0
    ATTRIBUTES = [
        AttributeInfo("power_W", "float", "W", "r", label="Power", description="Optical power"),
        AttributeInfo("wavelength_nm", "float", "nm", "rw", 400, 1100, "Correction wavelength"),
    ]
    COMMANDS = [
        CommandInfo("zero", [], timeout_s=30.0, description="Dark-zero the sensor (beam blocked)"),
    ]

    def _connect(self):                  # runs on this device's worker thread
        import pyvisa                    # import the vendor library here, never at module import
        port = self.config.get("resource", "")
        if not port:
            raise ServiceError(ErrorCode.NOT_SUPPORTED, "set [powermeter] resource in machine.toml")
        self._inst = pyvisa.ResourceManager().open_resource(port, timeout=2000)
        self.identity.update({"model": self._inst.query("*IDN?").strip(), "resource": port})

    def _disconnect(self):
        inst, self._inst = getattr(self, "_inst", None), None
        if inst is not None:
            inst.close()

    def _read_attribute(self, name):
        if name == "power_W":
            return float(self._inst.query("POW?"))
        if name == "wavelength_nm":
            return float(self._inst.query("WAV?"))
        raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, name)

    def _write_attribute(self, name, value):
        if self.config.get("read_only"):
            raise ServiceError(ErrorCode.FORBIDDEN, "read-only diagnostics mode")
        if name == "wavelength_nm":
            self._inst.write(f"WAV {value:.1f}")
            return value
        raise ServiceError(ErrorCode.UNKNOWN_ATTRIBUTE, name)

    def cmd_zero(self, ctx: CallContext):
        self._inst.write("ZERO")
        return {"ok": True}

    def cleanup(self, ctx):              # safe state when control is released / service stops
        return []                        # a power meter has no output to switch off
```

Rules the existing drivers follow:
* no instrument I/O at import time, and no writes when `read_only` is set;
* every wait is bounded (timeouts), and errors are raised as `ServiceError`;
* file locations (DLLs, ports) come from `machine.toml`, never from code;
* `cleanup()` returns a list of problems instead of raising.

### 2.2 Configuration — `core/config/machine.py`

Add a section to `DEFAULTS`, **disabled by default** so existing PCs are not
affected, and check it in `validate()`:

```python
"powermeter": {"enabled": False, "resource": ""},
```

Document it in `config/examples/machine.example.toml`.  On the PC, add to
`machine.toml`:

```toml
[powermeter]
enabled = true
resource = "ASRL5::INSTR"
```

### 2.3 Register it — `build_devices()` in `Samba_controls/service/host.py`

```python
from Samba_controls.service.devices.powermeter import PowerMeter
devices.append(PowerMeter("powermeter", dict(data["powermeter"], **ro)))
enabled["powermeter"] = data["powermeter"]["enabled"]
```

(Put it in the real-hardware branch.  Optionally add a simulated twin to
`service/devices/sim.py` for the simulation branch, so development and tests
work without the instrument.)

Restart the service (Stop/Start in Samba_controls).  The device appears in the
device list with its attributes and a **zero** button; nothing else is needed
to read it, write it and see it live.

### 2.4 Test it — `tests/test_drivers.py`

Drive the real class against a small fake of the vendor library (see the
existing fakes for `pyvisa`, `nidaqmx`, `pylablib`): connect, read, write,
refused writes in read-only mode, timeouts.  Run `scripts\run_tests.bat`.

### 2.5 Recording it in measurements (optional, more work)

To store the new value at every measured point:
1. read it in the point routine of `MokeSequencer` (`service/devices/logical.py`,
   `point()`) on the instrument's own worker, e.g.
   `host.run("powermeter", lambda d: d.read_attribute("power_W").value, 5.0, ctx.cancel, "power")`,
   and put it into the point record (`rec["power_W"] = ...`);
2. add a channel to `SENSORS` in `core/scan/channels.py`
   (`Channel("power", "power_W", "Optical power", "W")`) — the engine then
   writes it to the HDF5 file and it shows up in the plots;
3. add it to `DEVICES` in `Samba_mini/panels/right_panel.py` so it can be
   picked in the sensor rows.

Point timing matters here: a slow instrument read inside the point routine
lengthens every point.

## 3. Where things are

| What | File |
|---|---|
| Start/stop the service, device list, device panels | `Samba_controls/app.py`, `Samba_controls/panels/devices.py` |
| Users, machine configuration, MiniMoke import | `Samba_controls/panels/admin.py` |
| Service entry point, flags (`--read-only`, `--mode`) | `Samba_controls/service/__main__.py` |
| RPC dispatch, permissions, logging of manual connects | `Samba_controls/service/server.py` |
| Device creation, workers, monitor, cleanup | `Samba_controls/service/host.py` |
| Device base class | `Samba_controls/service/devices/base.py` |
| Drivers | `Samba_controls/service/devices/*.py` |
| Machine configuration defaults and checks | `core/config/machine.py` |
| Client used by both windows | `core/client.py` |
