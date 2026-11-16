# Implementation plan

Architecture and audit: [architecture.md](architecture.md).  Status: [progress.md](progress.md).

## 1. Workflow parity (MiniMoke HEAD `6c7c0fdd` → Samba_mini)

Units are written as they are physically known.  MiniMoke labels the coil
command "A", but the code writes that number as a voltage on the DAQ analogue
output `Dev1/ao3`; the amplifier's current-per-volt is not in the source, so
Samba_mini records **coil command in V** and shows a current only when
`coil.amps_per_volt` is configured.

| Workflow | MiniMoke parameters (units as labelled) | Samba_mini parameters | Recorded channels | Behaviour kept | Changes |
|---|---|---|---|---|---|
| **DC B-sweep** (`B-Sweep`) | `b_min`, `b_max`, `b_step` ("A", ±6), `sweep_freq` Hz (0.01–100), `num_sweeps` (1–1000), `x`, `y` µm | same names, coil in V; `position_stage` (default on) | per point: coil command V, Hall field mT (or NaN), balanced-diode DC V, DC std V, intensity V, intensity std V, cycle, branch, point timestamps; averaged loop per branch | n = ⌊span/step⌋+1, forward b_min→b_max then backward without repeated end point; T_point = 1/(f·n_total); 5 % settle, 95 % acquisition at 50 kHz; Hall live only if T_point ≥ 2 ms; forward and backward averaged separately; deadline pacing; initial field + 1 s; stage to (x,y); outputs to 0 at end; lock-in `OA. 0` at end | F1 settle is hardware-timed; F2/F3 latent synthetic-field paths not ported (validation makes them unreachable, as in MiniMoke); Hall read inside the DAQ window instead of after it; F4 labels; F7 tolerance; pause and auto-pause supported (MiniMoke DC sweep had neither) |
| **Lock-in B-sweep** (`B-Sweep LockIn`) | as DC + `volt` V (0–5), `sensi` V, `lockin_freq` Hz, `time_const` s, `phase` deg, `acq_time` s | same | as DC + lock-in X, Y, R (V), θ (deg); averaged DC, X, R | REFMODE 0; OA./SEN/OF./TC/REFP. configuration; settle = 5 τ; T_point = max(5τ+acq, 1/(f·n)); one `XY.` read per point; pause/auto-pause on hardware error with schedule deferral | F1: X/Y now belong to the current field point (MiniMoke read them one point late) |
| **X-sweep** | `volt`, `sensi`, `lockin_freq` (≥173 Hz), `time_const` (0.1–10 s), `phase`, `acq_time`, `settle_time` (0–60 s), `x_min/x_max/x_step` µm, `y` µm, `b` "A", `repeat_num` | same + `field_settle_s` (default 1 s, MiniMoke's initial-field wait) | per point: target and read-back X/Y µm, coil V, Hall mT, X/Y/R/θ, DC, DC std, intensity, intensity std, loop, iteration, timestamps | field sequence [+b, −b] × repeat; positions inner loop; settle after moves; return to first point at normal end, not on abort; auto-pause and retry the same point on hardware error | field reversals settle (F1); position read back after every move is recorded |
| **Y-sweep** | as X with `y_min/y_max/y_step`, fixed `x` | same | same | same | same |
| **XY map** | `x_*`, `y_*`, `b`, `repeat_num` (1–100), lock-in params, `settle_time` | same | same, stored as `[n_y, n_x]` per repeat | serpentine in y per column, fixed field, whole map repeated | — |
| **Time trace** (`Time`) | `b`, `volt`, `sensi`, `lockin_freq`, `time_const`, `phase`, `acq_time`, `x`, `y`, `duration`, `interval` | same | elapsed s, absolute timestamp, coil V, Hall mT, X/Y/R/θ, DC, DC std, intensity, intensity std | n = max(1, int(duration/interval)); pacing by interval; pause gap visible in time axis | runs as a backend operation so pacing does not depend on GUI round trips |
| Live readouts (Live tab) | intensity mV, balanced mV, field mT, Hall zero button | Samba_controls device panels + Samba_mini status | — | live DAQ/Hall polling only when idle; during a run the run's latest values are shown | monitor polling coordinated by the backend |
| Manual stages (motor tabs) | odometer jog 10…0.00001 mm, go-to, set zero (home) | Samba_controls stage panel, Samba_mini "Instruments" tab | — | longitudinal/polar selection; software zero writes offsets | needs the lease; jogs rejected while moving |
| Setup / geometry toggle | Longitudinal/Polar, PMOKE/LMOKE | setup pills, geometry pills | stored in metadata | polar ⇒ PMOKE | — |
| Archive | local `<dir>/<date>/<setup>/<sample>_<proc>_<datetime>.csv`, server copies, lab-notebook CSV | `<data dir>/<YYYYMMDD>/<HHMMSS>_<TYPE>_<sample>_<config>.h5` (Samba), CSV export, lab notebook | — | lab-notebook columns | server copy is a later item (network share not testable here) |

Validation rules kept: Hall minimum 2 ms per point
(`queue_validation_error`), parameter limits above.  Rejected configurations
are reported before anything moves.

## 2. Dependency assessment

| Component | Choice | Reason |
|---|---|---|
| Python | **3.11** (conda env `SambaMini`) | the MOKE env proves NI-DAQmx 1.4.1, PyVISA 1.16.2, pythonnet 3.0.5 (.NET Framework DLL), pylablib 1.4.5 and pytrinamic 0.2.23 on 3.11 with this hardware; PyQt6 6.10 and matplotlib support 3.11.  Samba itself targets 3.13 but is not run here. |
| GUI | PyQt6 6.10, matplotlib (QtAgg) | same stack as Samba; no PyQt5 in the new env |
| IPC | pyzmq 27 | see architecture §3.2 |
| Storage | h5py, numpy | Samba format |
| Passwords | argon2-cffi | maintained Argon2id implementation |
| Config | stdlib `tomllib` + `tomli-w` | human-editable, comments in the example |
| Drivers (backend only) | nidaqmx 1.4.1, pyvisa 1.16.2, pythonnet 3.0.5, pylablib 1.4.5 (installed `--no-deps` to keep PyQt5 out), pytrinamic 0.2.23 | versions verified in the MOKE env |
| Vendor runtimes (not pip) | NI-DAQmx driver, NI-VISA, Thorlabs Kinesis, Magnet-Physik USB driver + `MagnetPhysik.Usb.dll` (64-bit build from the MiniMoke `libs` folder; path configured, not committed) | |

## 3. Phases

Status 2026-11-16: phases 1–7 done; phase 8 (hardware validation) not started — see [progress.md](progress.md).

1. **Audit and design** – this document and architecture.md.
2. **Service core (simulation)** – protocol, device model, backend server with
   auth, lease, per-instrument workers, operations; simulated DAQ, lock-in,
   Hall probe and stages; client proxies.  Tests through real sockets.
3. **Engine and storage** – workflows, plans, Qt-free engine with
   start/pause/resume/abort, HDF5 writer/reader, CSV export, lab notebook;
   end-to-end simulated measurement through the service boundary.
4. **Samba_mini UI** – Samba layout, theme, plots, data browser, login,
   presets, instrument tab.
5. **Samba_controls UI** – backend lifecycle, device table, manual controls,
   user management, lease display.
6. **Real drivers** – NI-DAQ, 7270, Hall probe, Kinesis, TMCM-3212 with fakes in
   tests; MiniMoke INI import; real mode diagnostics without moving anything.
7. **Hardening and documentation** – recovery tests (timeouts, disconnects,
   lease loss, stale results, interrupted files, config migration), launchers,
   setup/troubleshooting, hardware check list.
8. **Hardware validation** – only with explicit authorisation from the lab:
   read-only connection checks first, then outputs at zero, then small moves and
   fields, then a short measurement compared against MiniMoke.
