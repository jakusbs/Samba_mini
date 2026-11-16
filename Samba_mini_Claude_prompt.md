# Build Samba_mini and Samba_controls for Windows

Act as a senior Python developer experienced in PyQt6 desktop applications, scientific acquisition, Windows instrument drivers, and reliable device-control systems. Work in the provided workspace: inspect the source, implement the application, and validate it incrementally.

## Goal and confirmed decisions

Recreate the structure, appearance, and relevant measurement experience of [Samba](https://github.com/jakusbs/Samba) for the Windows measurement PC currently running [MiniMoke](https://github.com/jakusbs/MiniMoke).

Build two applications:

- **Samba_controls:** a PyQt6 hardware-management application that hosts and manages local device services.
- **Samba_mini:** a PyQt6 measurement application that uses those services to run experiments, display live data, and save results.

These decisions are settled:

1. Use a **TANGO-inspired object model**, with devices, attributes, commands, state, and status. Actual TANGO, PyTango, Jive compatibility, and a TANGO database are **not required**.
2. The first deployment is **one shared Windows PC**, with local password-protected user profiles and one active operator controlling the setup at a time. Remote clients are future scope.
3. The first release covers **MiniMoke's existing measurement workflows**, using Samba's structure and interface. Do not expand the initial scope to all Samba hardware and experiments.
4. Use **PyQt6** for both new interfaces. Preserve Samba's visual identity and reuse suitable components and logic.

## Project repository and saving progress

Use [jakusbs/Samba_mini](https://github.com/jakusbs/Samba_mini) as the canonical repository for both applications, shared code, tests, and documentation. Samba and MiniMoke are reference repositories; preserve their source checkouts and the working MiniMoke installation.

Clone or use the existing Samba_mini checkout, inspect its current branch and working tree, and preserve any existing work. Read this prompt from `Samba_mini_Claude_prompt.md`. Develop on a descriptive feature branch, starting from the current default branch. Do not replace an existing implementation with a fresh scaffold.

Save progress to GitHub throughout the task: make focused commits at meaningful milestones, run the appropriate checks, and push the working branch. This task authorizes those commits and pushes; do not ask for confirmation at each milestone. Never force-push, rewrite shared history, or discard unrelated changes. Leave merging to the default branch as a separate decision.

Keep the README accurate about what is implemented, how to run it, and which checks actually passed. Record the architecture and phased plan in `docs/architecture.md` and `docs/implementation-plan.md` after the initial audit. Maintain a concise `docs/progress.md` with completed work, test results, remaining hardware checks, and the next step so work can resume in a later session.

Commit source, documentation, dependency specifications, and sanitized example configuration. Keep credentials, account databases and password hashes, real measurement data, machine-specific overrides, virtual environments, runtime logs, and build outputs out of Git. Use a suitable `.gitignore`; do not copy vendor binaries into the repository without checking redistribution terms. If GitHub authentication or push access is unavailable, preserve local commits and report that limitation accurately.

## Inspect the repositories before designing

Use Samba as the reference for application structure, UI, plotting, configuration, acquisition organization, and data browsing. Use MiniMoke as the reference for the actual Windows hardware, working communication protocols, measurement behavior, calibration, and installation constraints.

Read the current code and relevant documentation, including Samba's README, CLAUDE.md, core modules, Samba_main panels, and tests; and MiniMoke's entry point, classes, procedures, configs, Ethernet notes, and tests. Record the source revisions. If the installed MiniMoke checkout has local changes, inspect and preserve them. Treat source behavior as stronger evidence than potentially outdated documentation, and report discrepancies.

Useful starting points:

| Purpose | Source paths |
|---|---|
| Samba scan execution and threading | `core/scan/runner.py`, `core/scan/workers.py` |
| Samba hardware abstraction | `core/hardware.py`, `core/device_registry.py`, `core/setup_lock.py` |
| Samba UI and persistence | `Samba_main/samba.py`, `Samba_main/panels/`, `Samba_main/config.py`, `core/theme.py`, `core/plot_widgets.py`, `core/data_browser.py` |
| MiniMoke hardware initialization | `src/classes/__init__.py`, `src/classes/config_handler.py`, `configs/` |
| MiniMoke measurements | `src/procedures/b_sweep_proc.py`, `b_sweep_ac.py`, `position_sweep.py`, `x_sweep_proc.py`, `y_sweep_proc.py`, `xy_sweep_proc.py`, `time_proc.py` |

Known integration issues to verify:

- MiniMoke's entry point imports PyQt5 and PyMeasure UI components. Its class package creates hardware instances during import. Extract or adapt reusable drivers without importing this global initialization or loading PyQt5 into the new GUI processes.
- Samba is coupled to TANGO proxies and TANGO availability flags. Replace this coupling with an explicit backend interface; missing PyTango must not force the new application into simulation.
- Check actual dependencies of the scan runner rather than assuming it is Qt-free because documentation says so. Keep reusable scan logic independent of Qt where practical.
- Some existing offline paths return zeros or synthetic values. These must become explicit simulation or unavailable states, never apparently valid real measurements.

## Application and service boundaries

Retain a recognizable Samba layout: shared `core/`, thin application entry points, reusable panels, configuration modules, and separated scan execution. Add `Samba_controls/` and `Samba_mini/`. Propose the final directory layout after inspection; avoid copying large modules into both applications.

**Samba_controls owns hardware access.** Its backend creates, connects, configures, and closes device objects. It exposes them through a documented local API. The management UI shows connection state, status, errors, readbacks, and device configuration, and supports connect, disconnect, reconnect, and manual controls.

Run blocking driver work outside GUI threads. Prefer a backend process managed by Samba_controls, with serialized work per physical instrument. A single local server hosting several device objects is acceptable; separate servers per device are not mandatory. Keep the backend lifecycle explicit, prevent duplicate hosts, and document shutdown behavior. Do not require a Windows service or administrator privileges for normal use.

**Samba_mini uses client proxies only.** It must never open NI-DAQ, VISA, DLL, USB, or serial hardware connections directly. Keep scan orchestration in a reusable engine and route hardware operations through the service interface. Put tightly timed acquisition sequences in the backend where needed; repeated GUI-to-server calls must not replace existing hardware timing.

Choose a small, maintained IPC/RPC approach suitable for Windows and justify it briefly. Bind to loopback only for the first release. Specify protocol versioning, request IDs, structured errors, bounded timeouts, asynchronous operation handles, and a practical way to obtain progress and readbacks. Avoid unsafe object deserialization or remotely callable arbitrary Python evaluation.

Provide a device interface along these lines: identity and capabilities; state/status; attribute metadata with units and limits; read/write attributes; execute commands; operation progress/results; connect/disconnect/reconnect. Use this contract for both real and simulated devices. A compatibility adapter may preserve useful Samba proxy call patterns without pretending to implement TANGO.

## Hardware to adapt

Verify these devices against the current MiniMoke source and the local installation:

| Device/service | Existing implementation | Preserve or investigate |
|---|---|---|
| NI-DAQ acquisition and outputs | `src/classes/dac_class.py` | Channel mapping, finite acquisition, buffered outputs, trigger/task lifecycle, diode acquisition, coil output |
| Ametek / Signal Recovery 7270 lock-in | `src/classes/ametek7270_class.py` | VISA USB/Ethernet, protocol framing, X/Y/R/phase readings, settings, reconnect behavior |
| MagnetPhysik Hall probe | `src/classes/hallsensor_class.py`, `libs/MagnetPhysik.Usb.dll` | Compatible .NET/Python bitness, DLL loading, integration/filter behavior, measured field units |
| Longitudinal stage | `src/classes/longitudinal_stage_class.py` | Thorlabs Kinesis via pylablib, positions, offsets, backlash and motion completion |
| Polar stage | `src/classes/polar_stage_class.py` | Trinamic TMCM-3212 via pytrinamic, axis scaling, limits and motion completion |

The magnet interface may be a logical device backed by the NI-DAQ output and Hall probe. Shared physical hardware must have one owning driver/session; logical services must coordinate through that owner. Preserve the single Ametek session used for both configuration and measurement.

Do not invent device commands, ranges, calibration constants, or physical wiring. Inspect installed drivers and local configuration without initiating hardware activity. Preserve machine-specific overrides. Make DLL locations configurable instead of embedding a particular Windows username.

Audit every conversion among DAC output voltage, commanded coil current, estimated field, measured field, stage position, and time. Existing B-sweep names and display units are not sufficient evidence of physical units. Store setpoints and measured values separately. Mark estimated or later-calibrated field values explicitly; never describe them as simultaneous Hall measurements.

## Measurement UI and first-release scope

Preserve Samba's dark theme, overall panel arrangement, named configuration sidebar, trajectory/settings controls, sensor selection, live plots, status reporting, and data browser. Adapt hardware panels to MiniMoke's actual capabilities.

Implement:

- DC B-sweeps and lock-in B-sweeps, including forward/reverse sweeps, repeats, raw cycles and averaging as supported by MiniMoke.
- X, Y, and XY spatial scans.
- Time measurements at fixed settings.
- Longitudinal/polar stage selection, applicable manual positioning, live readbacks, and instrument settings.
- Start, pause, resume, and abort with explicit, tested behavior.

Preserve the meaningful existing parameters and recorded channels: DC/balanced-diode signal, intensity, Hall field, lock-in components, positions, timestamps, and relevant statistics. Establish a workflow parity table before changing behavior. Reuse existing test expectations where appropriate, but identify intentional correctness fixes.

Do not import assumptions about Beckhoff hysteresis engines, Zurich lock-ins, SmarAct stages, Cryo hardware, DG645 timing, or other absent equipment. Keep advanced scanlists, extra calibration workflows, and wider analysis integration for later unless required for first-release parity.

## Profiles and exclusive control

Implement local account creation, login/logout, password changes, and per-user preferences, named measurement presets, sample/operator defaults, and preferred data locations. Use a maintained password-hashing implementation with salts and an appropriate work factor; never store plaintext or reversibly encrypted passwords.

Keep machine-wide wiring, instrument addresses, calibration, and physical limits separate from user preferences. Use a simple administrator/operator distinction if needed for editing shared settings. Store account data locally, for example in SQLite, with schema migrations. Do not require cloud accounts or email.

Authentication and exclusive-control checks must be enforced in the backend, not just by disabled GUI buttons. Authenticated sessions identify the operator. One setup lease/reservation controls all conflicting mutations, including manual controls in Samba_controls. Monitoring must use coordinated readbacks and must not race the acquisition driver. Block user switching during an active measurement unless it is safely stopped first.

Reject unauthenticated control requests and concurrent conflicting commands. On a lost client, use a bounded heartbeat/lease policy to cancel outstanding work and perform the configured cleanup. Do not grant a new owner until the previous operation has stopped and hardware state is reconciled. Local application accounts organize access and attribution; do not claim they isolate files from an administrator or other processes running under the same Windows account.

## Acquisition correctness and recovery

Preserve verified MiniMoke acquisition and settling behavior. Do not assume every sensor supports Samba's original trigger/state sequence. Define completion semantics for each device and use operation IDs or equivalent evidence to avoid reading a previous acquisition as a new point.

Serialize access per instrument, enforce backend validation and configured limits, and bound waits for moves, integration, and commands. A client timeout does not prove a hardware operation stopped. Reconcile its result before retrying; never blindly repeat relative moves or other non-idempotent commands.

Separate explicit simulation from real mode. In real mode, unavailable required hardware prevents measurement; the interface can still start and display diagnostics. Clearly label simulated UI sessions and output files. Never silently substitute synthetic values, stale values, or zeros for missing readings.

Define pause, abort, disconnect, GUI exit, backend failure, and reconnect behavior for motion, coil output, lock-in excitation, and pending DAQ tasks. Base output cleanup and ramping on verified hardware behavior. Do not assume instantly setting every output to zero is universally appropriate. Prevent a timed-out operation from issuing late writes after abort. Preserve hardware interlocks; report when software cannot verify a safe state after a driver or process failure.

## Data, configuration, and Windows installation

Use Samba-style HDF5 organization and incremental acquisition saving. Record units, timestamps, user ID, sample metadata, configuration and calibration snapshots, device identities, software revision, real/simulation mode, and acquisition status. Preserve raw per-cycle data where applicable and offer CSV export for existing workflows.

Handle disk errors explicitly. Use a deliberate flush policy and test interrupted runs; document the actual recovery guarantees instead of claiming absolute crash-proof storage. Keep partial measurements identifiable and never mark an aborted run as completed.

Use Windows-appropriate application/configuration paths. Version schemas, back up files before migration, and preserve malformed configuration for diagnosis. Provide an explicit import path for relevant MiniMoke INI settings without overwriting the original installation.

Create a separate environment for the new application. Select the Python version after checking compatibility with the real driver stack, pythonnet/DLL requirements, PyQt6, and the chosen backend. Do not force Samba's Python version onto the existing installation. Provide reproducible dependencies, PowerShell or batch launchers, and setup/troubleshooting instructions. Separate vendor runtime prerequisites from Python packages. Validate source-based launching before considering an executable bundle.

## Implementation approach and acceptance

First produce a concise source-backed audit, reuse/adaptation map, architecture, workflow parity table, dependency assessment, and phased implementation plan. Then proceed with implementation using these confirmed decisions; ask only questions whose answers cannot be established from code or local configuration and materially block progress.

Build an end-to-end simulated measurement through the actual service/client boundary first. Then add workflows and verified real drivers incrementally. Work in the Samba_mini repository on the feature branch described above, committing and pushing each meaningful milestone. Preserve the working MiniMoke installation, environments, local overrides, and data.

Validate behavior, not merely imports: service startup, simulated scan saving, pause/resume/abort, unavailable devices, driver timeouts, disconnect recovery, stale-result rejection, unit conversion, profile persistence, backend authentication, competing control requests, and configuration migration. Inspect the running PyQt6 interfaces for responsiveness and layout.

Report automated tests separately from manual checks and real-hardware validation. Do not claim real hardware was tested when only mocks or simulation were available. Ask for explicit authorization before moving stages, energizing outputs, or starting a real measurement; continue source work and simulation independently.

The first release is complete when both apps launch on Windows; Samba_controls manages the local devices; Samba_mini runs the listed MiniMoke workflows through those services; users can log in and recover their preferences; conflicting control is rejected; results are saved with correct provenance and units; and installation and recovery procedures are documented. Deliver working code, example configuration, launchers, tests, documentation, and an honest list of remaining hardware checks.
