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
| 8 Hardware validation | **not started — needs the lab's authorisation** |

Environment: conda `SambaMini`, Python 3.11.18, PyQt6 6.10.2, matplotlib
3.11.2, h5py 3.16.0, pyzmq 27.3.0, argon2-cffi 25.1.0, nidaqmx 1.4.1, PyVISA
1.16.2, pythonnet 3.0.5, pylablib 1.4.5 (no PyQt5), pytrinamic 0.2.23.

## Tests

`scripts\run_tests.bat` → **55 passed** (≈60 s) on the measurement PC,
simulation and fake drivers only.  Per area: service 13, engine 11, drivers
10, core 13, recovery 4, process 2, GUI 2.  See the README table for what each
covers.

Manual checks done: offscreen renders of both windows inspected (Trajectory,
Instruments, Data Browser, 2D map, device readbacks, manual controls); the
batch launcher and both module entry points started as real processes against
an isolated temporary home and shut down cleanly; the MiniMoke importer read
`Documents\Minimoke-bugfixed` correctly in a dry run (nothing written there).

## Remaining hardware checks

Each needs the instruments; ask the lab before anything that moves or
energises.  Close MiniMoke, Kinesis and TMCL-IDE first.

1. **Read-only connection** (service in real mode): NI device `Dev1` opens;
   7270 answers `ID`/`VER` over Ethernet and the framing probe reports one
   extra chunk; Hall DLL loads in the SambaMini env and reads a field; Kinesis
   motors open and report positions consistent with the imported offsets.
   Note: connecting the TMCM-3212 writes MiniMoke's motor-current and
   StealthChop parameters (as MiniMoke does at start-up) — treat as energising.
2. **Hall probe threading/COM:** MiniMoke called
   `pythoncom.CoInitializeEx(APARTMENTTHREADED)` at start-up without saying
   why; the service loads the DLL in a worker thread without it (pywin32 is
   not installed).  Verify reads and zeroing; add COM initialisation if needed.
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
* No application icon (Samba's artwork carries ETH/author branding and was
  not copied).
* The data browser's Kerr (λ/2) display needs a BD calibration, which
  MiniMoke workflows do not produce; the pill stays disabled.
* Machine configuration is edited as a file (Samba_controls shows it and
  imports MiniMoke settings, but has no form editor).

## Next step

Phase 8, item 1: with the lab's go-ahead, start the service in real mode with
`scripts\service_console.bat --mode real` after importing the MiniMoke
settings, and record the connection results and identities here.
