# Samba_mini

Windows measurement software for the MiniMOKE setup, built on the structure and
look of [Samba](https://github.com/jakusbs/Samba) and the hardware and
workflows of [MiniMoke](https://github.com/jakusbs/MiniMoke).

| Application | Responsibility |
|---|---|
| **Samba_controls** | PyQt6 hardware-management window.  Starts (or attaches to) the **device service**, a separate background process that owns every instrument session; shows device state, readbacks, configuration and errors; connect/reconnect and manual controls; user accounts; MiniMoke settings import. |
| **Samba_mini** | PyQt6 measurement window in Samba's layout.  Runs the MiniMoke workflows through the service, plots live data, saves HDF5 (Samba layout), optional CSV and a lab-notebook row. |

## Status (2026-11-16)

**Implemented and tested in simulation and against fake vendor libraries.
Not yet validated on the real instruments** — see
[remaining hardware checks](docs/progress.md#remaining-hardware-checks).

* Device service: local ZeroMQ RPC (loopback only, versioned JSON protocol,
  request ids, structured errors, bounded timeouts, operation handles),
  TANGO-inspired device model, one worker thread per instrument, Argon2id
  local accounts, per-user preferences and presets, a setup lease with
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

1. Create the environment (Python 3.11, separate from MiniMoke's `MOKE` env):

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1
   ```

2. Start **Samba_controls** — it starts the device service.  With no
   configuration it runs in **simulation** mode.  The first start asks you to
   create the administrator account.

   ```bash
   scripts\Samba_controls.bat
   ```

3. Start **Samba_mini**, log in, pick a workflow in the *Trajectory* tab and
   press Start.

   ```bash
   scripts\Samba_mini.bat
   ```

To drive the real setup, import the MiniMoke settings (Samba_controls →
*Machine configuration* → *Import MiniMoke settings…*, choose mode *real*)
and restart the service.  Close MiniMoke and the Thorlabs Kinesis GUI first:
only one program can own the DAQ, the 7270's single Ethernet slot and the
stage controllers.  Details, vendor prerequisites, troubleshooting and
recovery: [docs/installation.md](docs/installation.md).

## Checks that passed

Automated (`scripts\run_tests.bat`, 55 tests, ~60 s, no hardware touched):

| Area | What is verified |
|---|---|
| Service (13) | bootstrap/login, unauthenticated and lease-less requests rejected, backend limit checks, competing control rejected, timed point, field-sweep operation, pause/resume/abort, unavailable device → no fake data, lease expiry → abort + cleanup, stage moves |
| Engine (11) | every workflow end to end through real sockets with HDF5 content/units/provenance checks; lock-in reading belongs to the current field point; Hall-probe speed validation as in MiniMoke; pause/resume/abort; auto-pause on a hardware fault then retry; refusal with a missing device; lease loss → `interrupted` |
| Drivers (10) | real driver classes against fake `nidaqmx`/`pyvisa`/`pylablib`/`pytrinamic`/`clr`: channel order, PFI0 trigger, settle-sample discard, stale-acquisition rejection, 7270 socket framing, set acknowledgements, range rounding, reconnect, port 50001 fallback, stage conversions and signs, motor parameters, bounded waits, Hall filter and bounded zeroing |
| Core (13) | protocol safety (NaN/arrays, no object dtypes, version check, no arbitrary methods), late writes after timeout/abort never reach the driver, config migration with backup and malformed-file preservation, MiniMoke import leaves MiniMoke untouched, Argon2id hashes and profile persistence, logout/manual control blocked during a run, coordinated readbacks, crash-interrupted HDF5 file readable and marked partial, disk-error auto-pause without data loss, GUI processes never load drivers/PyQt5, core and service are Qt-free |
| Recovery (4) | driver hang → bounded timeout → auto-pause → retry; device reconnect mid-run; service lost mid-run → `interrupted`; preferences/presets survive a service restart |
| Process / GUI (4) | service as a real process: start, duplicate instance refused, clean token shutdown; example config valid; both windows run a measurement offscreen and the GUI event loop stays responsive (< 0.5 s gaps) |

Manual (2026-11-16, simulation): both windows rendered and inspected
(layout, theme, live 1D/2D plots, data browser, instruments, device panels);
`scripts\service_console.bat`, `python -m Samba_mini` and
`python -m Samba_controls` started as real processes and shut down cleanly.

**Real hardware: not tested.**  No instrument was opened, moved or energised
during development.

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
