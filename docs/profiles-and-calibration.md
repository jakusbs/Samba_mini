# Profiles, saved state and calibration

How Samba_mini decides what belongs to whom, when it is saved, and how λ/2 (BD)
calibrations are made, recorded and used.  Introduced on branch
`fix/profiles-calibration-device-state` (2026-11-17).

## 1. Ownership

| State | Owner / scope | Stored in | Saved |
|---|---|---|---|
| **User preferences**: data folder, default operator, lab-notebook path, CSV export, θ display, last setup | account | service, `prefs` table | automatically |
| **Working state** per setup: the draft of every workflow, the selected configuration (and whether it was modified), the current **experiment** (sample, device, operator, notes, R4W/R2W, t_FM/t_S), the calibration-scan settings | account × setup (`prefs` key `working.<setup>`) | service | automatically |
| **Measurement presets** ("configurations", left list): workflow, its parameters, optics (incidence, mirror shift, polarization, λ/2, λ/4, noDC) | account × setup | service, `presets` table | only with **+** / **S** / **C** / **R** |
| **Calibration records** (λ/2): immutable versions | account | service, `calibrations` table | only with **Save** / **Save as new…** |
| **Active calibration** for the experiment on a setup | account × setup | service, `calibration_active` | only with **Activate** / **Deactivate** |
| **Machine configuration**: device connections, limits, wiring, stage zeros | the PC (administrators) | `machine.toml`, Samba_controls | administrators |
| **Live device state** | the device service | — | never stored by the GUIs |

A configuration is a reusable recipe and never contains the sample or the
operator.  Configurations saved before this change may still contain them;
loading such a configuration asks before copying that information into the
current experiment (and then runs the normal sample-change check).

Every account sees only its own preferences, working state, configurations and
calibrations.  As before, local accounts organise access and attribution on a
shared PC; they do not protect files from an administrator or from other
programs of the same Windows user.

## 2. Saving rules

* **Autosave** (preferences, working state): a change is sent about 0.4 s
  after the last edit.  Saves go out one after another, so an older value can
  never overwrite a newer one.  The status bar shows `Prefs: ✓ saved`,
  `● saving…` or `⚠ NOT SAVED`; the Profile tab shows the error with
  **Retry**.  A failed save keeps the values on screen and in memory
  (nothing typed is lost) and is retried with the next change.  If the
  preferences cannot be *loaded* at login, nothing is saved in that session,
  so the stored values are never overwritten with defaults.
* **One data folder**: the action bar's `Dir:` and the Profile tab's *Data
  folder* edit the same preference.
* **Each account has its own data folder**: `Documents\SambaMini Data\<user name>`
  (the root can be moved with `SAMBA_MINI_DATA_ROOT`), created at login; runs,
  scanlists, the lab notebook and the Data Browser use it.  An account whose
  saved folder is the shared root (the default of earlier versions) is moved
  to its own folder at login; a folder the user chose is kept, and *Own
  folder* in the Profile tab goes back.  Existing files are not moved by the
  software.
* **Configurations** (explicit): **+** saves the current settings under a new
  name (refused if the name exists), **S** saves them into the selected
  configuration, **C** copies the *saved* selected configuration, **R**
  renames it.  Rename and copy are single transactions in the service and are
  refused when the target name exists — nothing is overwritten.  While the
  current settings differ from the selected configuration, its entry shows
  `•` and a hint under the list.  Clicking another configuration (or the same
  one to revert) then asks **Save / Discard / Cancel**.  A failed save stays
  visible under the list and leaves the settings untouched.
* **Setups**: switching Longitudinal ↔ Polar parks the working state of one
  setup and restores the other's (or its defaults); the selected
  configuration and its "modified" mark belong to the setup.
* **Calibration editor**: unsaved values are never replaced behind the
  user's back.  Switching setup, logging out, closing the window, loading
  another calibration, importing a fit, starting a new one or resetting for a
  new sample ask **Save / Discard / Cancel** first.  Saving preferences never
  touches the calibration.

## 3. Login, logout, expiry

Login, logout, session expiry and account changes all end in one function
(`MainWindow._apply_session`) that sets *every* user-owned widget from the
account's stored state or an explicit default, so nothing from a previous user
survives because a key is missing.  It only fills widgets: stage positions are
*read*; no stage is moved, no output set and no instrument configured.

* **Logout** saves pending changes first (asks if that fails), releases
  control and logs out; the window returns to the logged-out defaults.
* **Session expiry** (service restart, idle timeout): the window returns to the
  logged-out defaults at once (after the running measurement, if one is
  running).  Unsaved changes are kept in memory and given back only if the
  *same* account logs in again; another account starts from its own state.
* Requests made before a login change are tagged with the client's
  *session epoch*; replies that arrive afterwards are dropped and cannot
  update what the next user sees (`core.gui.bridge.call_guarded`).

## 4. Calibration workflow

```
① Acquire        Calibration tab: Start runs the calibration time scan with the
                 settings shown in its "Calibration scan" box (Points, Int,
                 Settle), samples back to back, with the coil and the lock-in as
                 they are set (Hardware box).  Nothing else is used.
② Fit & review   When the scan completes it is fitted in the BD Calibration tab
                 (unsaved draft).  Or: Fit & Import… lists the calibration scans of
                 the data folder, best matches first, and fits the chosen one.
③ Save           Save = a new version of the calibration shown; Save as new… =
                 version 1 of a new calibration.
④ Activate       "Activate for this experiment" — for this account, setup and the
                 current sample.  Deactivate = measure without a calibration.
```

**File checks** (Fit & Import, and the automatic fit): the file's setup,
sample, acquisition status (`completed`) and real/simulation mode must match
the current experiment.  A mismatch or missing information is listed and
needs an explicit "use anyway"; the automatic fit after an acquisition never
asks, it only reports.  An accepted mismatch is recorded in the record's fit
information.  No file is chosen because it is the newest.

**Record contents** (`calibration.save`, immutable): `id`, `series`,
`version`, `name`, `creator` (from the session), `created`, `recorded`,
`setup`, `sample_id` (the sample it was *measured* on; `null` = unknown),
`values` (six numbers) and `unit` (`mV`), `mode` (`real` / `simulation` of the
measured scan; `null` for hand-entered or unknown), `service_mode` at saving,
`source_file`, `fit` (plateaus found, candidate runs, spacing CV, mean step,
midpoint, edited-after-fit, accepted mismatches), `origin` (`manual`, `fit`,
`migrated`), `note`.

**Use in measurements.**  At Start the active calibration is read from the
service (not from a cache) and frozen: the live θ display and the file use
that one snapshot until the run ends (a scanlist keeps one snapshot for all
its scans).  Activating or editing another calibration during a run is
allowed and applies from the next run; the BD tab says which calibration the
running measurement uses.  Every new file records

* `/data/calibration` — the six values (mV), with attributes
  `calibration_id` and `calibration_version` (as before, not written when
  there is no calibration);
* `/metadata` `bd_calibration_id`, `bd_calibration_version` and
  `bd_calibration_json` (the full snapshot, including the sample it was
  activated for; `{"active": false}` without a calibration).

**New sample.**  Changing the sample while a calibration activated for
another sample is active asks whether to start a new calibration; *Yes*
deactivates it in the service, so neither a setup switch nor a restart brings
it back.  The record itself stays available under **Load saved…**.

## 5. Migration (accounts database schema 1 → 2)

Runs automatically the first time the updated **device service** starts:

1. The database file is copied next to itself as
   `accounts.sqlite3.v1.<unix time>.bak` (in `%LOCALAPPDATA%\SambaMini\data`)
   before anything is changed.
2. Tables `calibrations` and `calibration_active` are created.
3. Each non-zero `bd_calibration_<setup>` preference becomes a calibration
   record with origin `migrated`, creator = the account that held it, created
   = the saved `bd_calibration_date_<setup>` (or *unknown*), and **sample,
   acquisition mode, source file and fit = unknown** (nothing is guessed).  It
   is made the account's active calibration for that setup, so runs keep the
   calibration they used before.  The BD tab shows the unknown fields.
4. The old preference keys are left in place (an older Samba_mini still finds
   them); the new version no longer reads or writes them.

All in one transaction; a failure leaves the database unchanged.  A database
written by a newer version is refused, as before.

**To upgrade on the measurement PC:** stop measurements, close Samba_mini,
stop and restart the device service from Samba_controls (or close and reopen
Samba_controls and let it start the service), then start Samba_mini.  A new
Samba_mini connected to the *old* running service still starts; saved
calibrations, activation and atomic rename are refused with a message asking
to restart the service, and runs record no calibration (logged).

**To go back** to the previous release: stop the service and replace
`accounts.sqlite3` with the `.bak` copy (calibrations and settings saved
after the upgrade are then lost).  The previous service refuses to open the
upgraded (schema 2) database — it does not start rather than modify it — so
the `.bak` copy is required for a downgrade.

## 6. Samba_controls device panel

The detail panel tags each request with the selected device and a selection
generation.  While a device's information is loading, every control is
disabled; a reply for an earlier selection is ignored; command buttons are
built for — and only ever act on — the device whose information they came
from.  Login changes, session expiry and service restarts clear the panel.
The panel shows the service's state (connected, disconnected, busy, fault,
unavailable) live from state events, the requested and applied value of the
last write, the result of each command, and live readbacks with their
quality (valid, simulated, stale with age, no reading).  Lease and permission
checks stay in the service.
