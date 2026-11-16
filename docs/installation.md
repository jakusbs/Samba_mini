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

### Python environment

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
2. Without `machine.toml` the service runs in **simulation** mode: every
   reading is labelled SIMULATED and file names end in `_SIM`.
3. Add operators in Samba_controls → *Users*.
4. `scripts\Samba_mini.bat` — log in; set your data folder and defaults in
   the *Profile* tab.

## 3. Switching to the real setup

1. Close MiniMoke, Kinesis and TMCL-IDE.
2. Samba_controls → *Machine configuration* → *Import MiniMoke settings…*
   (administrator).  Choose the MiniMoke folder in use (e.g.
   `Documents\Minimoke-bugfixed`) and *Mode after import: real*.  The import
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
* **Cleanup** (end of run, abort, lost client, service stop): coil command
  0 V, lock-in oscillator 0 V (`OA. 0`), stages stopped where they are.  This
  is what MiniMoke did; nothing is ramped.
* **Pause** holds after the current point with field, outputs and position
  unchanged.  A hardware error or a failed file write pauses automatically;
  *Resume* retries the same point.
* **Abort** stops at the next point (a running stage move is stopped) and
  keeps the data measured so far.
* Users cannot log out or switch while a measurement runs.

## 5. Data

`<data folder>\<YYYYMMDD>\<HHMMSS>_<TYPE>_<sample>_<config>[_SIM].h5`, Samba
layout (opens in Samba's data browser).  `scan_status` is `running` until the
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
| `stage_longitudinal` FAULT | Close the Kinesis GUI; check serial numbers in `machine.toml`. |
| `daq` FAULT | NI MAX: device name must match `[daq] device`; MiniMoke must be closed. |
| "Controlled by …" | Another user holds control; ask them to release it, or an administrator uses *Force release…* (stops their run and cleans up). |
| Lease stays *stopping previous owner's work* | An operation did not stop within `reconcile_wait_s`; check the setup, then an administrator uses *Force release…* (the state is then not verified by software). |
| Warning "outputs could not be confirmed safe" | Cleanup failed or the service was unreachable: check coil current, lock-in output and stages by hand. |

Logs: `%LOCALAPPDATA%\SambaMini\logs\service.log` (rotating).

## 7. Shutdown behaviour

* *Stop service* (or closing Samba_controls with *Yes*) asks the service to
  shut down: operations are aborted, cleanup runs, devices are disconnected
  (the 7270 socket is closed so its slot is freed), then the process exits.
* If it does not exit, Samba_controls offers to terminate it; then no cleanup
  runs and outputs keep their last values — check the setup.
* Closing Samba_mini during a run asks to abort it and waits for the cleanup.
