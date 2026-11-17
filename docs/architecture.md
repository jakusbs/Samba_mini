# Samba_mini architecture

This document records the source audit that the design is based on, the
reuse/adaptation map, and the architecture of the two applications.  The phased
plan, workflow parity table and dependency assessment are in
[implementation-plan.md](implementation-plan.md); current status is in
[progress.md](progress.md).

## 1. Source audit

### 1.1 Revisions inspected (2026-11-16)

| Source | Location on the measurement PC | Revision |
|---|---|---|
| Samba | `C:\Samba_Mini\Samba-main` (zip snapshot) | identical (ignoring CR/LF) to `jakusbs/Samba` `75c0bdf4` (2026-09-29) |
| MiniMoke | `C:\Samba_Mini\MiniMoke-main` (zip snapshot) | identical (ignoring CR/LF) to `jakusbs/MiniMoke` `6c7c0fdd` (2026-07-16) |
| Local MiniMoke installs | `Documents\Minimoke-bugfixed` (newest, Oct 2026), `Desktop\MiniMoke-main_mlfi`, `Documents\minimoke`, `minimoke-upgrade`, `minimoke_motor` (git, GitLab remotes, local modifications) | not modified; read only |
| MiniMoke Python env | `miniconda3\envs\MOKE` | Python 3.11.15, nidaqmx 1.4.1, pyvisa 1.16.2, pythonnet 3.0.5, pylablib 1.4.5, pytrinamic 0.2.23, pymeasure 0.15.0, PyQt5 5.15.11 |

The newest local install (`Minimoke-bugfixed`) diverges from MiniMoke HEAD: it
lacks `run_control.py` (no pause), its time trace has no pause gate, and it
contains an unused experiment `b_sweep_proc_new.py` whose note says the DC
reading "waits some time for the magnet to settle" (see finding F1).  Its
configuration is the best evidence of the rig: lock-in on Ethernet
`TCPIP0::192.168.77.2::50000::SOCKET`, DAQ channels as in HEAD, Kinesis
serials `80855672/80855670/80855667`.  `Desktop\MiniMoke-main_mlfi` adds a
Zurich MFLI driver; MFLI is out of scope.

Vendor runtime present (inspected without opening devices): NI-DAQmx and NI-VISA
(`nicaiu.dll`, `visa64.dll`), Thorlabs Kinesis, Magnet-Physik
`MagnetPhysik.Usb.dll` 1.3.0.1 (same SHA-256 in every install; copyright
Magnet-Physik, not redistributed here).

### 1.2 MiniMoke (hardware and measurement reference)

* `__main__.py` imports PyQt5 + PyMeasure UI; `src/classes/__init__.py`
  constructs every driver at import (DAC, Hall probe, both stages, lock-in), so
  importing any class opens hardware.  Nothing from MiniMoke is imported by
  Samba_mini; drivers were re-implemented from the source with the same calls.
* **NI-DAQ** (`dac_class.py`): one finite AO task (`ao1`, `ao2`, `ao3`=coils)
  and one finite AI task (`ai0` balanced diodes, `ai1` intensity), both
  start-triggered by a rising edge on `/Dev1/PFI0`, which a persistent DO task
  on the same terminal produces.  50 kHz, `round(rate*t)+1` samples, the extra
  last AO sample holds the output (stripped from the AI data).  Static outputs
  (`set_outputs_and_reset`) re-time the AO task to 2 samples at 10 kHz.
* **Ametek 7270** (`ametek7270_class.py`, derived from PyMeasure, MIT):
  NUL-terminated ASCII; every set is followed by a read that must be empty;
  Ethernet socket framing sends one extra status chunk per response which is
  detected at connect (`ID` probe) and drained after every transaction;
  reconnect = reopen + VISA clear + re-probe; port 50000 → 50001 fallback;
  one session shared for configuration and measurement; per point `XY.` read.
* **Hall probe** (`hallsensor_class.py`): pythonnet `clr.AddReference` of a
  hard-coded `C:/Users/intermag/Documents/minimoke/libs/...` path,
  `MP.HallProbe()`, `Speed=1` (470 Hz), `Filter = round(t*470)` clamped to 255
  (max ≈0.54 s), `read_mT` sleeps the integration time then `get_Tesla()*1000`.
* **Longitudinal stage**: pylablib `Thorlabs.KinesisMotor(serial)` ×3,
  `mm2steps = 2184533`, controller backlash `0.02 mm`, offsets in steps written
  back to the INI on "home" (software zero), `wait_move()` without timeout.
* **Polar stage**: pytrinamic TMCM-3212 over `usb_tmcl`, axis map X=2, Y=1, Z=0,
  per-axis currents and StealthChop parameters, `mm2steps = 51200`, Y and Z
  sign-inverted, completion by polling axis parameter 8 without timeout.
* **Procedures**: DC B-sweep, lock-in B-sweep, X/Y/XY position sweeps (shared
  `PositionSweep`), time trace; pause via a global flag (`run_control`).

### 1.3 Samba (structure and interface reference)

* PyQt6 + matplotlib, Catppuccin-Mocha dark theme (`core/theme.py`, `DARK_STYLE`
  in `samba.py`), no menu bar: action bar with setup pills and
  Start/Pause/Abort, horizontal splitter (named configuration list │ live tabs
  "2D Map/1D Plot/Log" │ sensor/plot panel), bottom tabs (Trajectory, Data
  Browser, …), eight-field status bar.
* `core/scan/runner.py` claims to be Qt-free but imports `PyQt6.QtCore` (unused)
  and `tango` directly; `workers.py` wraps it in a `QThread`.
* `core/hardware.py` caches a `SimProxy` for any unreachable device and, without
  PyTango, the runner records SimProxy data (beam-spot-like Gaussian, field =
  0.15·I) into real HDF5 files with only a log warning; `_read_hw_snapshot`
  stores SimProxy values in metadata.  None of this is ported.
* HDF5: `/data/<name>` datasets tagged with `role` (`x`, `x_setpoint`, `y`,
  `time`, `sensor`), `label`, `unit`; root `scan_status`/`scan_type`;
  `/metadata` attributes; pre-filled NaN arrays written per point and flushed
  every 10 points.  `core/data_browser.py` reads this by role and is reusable.
* Config: one JSON per setup with schema migrations; save is not atomic.

### 1.4 Findings that change behaviour (intentional correctness fixes)

| # | MiniMoke behaviour (source) | Consequence | Samba_mini |
|---|---|---|---|
| F1 | The coil value is only written to the DAQ inside the next acquisition buffer (`start_tasks`); B-sweep "settle" sleeps run *before* that (`b_sweep_proc.py`, `b_sweep_ac.py`). | DC average includes the coil transient; the lock-in B-sweep reads X/Y right after the step, i.e. it reports the *previous* field point; position sweeps acquire during the +B→−B reversal. | Coil step and settling are part of one hardware-timed DAQ generation: the buffer starts at the new coil value, the first `settle` samples of the AI record are discarded, the lock-in is read after the settle window. |
| F2 | Fast DC sweeps store `Magnetic Field (T) = b_set·1000/1000` (placeholder `A_TO_MT_APPROX`) when the Hall probe is skipped. | Synthetic value indistinguishable from a measurement.  Latent: MiniMoke's queue validation rejects every sweep with points < 2 ms, so this path is unreachable from its UI. | Same validation kept.  If the Hall probe is not read, the field is NaN with `hall_measured=False`; an estimate exists only as the separate `magnet.field_estimate_mT` attribute when a calibration is configured, and is never written as a measurement. |
| F3 | Fast-mode "slow Hall reads" set `dac.coils_output` without writing it. | Every averaged-loop field value would be the field at the last applied point.  Latent for the same reason as F2. | Not ported (unreachable); the averaged field is the mean of the simultaneous Hall readings. |
| F4 | DC B-sweep writes the averaged intensity into `Intensity STD (V)`; θ is labelled `(V)`. | Mislabelled columns. | Separate datasets with units (`deg` for θ). |
| F5 | Missing DAQ returns zeros, missing Hall returns 0.0 mT, missing lock-in uses `OfflineLockin` returning zeros, missing stage silently skips moves and records NaN. | Plausible-looking data from absent hardware. | Real mode refuses to start a workflow whose required devices are not ON; simulation is a separate, labelled mode. |
| F6 | Hall zeroing busy-waits forever; stage waits have no timeout; Kinesis jog is silently ignored while moving. | Hangs, lost commands. | Bounded waits, `TIMEOUT`/`BUSY` errors. |
| F7 | `n = int(|max−min| // step + 1)` with floats can drop the end point (e.g. 1.4 // 0.005 = 279.0). | Fewer points, different actual step. | Same formula with a 1e-9 relative tolerance; the actual step is recorded. |
| F8 | Lock-in USB recovery calls `pnputil /restart-device`, which needs administrator rights. | Silent failure for normal users. | Disabled by default (`lockin.allow_usb_reset = false`); the rig uses Ethernet. |

## 2. Reuse / adaptation map

| Samba / MiniMoke source | Samba_mini target | How |
|---|---|---|
| Samba `core/theme.py`, `DARK_STYLE` | `core/theme.py` | copied; stylesheet moved next to the tokens |
| Samba `core/plot_interact.py`, `plot_widgets.py`, `kerr.py` | `core/plot_interact.py`, `core/plot_widgets.py`, `core/kerr.py` | copied; bare imports → package imports, config constants from `core/plot_config.py` |
| Samba `core/data_browser.py`, `scan_index.py` | same names in `core/` | copied; package imports; search index in `%LOCALAPPDATA%\SambaMini\state`; sub-group channels (averaged loop, XY repeats) added |
| Samba `panels/config_list.py`, `_widgets.py` (NoScroll widgets) | `Samba_mini/panels/config_list.py`, `widgets.py` | adapted; configurations stored per user in the service's account database |
| Samba `panels/trajectory.py` (scan-type pills, ActuatorGroup, field sub-modes, field monitor, Timing), `core/nstep.py` | `Samba_mini/panels/trajectory.py`, `widgets.py` | same arrangement: Spatial (X/Y axis boxes; none ticked = time scan) and Field (Lock-in / DC B-sweep side by side); maps onto the six MiniMoke workflows without changing their parameters; N↔step coupling uses MiniMoke's point rule |
| Samba `_widgets.py` MokeMetadataGroup | `Samba_mini/panels/metadata.py` | same fields and keys; incidence replaces the PMOKE/LMOKE buttons (polar ⇒ PMOKE) |
| Samba `panels/hardware_panel.py` | `Samba_mini/panels/hardware.py` | Lock-in (7270) / Field / Detectors instead of Lock-in / Current Source / Field & Relay; lock-in values are per measurement (MiniMoke) |
| Samba `core/calibration.py` stage group | `Samba_mini/panels/calibration.py` | Calibration tab with stage positioning; no autofocus/LEDs |
| Samba `panels/right_panel.py`, `sensor_picker.py` | `Samba_mini/panels/right_panel.py` | same rows; the checkbox only plots (every MiniMoke channel is recorded) |
| Samba `panels/scanlist.py`, `ScanlistWorker`, `core/polarity.py` | `Samba_mini/panels/scanlist.py`, `core/polarity.py` (copied), scanlist loop in `Samba_mini/app.py` | N scans of the Trajectory measurement, field flip AB/ABBA by reversing the coil command; Samba's `.txt` (path, relay_sign, field_T); no relay flip, current sweep or refocus |
| Samba `core/bd_calibration.py`, `core/bd_fit.py` | `Samba_mini/panels/bd_calibration.py`, `core/bd_fit.py` (copied) | values per user and setup in the account database; `/data/calibration` in every new file; fit accepts `dc_signal` and `_SIM` names |
| Samba `samba.py` layout | `Samba_mini/app.py` | layout reproduced, code rewritten (no god object) |
| Samba runner HDF5 writer (`_open_hdf5`, `_write_point`, `_finalize_hdf5`) | `core/storage/hdf5_writer.py` | re-implemented with the same schema; no `hw_*`/TANGO fields |
| Samba `ScanWorker`/runner control flags | `core/scan/engine.py` | re-implemented Qt-free with an explicit state machine |
| Samba `config.py` migrations | `core/config/machine.py` | pattern kept; atomic save, backups, `.bad` copy of malformed files |
| MiniMoke `dac_class.py` | `Samba_controls/service/devices/nidaq.py` | same task/trigger structure; settle-prefix fix (F1); unavailable ≠ zeros |
| MiniMoke `ametek7270_class.py` | `.../devices/ametek7270.py` | protocol, framing probe, reconnect ladder re-implemented on PyVISA (no PyMeasure); MIT notice kept |
| MiniMoke `hallsensor_class.py` | `.../devices/hall_probe.py` | configurable DLL path, bounded zeroing |
| MiniMoke stage classes | `.../devices/kinesis_stage.py`, `trinamic_stage.py` | same conversions, signs, motor parameters; bounded waits; stop |
| MiniMoke procedures | `core/scan/workflows.py`, `plans.py`, backend `moke` sequencer | parity table in the plan |
| MiniMoke `data_archive.py` lab notebook | `core/storage/lab_notebook.py` | CSV row per run, same columns (+ user, mode, file status) |

Not ported: Samba `hardware.py` SimProxy, `setup_lock.py`, TANGO panels,
DC-hysteresis/PLC/Beckhoff, Zurich, SmarAct, Cryo, DG645, relay flip,
current sweep, autofocus/refocus, LEDs, script console, NAS server sync;
Samba's Device Registry and Setup Defaults (hardware configuration belongs
to Samba_controls here); MiniMoke PyMeasure UI and global initialisation,
Zurich MFLI.

## 3. Architecture

```
Samba_mini (repo)
├── core/                     shared library
│   ├── protocol.py           wire format, error codes, safe JSON (+ndarray, NaN)
│   ├── model.py              TANGO-inspired DevState, attribute/command metadata
│   ├── client.py             ServiceClient + DeviceProxy (Qt-free)
│   ├── units.py              unit metadata and conversions
│   ├── paths.py              %LOCALAPPDATA%\SambaMini locations
│   ├── config/               machine config (TOML, schema-versioned), MiniMoke INI import
│   ├── scan/                 workflows, plans, Qt-free ScanEngine
│   ├── storage/              HDF5 writer/reader, CSV export, lab notebook
│   └── theme.py, plot_*.py, data_browser.py, gui/   (PyQt6 only)
├── Samba_controls/
│   ├── service/              backend process (Qt-free): server, auth, lease, operations, devices
│   ├── app.py, panels/       PyQt6 management UI; starts/stops the backend process
├── Samba_mini/
│   ├── app.py, panels/       PyQt6 measurement UI; client proxies only
├── config/examples/          sanitised machine.example.toml
├── scripts/                  PowerShell/batch launchers, environment setup
└── tests/
```

### 3.1 Processes and ownership

```
 Samba_controls (PyQt6) ──spawns/stops──▶ backend process  (python -m Samba_controls.service)
        │  manual control, diagnostics          │ owns every driver session, one worker thread per
        │                                        │ physical instrument, logical devices, operations
        └────────── ZeroMQ, 127.0.0.1 ───────────┤
 Samba_mini (PyQt6) ── ScanEngine thread ────────┘
```

* **Backend** (`Samba_controls.service`): creates, connects, configures and
  closes devices; every call to a physical instrument runs on that instrument's
  single worker thread (`DeviceWorker`), so access is serialised per instrument.
  A queued call carries a deadline and a cancel token and is *dropped* if it
  has expired or been cancelled before it starts — a timed-out or aborted
  request can never issue a late write.  Duplicate hosts are prevented by an
  exclusive lock file in `state\` plus the port bind; a second start reports
  the running instance.  No Windows service, no administrator rights.
* **Samba_controls** starts the backend as a child process (or attaches to a
  running one), shows device state/status/errors/readbacks/configuration, and
  offers connect/disconnect/reconnect and manual controls.  Closing it asks
  whether to stop the backend; it refuses while a measurement holds the lease.
* **Samba_mini** never imports a driver package or `Samba_controls.service`
  (enforced by a test).  It runs the reusable `ScanEngine` in a worker thread
  and talks to the backend through `DeviceProxy` objects.

### 3.2 IPC choice

ZeroMQ (pyzmq, maintained, Windows wheels, already in the MOKE env) with JSON
messages.  ROUTER/DEALER gives asynchronous request/reply with request ids (no
REQ/REP lock-step after a timeout); PUB/SUB gives notifications.  Alternatives
considered: stdlib `multiprocessing.connection` (pickle — unsafe), HTTP/gRPC
(more machinery than needed on one PC).  Bound to `127.0.0.1` only.

Protocol details (see `core/protocol.py`): `proto: "samba-mini/2"` checked on
every message; request `id`; structured errors `{code, message, details}`;
per-request `timeout_s` capped at 120 s; JSON only — NaN/±inf and numpy
arrays have explicit encodings, arrays restricted to numeric dtypes; methods
dispatched through an explicit table, device commands through each device's
declared command list.  Long work returns an **operation handle**; progress and
data are pulled loss-free with `op.wait(op_id, since_seq)` (long-poll) while
PUB events only signal that something changed.

### 3.3 Device contract (TANGO-inspired, not TANGO)

Every device (real, simulated or logical) exposes:

* identity (`model`, `serial`, `resource`, `driver`), `capabilities`, `simulated`
* `state` (`ON`, `OFF`, `MOVING`, `RUNNING`, `FAULT`, `DISABLED`, `UNKNOWN`) and `status` text, `last_error`
* attribute metadata: name, dtype, unit, access, min/max, description
* `read`/`write` attributes (writes validated against configured limits in the backend)
* `command` metadata: arguments with units/limits, `mutating`, `idempotent`, timeout
* `connect`/`disconnect`/`reconnect`

Readings carry `quality`: `VALID`, `INVALID` (no reading), `STALE` (cached,
with age), `SIMULATED`.  A missing reading is NaN/`INVALID`, never 0.

Devices: `daq` (NI-DAQ owner), `lockin` (7270), `hall` (Hall probe),
`stage_longitudinal` (Kinesis), `stage_polar` (TMCM-3212), and two logical
devices: `magnet` (coil command through `daq`, field readback through `hall`)
and `moke` (acquisition sequencer that runs the timed per-point sequences using
the owners' workers).  Logical devices never open hardware themselves.

### 3.4 Accounts, sessions and the setup lease

* Accounts in SQLite (`data\accounts.sqlite3`, `schema_version` table with
  migrations, backup before migration).  Passwords hashed with Argon2id
  (`argon2-cffi`, salted, default work factors).  Roles: `admin` (edit machine
  configuration, manage users) and `operator`.  The first account created on an
  empty database becomes admin.
* Per-user preferences, per-setup working state, named presets and λ/2
  calibration records (schema 2) are stored in the same database and served by
  the backend after login.  Who owns what, when it is saved and how the
  calibration snapshot reaches the files: [profiles-and-calibration.md](profiles-and-calibration.md).
* `auth.login` returns a random session token; every request except
  `server.hello`/`auth.login` requires it.  Sessions identify the operator.
* **Lease:** one setup lease guards every mutation (attribute writes, mutating
  commands, operations, connect/disconnect, manual controls in
  Samba_controls).  It is held by one session, renewed by heartbeats, expires
  after `lease_ttl_s` (default 15 s).  On expiry or release with work
  outstanding the backend cancels operations, runs the configured cleanup and
  only then frees the lease (`LEASE_RECONCILING` until done).  Reads and cached
  monitoring do not need the lease; monitoring never polls an instrument that
  an operation is using — it serves the operation's latest values instead.
* **Devices of a run:** `run.begin(required_devices=...)` declares what a
  measurement uses.  While it runs, only those devices are locked (commands
  refused with `BUSY` except `stop`, reads served from the run's values, no
  monitor polling).  Undeclared devices stay usable by the lease holder — e.g.
  the stage during a calibration or time scan without stage positioning, so
  the sample can be focused while the signal is recorded.
* User switching while a measurement runs is refused by the backend
  (`auth.logout` with the lease held by a running operation → `BUSY`).
* These accounts organise access and attribution on a shared Windows login; they
  do not protect files from an administrator or other processes of the same
  Windows user.

### 3.5 Acquisition semantics

* **Point identity:** every DAQ acquisition gets a monotonically increasing
  `acq_id`; `measure_point` returns it together with the coil value actually in
  the buffer and timestamps; the engine rejects a point whose `acq_id` is not
  newer than the previous one or whose request token does not match.
* **Completion:** DAQ — `wait_until_done` with `acq_time + settle + 10 s`;
  Hall — integration time then one read; lock-in — one `XY.` transaction after
  the settle window; stages — poll `is_moving`/position-reached (AP 8) with the configured
  `move_timeout_s` bound, followed by a position readback that is recorded.
* **Retries:** a timed-out move is never repeated blindly.  The backend reads
  the position back, reports `TIMEOUT` with the readback, and the engine pauses
  the run (MiniMoke's auto-pause behaviour) for the operator to decide.
  Relative moves are only offered as manual jogs, never inside scans.
* **Pause** holds after the current point with field, outputs and position
  unchanged (MiniMoke behaviour); pacing resumes from the pause.
* **Abort** stops at the next point boundary (moves are stopped with the
  controller's stop command), then runs cleanup; no move back to the start.
* **Lock-in:** measurements never configure it -- only "Apply now" (Hardware
  box) or Samba_controls does; a run reads its settings before it starts (the
  lock-in B-sweep settles for the lock-in's real time constant), records them
  (`lockin_settings_json`) and warns when they differ from the Hardware box.
* **Coil after a run** (`cleanup.coil_after_run`, default `keep`): the coil
  keeps running -- a field sweep returns to the value it started from, other
  measurements keep their coil command; `zero` restores MiniMoke's 0 V.
* **Cleanup at run end** (end, abort, failure): DAQ tasks stopped, stages
  stopped where they are; the coil as above; the lock-in is never switched off
  (runs do not drive it).
* **When control ends:** releasing it, the holder chooses whether a running
  coil / lock-in oscillator stays on (Samba_mini and Samba_controls ask);
  otherwise -- and always when the client is lost (lease expiry), on a forced
  release and when the service stops -- coil → 0 V, `OA. 0`, stages stopped.
  Zeroing is instant (as MiniMoke); an optional `coil.ramp_rate_V_per_s` exists
  but is off by default until verified on the rig.
* **Backend failure / lost client:** the client marks the file `interrupted`
  and reports that the hardware state could not be verified; the backend's
  lease watchdog performs cleanup when a client disappears.

### 3.6 Data

HDF5 follows Samba's layout (`/data` datasets with `role`/`label`/`unit`;
measured channels are saved under the names given in Samba_controls' Channels
tab, as Samba saves sensors under their labels, each with its fixed `channel`
id, `device`, physical `line` and plot `y_axis` as attributes -- only the
channels ticked in the sensor rows are measured and saved;
`/metadata` attributes, root `scan_status`, `scan_type`), plus Samba_mini
provenance: user, sample, operator, `mode` (`real`/`simulation`), workflow
parameters (JSON), machine configuration and calibration snapshot, device
identities, software revision.  Arrays are pre-allocated with NaN and written
per point; flush every 10 points, at every sweep/row end and at finalisation.
Raw per-sweep data are kept; the averaged B-sweep loop is in `/data/averaged`.
`scan_status` is `running` until finalisation and then `completed`, `aborted`,
`failed` or `interrupted`; a file that still says `running` after a crash is
recognisable as partial.  CSV export reproduces MiniMoke's columns (with fixed
units).

## Review fixes: control boundaries and acquisition results

A run now receives a private `run_id` capability from `run.begin`. The scan
engine uses `client.for_run(run_id)`; the shared GUI client remains a manual
client. The service reserves physical instruments throughout the run, including
between operations and during synchronous spatial measurements. Logical magnet
writes reserve the DAQ, and MOKE commands resolve their physical dependencies.
Manual commands to unused devices remain available (including stage jogging
during calibration time scans). A scoped client cannot command instruments the
run did not reserve. Stops remain available to the lease holder.

A mutating command, and `run.begin`, is refused with `BUSY` (details
`inflight: true`) while another command on the same instrument (any, for
`run.begin`) is still finishing. The engine treats that as transient at the
start: it waits up to 30 s for the command (e.g. a jog clicked just before
Start), abortably. An abort sends the stage stop only once the run has begun,
the engine checks for an abort before every stage move, and a service error
that arrives after the operator's abort ends the run as *aborted*, not
*failed* (the error is logged).

Lease reconciliation releases ownership only when operations have stopped and
cleanup reports success. Administrator force-release retries reconciliation;
it does not bypass a failed or still-running cleanup. Each lease and run has a
cancellation generation so queued work cannot outlive its owner. Request
budgets start at server receipt, include the RPC executor queue, and propagate
through logical devices to physical workers. Work that has not started is
dropped after expiry. A blocking vendor call already in progress can still
finish; its outcome remains unknown after a timeout until cleanup/readback.

Operation consumers drain all result pages before acting on a final state,
including failure and abort. Lock-in field sweeps configure the instrument
before calculating timing, use the applied time constant, and save both the
requested parameters and applied settings/timing in the data file.

Windows CI runs simulation, fake-driver, process and offscreen GUI tests.
These checks do not validate actual NI, Hall-probe or stage hardware.

The run capability changes the RPC contract to `samba-mini/2`. Restart both
applications from the same checkout; mixed old/new clients are refused before
control begins. Existing account and measurement files keep their schemas.
