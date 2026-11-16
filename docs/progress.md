# Progress

Working branch: `feature/samba-mini-first-release` (from `main` at `960feb7`),
pushed to `jakusbs/Samba_mini`.  Not merged.

## Done (2026-11-16)

| Phase | State |
|---|---|
| 1 Audit and design | done — `docs/architecture.md`, `docs/implementation-plan.md` |
| 2 Service core (simulation) | done — protocol, device model, workers, auth, lease, operations, simulated instruments, client |
| 3 Engine and storage | done — all six workflows, HDF5 (Samba layout), CSV export, lab notebook |
| 4 Samba_mini UI | done — Samba layout/theme down to the tabs (Spatial/Field trajectory, Timing/Metadata/Hardware, Calibration, Scanlist, BD Calibration), live 1D/2D plots, θ display, data browser, configurations, profile |
| 5 Samba_controls UI | done — service lifecycle, device table/detail, manual commands, users, configuration import |
| 6 Real drivers | written and tested against fake vendor libraries; **not run on the instruments** |
| 7 Hardening and documentation | done — recovery tests, launchers, installation/troubleshooting |
| 8 Hardware validation | read-only connection checks done for DAQ, 7270, Kinesis stage and Hall probe; the rest pending |

Environment: conda `SambaMini`, Python 3.11.18, PyQt6 6.10.2, matplotlib
3.11.2, h5py 3.16.0, pyzmq 27.3.0, argon2-cffi 25.1.0, nidaqmx 1.4.1, PyVISA
1.16.2, pythonnet 3.0.5, pylablib 1.4.5 (no PyQt5), pytrinamic 0.2.23.

* 2026-11-16 — Windows installer `install.bat` / `scripts\install.ps1`
  (environment, launcher config, Desktop + Start-menu shortcuts, uninstall);
  tested into temporary folders (install and uninstall), then run on this PC
  with `-SkipEnv`: Desktop shortcuts *Samba_mini* and *Samba_controls* and the
  Start-menu folder *Samba_mini* exist.  Python environments inventoried
  (docs/installation.md).
* 2026-11-16 — Samba_controls offers the MiniMoke import when *real* is
  chosen and no `machine.toml` exists (previously the service started with
  built-in defaults and found only the DAQ); it lists the MiniMoke folders on
  the PC, most recently used first, and saves the procedure presets after the
  next login.  Starting with a different mode than the running service now
  says so.  Manual *Connect* / *Disconnect* / *Reconnect* results (success
  or failure, who asked, the polar stage's actual COM port) now go to
  `service.log`.  On this PC `machine.toml` was created from
  `Documents\Minimoke-bugfixed` (mode real).

* 2026-11-16 — Samba_mini arranged like Samba inside the tabs as well:
  Spatial | Field trajectory (X/Y axis boxes with "Scan enabled", none
  ticked = time scan; Lock-in and DC B-sweep side by side with the Hall
  field monitor), Timing | Metadata | Hardware row (Samba's metadata fields;
  7270 settings per measurement), Calibration plot tab with stage
  positioning, Samba's sensor rows (checkbox plots only), action bar and
  status bar.  Added Samba's Scanlist tab (N scans, field flip AB/ABBA,
  Samba's `.txt`) and BD Calibration tab with the θ display
  (`/data/calibration` in every new file).  MiniMoke workflows, engine and
  file layout unchanged.

* 2026-11-16 — Calibration tab shows a live signal plot (two channels, rolling
  window, readbacks while idle and every measured point during runs) next to
  the stage positioning; spinbox arrows removed (both windows); clearer
  Interval / Repeats explanations; the Samba_controls window no longer offers
  simulation (always the real instruments; simulation stays available from
  the command line for development and tests); `docs/adding-a-device.md`.

* 2026-11-16 — Stage positioning uses Samba's digit jog: a ▲ and ▼ for every
  digit, MiniMoke's odometer steps 10 mm … 0.00001 mm, relative jogs (as
  MiniMoke), Enter in "Go to" for an absolute move, disabled until the
  position is read.  Units checked against MiniMoke's source: scan ranges in
  µm (procedures), stage in mm (motor tabs), steps = mm × mm2steps from the
  MiniMoke configuration (2184533 /mm Kinesis, 51200 /mm Trinamic, Y/Z of the
  polar stage inverted) — identical.

* 2026-11-16 — **Crash fix.**  Opening the Data Browser during an XY map
  closed Samba_mini (19:25, real XY map `192009_SPATIAL_XY_...h5` stopped at
  380/8405 points and stays marked "running").  Cause: Samba_mini stores the
  map set-points per point (`[n_y, n_x]`), Samba's reader expects 1-D axes;
  matplotlib raised, and PyQt6 aborts the whole application on any exception
  escaping a slot — taking the measurement (run inside the window) with it.
  Fixes: the reader turns per-point set-points into axes (also for partly
  measured maps); the browser shows a message instead of failing and never
  leaves the previous file's plot under a new file name; field sweeps open as
  curves, only spatial maps open as maps; both windows log to
  `%LOCALAPPDATA%\\SambaMini\\logs\\samba_mini.log` / `samba_controls.log` and
  show unexpected errors in a message instead of closing.  The HDF5 files
  themselves were fine (all of today's files read back intact).

* 2026-11-16 — Calibration tab measures only on Start (Samba's calibration
  time scan: the tab's own samples / interval / Int, saved as
  `..._calibration.h5` for the BD Fit & Import; no live monitoring).  The
  *Repeats* field is gone: as in Samba, a measurement is repeated with the
  Scanlist tab (one file per scan, Samba's scanlist `.txt`).  MiniMoke's
  `repeat_num` is now always 1 — an X/Y-sweep still measures its line at +B
  and at −B, an XY map once; configurations saved with more repeats run once.
* 2026-11-16 — **"Measurement freezes" fix (7270 over Ethernet).**  The point
  timestamps of today's real runs showed points taking +2.0 s (the 7270 read
  timeout): in the 19:46 calibration time scan points 5, 6, 40 and then 78–80
  in a row; in the X-sweeps every other point or so.  After each reply the
  7270's Ethernet socket sends an extra status chunk; when it did not come,
  the driver (like MiniMoke's) waited the full 2 s timeout for it, silently.
  The driver now waits at most 100 ms for it, counts and logs missing chunks
  (service log: "7270: status chunk missing after a reply"), and discards a
  late chunk before the next command so replies stay aligned.  Why the 7270
  sometimes omits it is not known yet; the log count will show how often.
  **Confirmed on the real setup** (service restarted 20:48:44 with
  `9a1dc53`): calibration time scan 20:49:11, interval 0.2 s — 129 points at
  0.202 s/point, longest gap 0.288 s, no gap over 1 s (the same scan on the
  old service at 20:46:53: two 2.16 s gaps in 80 points).  The service log
  counted 10 missing status chunks within the first ~35 s.
* 2026-11-16 — **Focusing during the calibration scan.**  The service now
  locks only the devices a run declares (`run.begin(required_devices)`):
  commands to them are refused, their reads come from the run, the monitor
  leaves them alone.  The calibration time scan (and any time scan without
  stage positioning) does not use the stage, so the digit jog stays usable
  and its position stays live while the signal is recorded; runs that move
  the stage (X/Y-sweeps, XY maps, "go to x, y first") still lock it.  Tested
  in simulation only.
* 2026-11-16 — After the service restart (23:16:50, `e446689`) three
  calibration scans on the real setup each reported "the stage can be moved
  meanwhile", no move was refused, and the intensity of scan 231726 dipped
  and settled ~4 % lower between 12 and 35 s, consistent with focusing moves.
  Successful moves were not logged then, so manual commands on devices the run
  does not use are now logged (`manual stage_longitudinal.jog(...) by user …
  during a measurement`), as are refusals.  The calibration plot's time axis
  now follows the recorded data instead of spanning the planned duration
  (a 10 000-sample scan squeezed the first minutes into a sliver).

## Tests

`scripts\run_tests.bat` → **79 passed** (≈85 s) on the measurement PC,
simulation and fake drivers only.  Per area: service 16, engine 13, drivers
14, core 16, recovery 4, process 2, GUI 14.  See the README table for what each
covers.

Manual checks done: offscreen renders of both windows inspected (Trajectory,
Data Browser, 2D map, device readbacks, manual controls), and Samba_mini
rendered next to the original Samba (run with its simulated devices in a
throwaway home folder) for the Spatial, Field, time-scan, Calibration,
Scanlist and BD Calibration views; the
batch launcher and both module entry points started as real processes against
an isolated temporary home and shut down cleanly; the MiniMoke importer read
`Documents\Minimoke-bugfixed` correctly in a dry run (nothing written there).

## Hardware checks done

**2026-11-16 14:29 — read-only connection check (authorised by the user).**
Service started with `--read-only` (control, writes, mutating commands and
cleanup writes refused; no connect-time configuration writes) from an
isolated temporary home whose `machine.toml` was imported from
`Documents\Minimoke-bugfixed`.  Hall probe excluded because Magnet-Physik's
*USB Teslameter* application was running; polar stage excluded (its connect
writes motor currents).  MiniMoke, Kinesis GUI and TMCL-IDE were not running.

| Device | Result |
|---|---|
| NI-DAQ | ON — USB-6259 (BNC), serial 21136571, `Dev1`; tasks, channels and PFI0 triggers created; no acquisition, no output written (coil command reported as unknown) |
| Ametek 7270 | ON — `7270` firmware 2.11 at `TCPIP0::192.168.77.2::50000::SOCKET`; framing probe found 1 extra status chunk per response (socket framing, as MiniMoke documented); readback REFMODE 0, OA 0 V, SEN 1 mV, OF 8957 Hz, TC 0.2 s, REFP 0°; `XY.` = −1.375e−4 V, +1.720e−4 V (identical in three reads ~1 s apart: plausible at OA 0 V with the 7270's 4-digit replies; recheck in item 5); session closed at shutdown |
| Kinesis stage | ON — motors 80855672/80855670/80855667 opened; not moving; with the imported offsets x = −0.0250 mm, y = −0.0087 mm, z = 0.0000 mm; backlash setting left unchanged |
| Refusals | `lease.acquire` and `daq.zero_outputs` → FORBIDDEN |
| Shutdown | exit code 0; "read-only mode: shutdown cleanup skipped" |

**2026-11-16 14:42 — read-only Hall probe check (authorised; the user asked
for Magnet-Physik's *USB Teslameter* application to be closed, which was done
with a normal window close).**  Same `--read-only` service, Hall probe only.

* The first attempt failed before touching the probe: .NET refused to load
  `Documents\Minimoke-bugfixed\libs\MagnetPhysik.Usb.dll` (HRESULT
  0x80131515, "network location").  That copy — like `Desktop\MiniMoke-main_mlfi`
  and the downloaded snapshot — carries Windows' Mark of the Web
  (Zone.Identifier); the byte-identical copies in `Documents\minimoke\libs`,
  `Documents\minimoke-upgrade\libs` and `Desktop\MiniMOKE` do not.  This is why
  MiniMoke hard-codes the `Documents\minimoke\libs` path.  The driver now
  reports this case explicitly and the importer prefers an unmarked copy;
  nothing was unblocked.
* With `Documents\minimoke\libs\MagnetPhysik.Usb.dll`: probe ON; Speed 1
  (470 samples/s) and Filter 0 (filter off) were read, not written; five reads
  0.027, 0.028, 0.028, 0.044, 0.045 mT (no coil current; near-zero ambient
  field).  The DLL loaded and read on the service worker thread without
  MiniMoke's `pythoncom.CoInitializeEx` call.  Zeroing was refused (FORBIDDEN).
* The USB Teslameter application was left closed; reopen it if it is needed.

**2026-11-16 16:16 — first real-mode start (the user, from the Samba_controls
desktop icon).**  `machine.toml` imported from `Documents\Minimoke-bugfixed`
(mode real).  Connection results in `service.log`: DAQ, 7270 (1 extra status
chunk per response, as before), Hall probe and Kinesis stage **ON**, with
their connect-time writes done without error (Hall Speed 1 and Filter from
the default 0.5 s integration; Kinesis backlash 0.02 mm).  Polar stage
DEVICE_UNAVAILABLE: its TMCM-3212 was not on USB (Windows listed its earlier
COM5/COM8/COM13 entries as not present).  After the user plugged it in it
appeared on COM13 (found by pytrinamic's port enumeration, no port opened);
the user pressed *Connect* at 16:29:55 and reported **stage_polar ON**
(motor currents, speed, microsteps and StealthChop parameters written as in
MiniMoke; nothing moved).  No measurement, coil output or stage move yet.

## Remaining hardware checks

Each needs the instruments; ask the lab before anything that moves or
energises.  Close MiniMoke, Kinesis and TMCL-IDE first.

1. **Polar stage readback:** connection done (16:29, above); compare the
   position readback with MiniMoke's for the same offsets before any move.
   Never run it together with the USB Teslameter application holding the
   Hall probe (same for MiniMoke).
2. **Hall probe zeroing and writes:** reads work on the worker thread without
   COM initialisation (checked); Speed/Filter writes at connect worked in the
   real-mode start; per-workflow integration changes and zeroing are still
   untested.  The probe was found with Filter 0 (off) — Samba_mini sets
   the integration per workflow, MiniMoke reset it to 0.5 s at the end of runs.
3. **DAQ timing (finding F1 fix):** with the coil connected, confirm the coil
   steps at the trigger and that the settle prefix removes the transient;
   compare a DC loop with MiniMoke on the same sample.  Confirm the AO holds
   the last sample after a finite generation.
4. **Coil amplifier:** measure the current per volt (`coil.amps_per_volt`)
   and the coil time constant; choose `field_settle_s` and confirm the B-sweep
   5 % settle fraction is enough.  Confirm instant zeroing at cleanup is
   acceptable for the amplifier (MiniMoke always did it).
5. **Lock-in:** REFMODE 0 + configure sequence; SEN/TC rounding; `XY.` reads;
   `OA. 0` at cleanup; reconnect via port 50001 after a crashed client.
6. **Hall integration at 2 ms:** `Filter = round(0.002·470) = 1`, but the
   driver documentation in MiniMoke says valid values are 0 or 2–255.  Check
   what the probe does with 1 (MiniMoke has the same behaviour).
7. **Stages:** absolute moves, timeouts, `stop()` mid-move (Kinesis) and TMCL
   `MST` mid-move (TMCM-3212; never used by MiniMoke), Y/Z sign convention on
   the polar stage, software zero vs. physical zero; then set `limits_mm`.
8. **Concurrency:** Samba_controls monitoring while Samba_mini measures — no
   USB/serial collisions; lease loss with real hardware (kill Samba_mini during
   a run → coil 0 V, oscillator 0 V, stage stopped).
9. **Parity run:** one short measurement of each workflow compared with
   MiniMoke on the same sample.

## Known limitations

* Server copies of data files and the server lab notebook (MiniMoke's
  `\\d.ethz.ch\...` archive) are not implemented; data stay local.
* Application icons are derived from Samba's dancer artwork (same lab) with a
  badge per app (`scripts\make_icons.py`); replace `assets\*.ico` to rebrand.
* The θ (µrad) display needs a λ/2 calibration: enter or fit it in the BD
  Calibration tab (Fit & Import expects a time scan from a configuration
  named "calibration" while the plate is stepped through ticks 0–25).
* Scanlist field flip reverses the coil command; the field_T column is the
  scan's median Hall reading.  There is no relay on these setups
  (relay_sign is always +1).
* Machine configuration is edited as a file (Samba_controls shows it and
  imports MiniMoke settings, but has no form editor).

## Next step

With the lab's go-ahead, remaining items 1–9 (these write settings, energise
outputs or move stages).  When creating the real `machine.toml`, make sure
`[hall] dll_path` points to `Documents\minimoke\libs\MagnetPhysik.Usb.dll`
(the importer now chooses it automatically).
