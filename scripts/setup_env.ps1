# Create or update the SambaMini conda environment (separate from MiniMoke's "MOKE" env).
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1 [-Conda <path to conda.exe>] [-NoHardware]
#
# Vendor runtimes are NOT installed by this script (see docs\installation.md):
# NI-DAQmx, NI-VISA, Thorlabs Kinesis (FTDI driver), Magnet-Physik USB driver.
param(
    [string]$Conda = "$env:USERPROFILE\miniconda3\Scripts\conda.exe",
    [string]$EnvName = "SambaMini",
    [switch]$NoHardware
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path $Conda)) { throw "conda not found at $Conda (pass -Conda)" }

$envs = & $Conda env list
if (-not ($envs -match "^\s*$EnvName\s")) {
    Write-Host "Creating conda environment $EnvName (Python 3.11)..."
    & $Conda create -y -n $EnvName python=3.11 pip
    if ($LASTEXITCODE -ne 0) { throw "conda create failed" }
}
$base = Split-Path -Parent (Split-Path -Parent $Conda)
$py = Join-Path $base "envs\$EnvName\python.exe"
if (-not (Test-Path $py)) { throw "environment python not found: $py" }

& $py -m pip install --disable-pip-version-check -r "$repo\requirements.txt" -r "$repo\requirements-dev.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install (core) failed" }
if (-not $NoHardware) {
    & $py -m pip install --disable-pip-version-check -r "$repo\requirements-hw.txt"
    if ($LASTEXITCODE -ne 0) { throw "pip install (hardware) failed" }
    & $py -m pip install --disable-pip-version-check --no-deps "pylablib==1.4.5"
    if ($LASTEXITCODE -ne 0) { throw "pip install pylablib failed" }
}

Write-Host "`nChecking imports (no hardware is opened)..."
& $py -c "import PyQt6.QtWidgets, matplotlib, h5py, zmq, argon2, tomli_w; print('core OK')"
if (-not $NoHardware) {
    & $py -c "import nidaqmx, pyvisa, pytrinamic, clr_loader; import pylablib.devices.Thorlabs; import sys; print('drivers OK; PyQt5 loaded:', 'PyQt5' in sys.modules)"
}
Write-Host "`nEnvironment ready: $py"
Write-Host "Next: scripts\Samba_controls.bat (service + management), then scripts\Samba_mini.bat"
