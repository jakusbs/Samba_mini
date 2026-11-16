# Progress

Working branch: `feature/samba-mini-first-release` (from `main` at `960feb7`).

## Done

* 2026-11-16 — Source audit of Samba `75c0bdf4`, MiniMoke `6c7c0fdd` and the
  local MiniMoke installs; architecture, reuse map, parity table, dependency
  assessment and phased plan written (`docs/architecture.md`,
  `docs/implementation-plan.md`).
* New conda environment `SambaMini` (Python 3.11.18) with PyQt6 6.10.2,
  matplotlib, h5py, pyzmq, argon2-cffi and the MOKE-verified driver versions.

## Tests

None yet.

## Remaining hardware checks

To be filled as drivers land (see implementation plan, phase 8).

## Next step

Phase 2: service core with simulated devices.
