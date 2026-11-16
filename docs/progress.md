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

## Tests

`scripts\run_tests.bat` → **69 passed** (≈70 s) on the measurement PC,
simulation and fake drivers only.  Per area: service 15, engine 13, drivers
12, core 16, recovery 4, process 2, GUI 7.  See the README table for what each
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
