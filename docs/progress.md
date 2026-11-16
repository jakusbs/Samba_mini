# Progress

Working branch: `feature/samba-mini-first-release` (from `main` at `960feb7`),
pushed to `jakusbs/Samba_mini`.  Not merged.

## Done (2026-11-16)

| Phase | State |
|---|---|
| 1 Audit and design | done — `docs/architecture.md`, `docs/implementation-plan.md` |
| 2 Service core (simulation) | done — protocol, device model, workers, auth, lease, operations, simulated instruments, client |
| 3 Engine and storage | done — all six workflows, HDF5 (Samba layout), CSV export, lab notebook |
| 4 Samba_mini UI | done — Samba layout/theme, live 1D/2D plots, data browser, configurations, instruments, profile |
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

## Tests

`scripts\run_tests.bat` → **59 passed** (≈60 s) on the measurement PC,
simulation and fake drivers only.  Per area: service 14, engine 11, drivers
12, core 14, recovery 4, process 2, GUI 2.  See the README table for what each
covers.

Manual checks done: offscreen renders of both windows inspected (Trajectory,
Instruments, Data Browser, 2D map, device readbacks, manual controls); the
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

## Remaining hardware checks

Each needs the instruments; ask the lab before anything that moves or
energises.  Close MiniMoke, Kinesis and TMCL-IDE first.

1. **Polar stage connection:** its connect writes MiniMoke's motor-current and
   StealthChop parameters — treat as energising; not possible read-only.
   Never run it together with the USB Teslameter application holding the
   Hall probe (same for MiniMoke).
2. **Hall probe zeroing and writes:** reads work on the worker thread without
   COM initialisation (checked); setting Speed/Filter and zeroing (writes) are
   still untested.  The probe was found with Filter 0 (off) — Samba_mini sets
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
* The data browser's Kerr (λ/2) display needs a BD calibration, which
  MiniMoke workflows do not produce; the pill stays disabled.
* Machine configuration is edited as a file (Samba_controls shows it and
  imports MiniMoke settings, but has no form editor).

## Next step

With the lab's go-ahead, remaining items 1–9 (these write settings, energise
outputs or move stages).  When creating the real `machine.toml`, make sure
`[hall] dll_path` points to `Documents\minimoke\libs\MagnetPhysik.Usb.dll`
(the importer now chooses it automatically).
