# Installation, operation and recovery

## 1. Prerequisites

### Vendor runtimes (installed outside Python, already present on the MiniMOKE PC)

| Instrument | Runtime | Notes |
|---|---|---|
| NI-DAQ | NI-DAQmx driver (provides `nicaiu.dll`); NI MAX to check the device name (`Dev1`) | |
| Ametek 7270 | NI-VISA (provides `visa64.dll`) | Ethernet (`TCPIP0::<ip>::50000::SOCKET`) or USB (`USB0::0x0A2D::0x001B::<serial>::RAW`); the 7270 accepts **one** Ethernet client |
| Hall probe | Magnet-Physik USB Teslameter driver + the **64-bit** `MagnetPhysik.Usb.dll` from MiniMoke's `libs` folder | The DLL is not redistributed in this repository; point `[hall] dll_path` at it |
| Longitudinal stage | Thorlabs Kinesis / FTDI driver | Close the Kinesis GUI: it holds the motors |
| Polar stage | Trinamic TMCM-3212 USB (TMCL) | Close TMCL-IDE |

### Installer (counterpart of Samba's `install.sh`)

Double-click **`install.bat`** in the repository (or run
`powershell -ExecutionPolicy Bypass -File scripts\install.ps1`).  It finds
conda, creates/updates the **SambaMini** environment, checks the imports
without opening any instrument, records the interpreter in `.install_config`
(used by the `scripts\*.bat` launchers) and creates **Desktop** and **Start
menu** shortcuts *Samba_mini* and *Samba_controls* (icons in `assets\`,
derived from Samba's artwork with `scripts\make_icons.py`), plus a Start-menu
*service console (diagnostics)* entry.  Options: `-SkipEnv` (shortcuts only),
`-NoHardware`, `-NoDesktop`, `-Uninstall` (removes shortcuts and
`.install_config`; environment, configuration, accounts and data are kept).
The shortcuts start `pythonw.exe -m Samba_mini` / `-m Samba_controls` directly
(no console window); each app has its own taskbar identity and icon.
While an app loads, a splash screen in Samba's style is shown (at least 2 s;
artwork in `assets\*_splash.png`, built from Samba's `samba_splash.svg` with
`scripts\make_splash.py`).  Set `SAMBA_MINI_NO_SPLASH=1` to start without it.

### Python environments on the MiniMOKE PC (inventory 2026-11-16)

| Interpreter | Python | Used by | Notes |
|---|---|---|---|
| `%USERPROFILE%\miniconda3` (base) | 3.10.19 | first `python` on PATH | mixed: PyQt6 6.7, PyMeasure 0.14, drivers, PyInstaller; the frozen `Desktop\MiniMOKE\MiniMOKE.exe` (2023, bundles python310) was probably built here |
| `miniconda3\envs\MOKE` | 3.11.15 | **MiniMoke** (all `scripts\*.bat` do `conda activate MOKE`; *Spyder (MOKE)* shortcut) | PyQt5, PyMeasure 0.15, drivers, zhinst |
| `miniconda3\envs\MOKE_mfli` | 3.11.15 | MiniMoke MFLI variant (`Desktop\MiniMoke-main_mlfi`; *Spyder (MOKE_mfli)*) | same package set as MOKE |
| `miniconda3\envs\SambaMini` | 3.11.18 | **Samba_mini / Samba_controls** (created by this project) | PyQt6 6.10, no PyQt5, drivers pinned to MOKE's versions |
| `C:\ProgramData\miniconda3` | 3.13.11 | *Spyder* (all-users) shortcut | second, machine-wide Miniconda; no instrument drivers |
| `AppData\Local\spyder-6`, `C:\ProgramData\spyder-6` (each base + `spyder-runtime`) | 3.11 / 3.12 | standalone Spyder 6 installers | IDE runtimes only |
| `AppData\Local\Microsoft\WindowsApps\python.exe` | — | — | Microsoft Store alias (second `python` on PATH) |

Because the first `python` on PATH is the miniconda **base** environment,
nothing in Samba_mini relies on PATH: the shortcuts, launchers and the service
child process always use the SambaMini interpreter explicitly.  The installer
only adds/updates SambaMini and never changes MOKE, MOKE_mfli or base.

### Python environment (details)

`scripts\setup_env.ps1` creates the conda environment **SambaMini** (Python
3.11) next to MiniMoke's `MOKE` environment without touching it:

* `requirements.txt` — GUI, client and service core (PyQt6, matplotlib, h5py, pyzmq, argon2-cffi, tomli-w)
* `requirements-hw.txt` — drivers in the versions MiniMoke runs with
* `pylablib==1.4.5` is installed with `--no-deps` because it declares PyQt5,
  which must not share the environment with PyQt6.

Python 3.11 was chosen because it is the version on which the MOKE
environment proves nidaqmx 1.4.1, PyVISA 1.16.2, pythonnet 3.0.5 (.NET
Framework DLL), pylablib 1.4.5 and pytrinamic 0.2.23 with this hardware.

## 2. First start

1. `scripts\Samba_controls.bat` starts the service and asks to create the
   **first administrator** (only possible while no account exists).
2. Samba_controls always starts the service on the real instruments.  While
   there is no `machine.toml` it offers the MiniMoke import (section 3)
   instead of starting with guessed addresses.  Simulated instruments are for
   development only: `scripts\service_console.bat --mode simulation` (every
   reading is then labelled SIMULATED and file names end in `_SIM`).
3. Add operators in Samba_controls → *Users*.
4. `scripts\Samba_mini.bat` — log in; set your data folder and defaults in
   the *Profile* tab.

## 3. Switching to the real setup

1. Close MiniMoke, Kinesis and TMCL-IDE.
2. First time (no `machine.toml` yet): press *Start service*.  Samba_controls
   offers *Import from MiniMoke…*, lists the
   MiniMoke folders it finds in Documents and on the Desktop (most recently
   used first), shows the values and writes them with mode *real*, then starts
   the service.  Like creating the first administrator, this bootstrap step
   needs no login and is only offered while no configuration file exists; the
   procedure presets are saved for whoever logs in next in that window.
   Later changes: Samba_controls → *Machine configuration* → *Import MiniMoke
   settings…* (administrator).  Choose the MiniMoke folder in use (e.g.
   `Documents\Minimoke-bugfixed`); the configuration is set to the real
   instruments.  The import
   reads `configs\*.ini` and the DLL location; it never writes there.  It
   writes `%LOCALAPPDATA%\SambaMini\config\machine.toml` (a backup of any
   previous file goes to `config\backups`), `state\stage_offsets.json`, and
   saves MiniMoke's last procedure settings as presets in your account.
   Command line alternative:

   ```bash
   python -m core.config.minimoke_import --from "C:\Users\<user>\Documents\Minimoke-bugfixed" --write --mode real
   ```

3. Review `machine.toml` (see `config\examples\machine.example.toml`):
   coil limits, optional stage `limits_mm`, cleanup policy.
4. Optional first check without touching outputs:
   `scripts\service_console.bat --mode real --read-only` connects and reads only
   (no control, no writes, no cleanup writes, no connect-time configuration
   writes; the polar stage stays disconnected because its connect writes motor
   currents).  Stop it with Ctrl+C.
5. Stop and start the service in Samba_controls.  Devices that cannot be
   opened are shown as FAULT or DISABLED with the reason; the windows still
   open, but measurements that need them are refused.

## 4. Operation rules

* **Control (lease):** one session controls the setup.  Samba_mini takes it
  when a run starts; manual controls in either window also need it
  (*Take control*).  Reads and monitoring never need it.
* **Heartbeat:** a client that stops answering for `lease_ttl_s` (15 s) loses
  control; the service aborts its operation and runs the cleanup before anyone
  else can take control.
* **Lock-in:** set it with *Apply now* (Hardware box) or in Samba_controls;
  measurements read it as it is set and never change or switch it off.  The
  sensitivity is one of the 7270's ranges, picked from a list in mV.
* **Settle** (Timing box): the wait before measuring -- after every stage move
  and coil change in spatial scans, once before the first sample of a time
  scan.  The Calibration tab's scan has Points, Int and Settle; it takes its
  samples back to back with the coil and lock-in as they are.
* **Stage positioning** (Calibration tab): the digit jog is in µm (10 mm … 10
  nm); *Alignment…* opens the same stage in mm (10 mm … 1 µm) for coarse moves.
* **Coil:** keeps running between measurements (a field sweep returns to the
  value it started from); *Zero outputs* switches it off.  The status bar shows
  "Coil: … V ON" while it is on.  `[cleanup] coil_after_run = "zero"` in
  machine.toml restores MiniMoke's 0 V after every measurement.
* **Releasing control / closing** with the coil or the lock-in oscillator on
  asks whether to leave them on (with nobody in control) or switch them off.
* **Cleanup when control is lost** (client gone, forced release) and at
  service stop: coil command 0 V, lock-in oscillator 0 V (`OA. 0`), stages
  stopped where they are.  Nothing is ramped (as MiniMoke).
* **Pause** holds after the current point with field, outputs and position
  unchanged.  A hardware error or a failed file write pauses automatically;
  *Resume* retries the same point.
* **Abort** stops at the next point (a running stage move is stopped) and
  keeps the data measured so far.
* Users cannot log out or switch while a measurement runs.
* What is saved automatically (preferences, per-setup draft and experiment) and what
  only on request (configurations, λ/2 calibrations): [profiles-and-calibration.md](profiles-and-calibration.md).

## 5. Data

Every account has its own data folder, `Documents\SambaMini Data\<user name>`,
created at login and shown in the Data Browser (a user can choose another one in
the Profile tab; *Own folder* goes back).

`<data folder>\<YYYYMMDD>\<HHMMSS>_<TYPE>_<sample>_<config>[_SIM].h5`, Samba
layout (opens in Samba's data browser).  Only the channels ticked in Samba_mini's
sensor rows are measured and saved, each under its name from Samba_controls'
*Channels* tab (e.g. `/data/BD`, `/data/Focus_line`) with its channel id as an
attribute; files written before channel names keep `/data/dc_signal` etc.  No
standard deviations are recorded.  `scan_status` is `running` until the
run ends, then `completed`, `aborted`, `failed` or `interrupted`.

Recovery guarantees: points are written as they arrive; the file is flushed
every 10 points, at the end of each sweep/row and at the end.  After a crash
the data up to the last flush are readable and the file still says `running`.
A power loss can lose data the OS had not written yet, and a crash inside an
HDF5 write can leave a file that h5py refuses to open (`h5clear -s` may
recover it).  A failed write pauses the run with the points kept in memory.

CSV for existing scripts: tick *Also export CSV*, or
`python -m core.storage.csv_export <file.h5>`.

## 6. Troubleshooting

| Symptom | Cause / action |
|---|---|
| Samba_mini shows *SERVICE OFFLINE* | Start Samba_controls (it starts the service). |
| Service exits with code 3 | Another service instance is running (see `%LOCALAPPDATA%\SambaMini\state\service.pid`). |
| Service exits with code 4 | Ports 47811/47812 in use; change `[backend]` ports. |
| Configuration status *invalid* | `machine.toml` could not be parsed or failed validation; a copy is kept as `machine.toml.bad-<time>`; all hardware disabled until fixed. The problems are listed in Samba_controls → *Machine configuration*. |
| `lockin` FAULT "not reachable" | 7270 off, cable/network, or another program (MiniMoke) holds its Ethernet slot. Port 50001 is tried automatically. Power-cycle the 7270 if a crashed program left the slot occupied. |
| `hall` DISABLED "DLL not found" | Set `[hall] dll_path` to the 64-bit `MagnetPhysik.Usb.dll`. |
| `hall` DISABLED "marked as downloaded" | The DLL copy came from a downloaded zip (Mark of the Web) and .NET will not load it. Use an unmarked copy (on the MiniMOKE PC: `Documents\minimoke\libs`) or unblock the file after checking where it came from. |
| `hall` cannot connect | Close Magnet-Physik's *USB Teslameter* application and MiniMoke; only one program can use the probe. |
| `stage_longitudinal` FAULT | Close the Kinesis GUI; check serial numbers in `machine.toml`. |
| `daq` FAULT | NI MAX: device name must match `[daq] device`; MiniMoke must be closed. |
| "Controlled by …" | Another user holds control; ask them to release it, or an administrator uses *Force release…* (stops their run and cleans up). |
| Lease stays *stopping previous owner's work* | An operation did not stop within `reconcile_wait_s`; check the setup, then an administrator uses *Force release…* (the state is then not verified by software). |
| Warning "outputs could not be confirmed safe" | Cleanup failed or the service was unreachable: check coil current, lock-in output and stages by hand. |
| BD Calibration / R / C say "the running device service is older" | Samba_mini was updated but the service still runs the previous version. When no measurement runs: Samba_controls → *Stop service*, then *Start service* (this also migrates the accounts database; see [profiles-and-calibration.md](profiles-and-calibration.md#5-migration-accounts-database-schema-1--2)). |
| Status bar `Prefs: ⚠ NOT SAVED` | The last automatic save failed (service unreachable, session expired). Your changes are kept; the Profile tab shows the reason and *Retry*. After a session expiry, log in with the same account to get them back. |

Logs: `%LOCALAPPDATA%\SambaMini\logs\service.log` (rotating).

## 7. Shutdown behaviour

* *Stop service* (or closing Samba_controls with *Yes*) asks the service to
  shut down: operations are aborted, cleanup runs, devices are disconnected
  (the 7270 socket is closed so its slot is freed), then the process exits.
* If it does not exit, Samba_controls offers to terminate it; then no cleanup
  runs and outputs keep their last values — check the setup.
* Closing Samba_mini during a run asks to abort it and waits for the cleanup.
