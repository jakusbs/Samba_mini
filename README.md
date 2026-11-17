# Samba_mini

Windows measurement software for the MiniMOKE setup, built on the structure and
look of [Samba](https://github.com/jakusbs/Samba) and the hardware and
workflows of [MiniMoke](https://github.com/jakusbs/MiniMoke).

| Application | Responsibility |
|---|---|
| **Samba_controls** | PyQt6 hardware-management window.  Starts (or attaches to) the **device service**, a separate background process that owns every instrument session; shows device state, readbacks, configuration and errors; connect/reconnect and manual controls; user accounts; MiniMoke settings import. |
| **Samba_mini** | PyQt6 measurement window in Samba's layout.  Runs the MiniMoke workflows through the service, plots live data, saves HDF5 (Samba layout), optional CSV and a lab-notebook row. |

## Status (2026-11-17)

**Implemented and tested in simulation and against fake vendor libraries.
Not yet validated on the real instruments** — see
[remaining hardware checks](docs/progress.md#remaining-hardware-checks).

* Device service: local ZeroMQ RPC (loopback only, versioned JSON protocol,
  request ids, structured errors, bounded timeouts, operation handles),
  TANGO-inspired device model, one worker thread per instrument, Argon2id
  local accounts, per-user preferences and presets, versioned λ/2
  calibration records (see [profiles and calibration](docs/profiles-and-calibration.md)),
  a setup lease with
  heartbeat expiry and cleanup-before-release.
* Drivers re-implemented from MiniMoke: NI-DAQ (`nidaqmx`), Ametek 7270
  (PyVISA, USB or Ethernet), Magnet-Physik Hall probe (pythonnet), Thorlabs
  Kinesis (pylablib), Trinamic TMCM-3212 (pytrinamic); plus an explicit,
  labelled simulation mode.
* Workflows: DC B-sweep, lock-in B-sweep, X-, Y- and XY-sweeps, time trace,
  with start / pause / resume / abort and MiniMoke's auto-pause-and-retry on
  hardware errors.
* Intentional correctness fixes relative to MiniMoke are listed in
  [docs/architecture.md §1.4](docs/architecture.md) (most importantly: the
  coil step and its settling are now hardware-timed, so lock-in B-sweep
  readings belong to the current field point; MiniMoke read them one point
  late).

## Quick start

1. Run the installer (the Windows counterpart of Samba's `install.sh`): it
   creates the Python 3.11 environment **SambaMini** next to MiniMoke's `MOKE`
   environment and puts **Samba_mini** and **Samba_controls** shortcuts on the
   Desktop and in the Start menu.

   ```bash
   install.bat
   ```

2. Start **Samba_controls** (desktop icon) — it starts the device service on
   the real instruments.  The first start asks you to create the administrator
   account and, while this PC has no machine configuration, offers to import
   the instrument settings from MiniMoke.  (Simulated instruments are for
   development only, from the command line:
   `scripts\service_console.bat --mode simulation`.)

3. Start **Samba_mini** (desktop icon), log in, pick a measurement in the
   *Trajectory* tab and press Start.

Without shortcuts: `scripts\Samba_controls.bat`, `scripts\Samba_mini.bat`.

The MiniMoke import is offered automatically while there is no configuration,
or later by an administrator in Samba_controls → *Machine configuration* →
*Import MiniMoke settings…* (then restart the service).  Close MiniMoke and
the Thorlabs Kinesis GUI first: only one program can own the DAQ, the 7270's
single Ethernet slot and the stage controllers.  How Samba_controls works and
how to add an instrument: [docs/adding-a-device.md](docs/adding-a-device.md).  Details, vendor prerequisites, troubleshooting and
recovery: [docs/installation.md](docs/installation.md).

## Checks that passed

Automated (`scripts\run_tests.bat`, 157 tests, ~270 s, no hardware touched):

| Area | What is verified |
|---|---|
| Service (16) | only the devices a run declares are locked (the stage stays movable during a time scan, not during a run that uses it); bootstrap/login, unauthenticated and lease-less requests rejected, backend limit checks, competing control rejected, timed point, field-sweep operation, pause/resume/abort, unavailable device → no fake data, lease expiry → abort + cleanup, stage moves, read-only diagnostics mode refuses control/writes/cleanup, manual connect/disconnect results logged with who asked |
| Engine (14) | every workflow end to end through real sockets with HDF5 content/units/provenance checks; an XY map is measured column by column, Y from negative to positive in every column (no zigzag, also with From y > To y), the order recorded in the file; Samba's metadata fields and `/data/calibration` stored under Samba's keys (all-zero calibration not written); lock-in reading belongs to the current field point; Hall-probe speed validation as in MiniMoke; pause/resume/abort; auto-pause on a hardware fault then retry; refusal with a missing device; lease loss → `interrupted` |
| Drivers (17) | a missing or late 7270 Ethernet status chunk no longer stalls a read for the 2 s timeout (regression of a real freeze); an overloaded 7270 (status pair without NUL) no longer garbles the next reply, a stray byte re-syncs instead of faulting the lock-in (regression of a real stuck setup); an overload flickering at the edge of the range is logged once per 5 min with a count, not at every flip; real driver classes against fake `nidaqmx`/`pyvisa`/`pylablib`/`pytrinamic`/`clr`: channel order, PFI0 trigger, settle-sample discard, stale-acquisition rejection, 7270 socket framing, set acknowledgements, range rounding, reconnect, port 50001 fallback, stage conversions and signs, motor parameters, bounded waits, Hall filter and bounded zeroing, no connect-time writes in read-only mode, downloaded (Mark-of-the-Web) DLL refused with a clear message |
| Core (16) | Samba's λ/2 staircase fit reads Samba_mini time scans; protocol safety (NaN/arrays, no object dtypes, version check, no arbitrary methods), late writes after timeout/abort never reach the driver, config migration with backup and malformed-file preservation, MiniMoke import leaves MiniMoke untouched and finds MiniMoke folders newest first, Argon2id hashes and profile persistence, logout/manual control blocked during a run, coordinated readbacks of the run's devices, crash-interrupted HDF5 file readable and marked partial, disk-error auto-pause without data loss, GUI processes never load drivers/PyQt5, core and service are Qt-free |
| Recovery (4) | driver hang → bounded timeout → auto-pause → retry; device reconnect mid-run; service lost mid-run → `interrupted`; preferences/presets survive a service restart |
| Control/acquisition review (22, PR #3) | the lease is freed only after a verified cleanup and a forced release cannot skip one in progress; a run locks logical aliases between operations and leaves the unused stage available; RPC deadlines and executor queue time drop late mutations; an old queued write or request is cancelled when its owner or lease generation ends; the engine drains every result page before the final state; lock-in settling uses the lock-in's own (read) time constant; run.begin waits for a finishing command; aborts before or right after Start end as aborted (no stage stop sent) |
| Coil and lock-in between runs (4) | measurements never configure or switch off the lock-in (its settings are read and recorded, differences to the Hardware box reported); the coil keeps running after a measurement (field sweeps return to their start value; `coil_after_run = "zero"` restores 0 V); releasing control keeps the coil / oscillator only when the holder chose so, a lost client always switches them off; Samba_mini asks before releasing with the coil on (Cancel keeps control), shows the coil in the status bar, and its start message lists only what the run does |
| Settle, calibration box, units (7) | one Settle after every move and coil change (old configurations take the longer of their two waits, the estimate counts it), once before a time scan's first sample; the calibration scan keeps Points / Int / Settle, takes samples back to back and measures with the coil as it is; time scans take their samples back to back (no interval; old configurations keep their number of samples); the lock-in sensitivity is a list of the 7270's ranges in mV |
| Stuck control (4) | an administrator's forced release reconnects a faulted lock-in and frees the setup, frees it even when a device stays unreachable (cleanup reported as unconfirmed; a plain release still waits); the client records the service instance at login, so a service restart before the login no longer warns "restarted" every 2 s; Samba_mini stays responsive while no service runs (its service check runs in the background; it froze the window ~1.5 s every 2 s) |
| Channels (6) | channel names validated (unique, not a name the files use themselves) and saved like Samba (`/data/<name>`, Samba's `_hdf5_key`); the service lists names, lines and read/read-write access; the DAQ returns no std values; a run saves only the ticked channels with their id, device, unit, line and plot axis as attributes and never queries an unticked Hall probe or lock-in (a disabled Hall probe is no obstacle); B-sweep means saved per ticked channel; lock-in channels skipped (with a warning) where the measurement has no lock-in; nothing ticked is refused; CSV export and BD fit find channels by id ("DC" may be another channel); Samba_controls' Channels tab renames (administrators, clashes refused) and Samba_mini shows and saves under the new names; sensor rows are saved with the configuration; the calibration scan always records the BD channel |
| Data folders, device settings, start-up lights (8) | each account measures into its own folder (`Documents\SambaMini Data\<user>`, created at login; the old shared default becomes the account's own; a chosen folder is kept) and the Data Browser shows it; instruments connect one after another in `start_order` with INIT → ON per device and start-up progress events; the public device list feeds Samba_controls' lights before login (no values or settings in it); lights go waiting → starting → running / fault and select the device; display names in the device table, selection by device id; device settings (display name, enabled, start order, connection settings, extra Kinesis/Trinamic stages) are saved by administrators only, validated, refused when the file changed meanwhile (edits kept), setup stages cannot be removed, the old file is backed up, other sections are kept, and a restarted service uses them |
| Profiles and calibration (20) | user A → logout → user B with empty preferences sees defaults, A gets A's state back (including the Calibration tab's Y1 / Y2, also after closing Samba_mini), nothing touches an instrument; session expiry resets and gives unsaved changes back to the same account; one data folder setting, failed autosaves keep the edits and retry; saving preferences never touches calibration edits, navigation asks Save/Discard/Cancel; a new-sample calibration reset survives setup switches and a restart; "Load saved" reads the service; calibration files from another setup, sample, mode or an incomplete scan are explained and need an explicit choice; the calibration scan uses its own visible settings and is fitted for review; live θ display and file share one frozen calibration snapshot (id/version in the file); configurations never change the sample or calibration (sample info in legacy ones is ignored without asking and dropped when saved); each setup keeps its own draft and selected configuration; rename/copy/new refuse collisions, failed saves change nothing; late stage and device-info replies after a selection change are ignored and device commands stay bound to their device; replies of an earlier login session are dropped; calibration records are versioned and per account; schema-1 calibrations migrate with a backup and unknown provenance |
| Process / GUI (19) | the start-up splash shows the version and status, stays up for the minimum time, hands over to the window and can be switched off (`SAMBA_MINI_NO_SPLASH=1`), with a plain card if the artwork is missing; the stage can be jogged to focus during a calibration scan (locked during an X-sweep), the move is logged; the calibration plot's time axis follows the data; service as a real process: start, duplicate instance refused, clean token shutdown; example config valid; the Data Browser opens an XY map while it is being measured without closing the window (regression of a real crash), map axes from partly measured set-point grids; an error escaping a button or file handler is logged and shown instead of closing the window; both windows run a measurement offscreen and the GUI event loop stays responsive (< 0.5 s gaps); a run greys only the choice of measurement, setup, account, channels and (in a scanlist) the scanlist settings, while positions, timing, metadata, Hardware values and the configuration list stay editable and a scanlist keeps the sample and parameters it started with; Samba_controls starts only the real instruments and offers the MiniMoke import without a configuration (declined: nothing started); the µm digit jog moves the stage by the clicked digit (down to 10 nm, leading zeros dimmed) and stays disabled until a position is read, the mm Alignment window moves the same stage and is locked with it; every MiniMoke workflow round-trips through Samba's Spatial/Field layout with its parameters unchanged, settings kept per measurement; sensor checkboxes only plot; a scanlist of N scans with ABBA field flip writes Samba's `.txt`; the active BD calibration is kept per setup and reaches `/data/calibration` |

Manual (2026-11-16, simulation): both windows rendered and inspected
(layout, theme, live 1D/2D plots, data browser, device panels); Samba_mini
rendered next to the original Samba (Spatial, Field, time scan, Calibration,
Scanlist and BD Calibration tabs, θ display) to check the arrangement;
`scripts\service_console.bat`, `python -m Samba_mini` and
`python -m Samba_controls` started as real processes and shut down cleanly.
Manual (2026-11-17, simulation): offscreen renders of the BD Calibration,
Calibration and Profile tabs and of the Samba_controls device panel inspected
after the profile/calibration changes.

**Real hardware:** only read-only connection checks so far (2026-11-16, with
the service's enforced `--read-only` mode): NI USB-6259, Ametek 7270 (Ethernet),
the Kinesis stage and the Hall probe connected and answered; the polar stage is
not yet checked.  Nothing has been moved, energised or measured — see
[docs/progress.md](docs/progress.md#hardware-checks-done).

## Repository layout

```
core/               shared library: protocol, device model, client, scan engine,
                    storage, configuration; Samba's theme, plot widgets and data browser
Samba_controls/     management UI; service/ = the device service (only place drivers are imported)
Samba_mini/         measurement UI
config/examples/    sanitised machine configuration
scripts/            launchers, environment setup, test runner
tests/              automated tests (simulation and fake drivers)
docs/               architecture, implementation plan, installation, progress
```

Machine configuration, accounts, logs and state live in
`%LOCALAPPDATA%\SambaMini` (never in the repository); measurement data go to
each user's data folder.  The brief is in
[Samba_mini_Claude_prompt.md](Samba_mini_Claude_prompt.md).
