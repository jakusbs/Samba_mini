# Samba_mini

A planned Windows measurement application based on [Samba](https://github.com/jakusbs/Samba), adapted to the hardware and workflows of [MiniMoke](https://github.com/jakusbs/MiniMoke).

**Current status:** project brief and implementation prompt are available. Application code has not yet been implemented in this repository.

## Applications

| Application | Responsibility |
|---|---|
| **Samba_controls** | PyQt6 device-management interface and local device-service host; owns physical hardware connections. |
| **Samba_mini** | PyQt6 measurement interface; runs experiments through the local services, plots live data, and saves results. |

The backend uses a TANGO-inspired device/attribute/command model. Actual TANGO infrastructure is not required. The initial deployment is one shared Windows PC with password-protected local profiles and exclusive control for one operator.

## Initial measurement scope

- DC and lock-in B-sweeps.
- X, Y, and XY spatial scans.
- Time measurements.
- Longitudinal and polar stage controls, instrument settings, and live readbacks.
- HDF5 acquisition saving and CSV export.

Samba supplies the interface and architecture reference. MiniMoke supplies the existing driver and measurement reference. Preserve the working MiniMoke installation during development.

## Start implementation

The complete brief is in [Samba_mini_Claude_prompt.md](Samba_mini_Claude_prompt.md).

Give Claude access to this repository and the two reference repositories, then ask it to read and implement that prompt. Start with a source audit and a simulated end-to-end measurement before testing real instruments.

Develop on a feature branch and commit and push meaningful milestones. Maintain the implementation plan, architecture, test results, and remaining work in `docs/` as development proceeds. Machine-specific settings, account databases, credentials, and real measurements must remain outside Git.
