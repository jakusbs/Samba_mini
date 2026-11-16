# Samba_mini installer for Windows (counterpart of Samba's Linux install.sh).
#
#   install.bat                      (double-click, or run from a console)
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 [options]
#
# What it does:
#   1. finds conda (miniconda3/miniforge3/anaconda3, per-user or all-users)
#   2. creates/updates the "SambaMini" environment (Python 3.11) via setup_env.ps1
#      -- MiniMoke's MOKE / MOKE_mfli environments are not touched
#   3. verifies the imports (no instrument is opened)
#   4. records the interpreter in .install_config for the .bat launchers
#   5. creates Desktop + Start-menu shortcuts "Samba_mini" and "Samba_controls"
#      (Start menu also gets "Samba_mini service console (diagnostics)")
#
# Options:
#   -Conda <path to conda.exe>   use this conda (default: auto-detect)
#   -EnvName <name>              environment name (default SambaMini)
#   -NoHardware                  skip the driver packages (simulation-only PC)
#   -SkipEnv                     do not create/update the environment, only shortcuts
#   -NoDesktop                   Start-menu shortcuts only
#   -Uninstall                   remove the shortcuts and .install_config
#                                (environment, configuration, accounts and data are kept)
#   -DesktopDir / -StartMenuDir  override shortcut locations (testing)
param(
    [string]$Conda = "",
    [string]$EnvName = "SambaMini",
    [switch]$NoHardware,
    [switch]$SkipEnv,
    [switch]$NoDesktop,
    [switch]$Uninstall,
    [string]$DesktopDir = [Environment]::GetFolderPath("Desktop"),
    [string]$StartMenuDir = (Join-Path ([Environment]::GetFolderPath("Programs")) "Samba_mini")
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$configFile = Join-Path $repo ".install_config"

function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Step($m) { Write-Host "`n>> $m" -ForegroundColor Cyan }

$shortcuts = @(
    @{ Name = "Samba_mini";     Module = "Samba_mini";     Icon = "samba_mini.ico";
       Desc = "Samba_mini - MiniMOKE measurements (needs the device service)" },
    @{ Name = "Samba_controls"; Module = "Samba_controls"; Icon = "samba_controls.ico";
       Desc = "Samba_controls - device service and instrument management" }
)

Write-Host ""
Write-Host "=========================================" -ForegroundColor White
Write-Host "  Samba_mini  -  Windows installer" -ForegroundColor White
Write-Host "=========================================" -ForegroundColor White
Write-Host "  Project dir : $repo"

if ($Uninstall) {
    Step "Removing shortcuts"
    foreach ($s in $shortcuts) {
        $p = Join-Path $DesktopDir "$($s.Name).lnk"
        if (Test-Path $p) { Remove-Item $p -Force; Ok "removed $p" }
    }
    if (Test-Path $StartMenuDir) { Remove-Item $StartMenuDir -Recurse -Force; Ok "removed $StartMenuDir" }
    if (Test-Path $configFile) { Remove-Item $configFile -Force; Ok "removed .install_config" }
    Write-Host "`nEnvironment '$EnvName', %LOCALAPPDATA%\SambaMini (configuration, accounts) and data were kept."
    exit 0
}

# ---------------------------------------------------------------- 1. conda
Step "Conda"
if (-not $Conda) {
    $candidates = @("$env:USERPROFILE\miniconda3", "$env:USERPROFILE\miniforge3", "$env:USERPROFILE\anaconda3",
                    "C:\ProgramData\miniconda3", "C:\ProgramData\miniforge3", "C:\ProgramData\anaconda3")
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c "Scripts\conda.exe")) { $Conda = Join-Path $c "Scripts\conda.exe"; break }
    }
}
if (-not $Conda -or -not (Test-Path $Conda)) { throw "conda not found - install Miniconda first or pass -Conda <path>" }
$condaBase = Split-Path -Parent (Split-Path -Parent $Conda)
Ok "conda: $Conda"
$py = Join-Path $condaBase "envs\$EnvName\python.exe"

# ---------------------------------------------------------------- 2./3. environment
if ($SkipEnv) {
    Step "Environment (skipped)"
    if (-not (Test-Path $py)) { throw "environment $EnvName not found at $py; run without -SkipEnv" }
    & $py -c "import PyQt6.QtWidgets, matplotlib, h5py, zmq, argon2, tomli_w; print('  core imports OK')"
    if ($LASTEXITCODE -ne 0) { throw "core imports failed in $py" }
} else {
    Step "Environment '$EnvName' (Python 3.11)"
    $setupArgs = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "setup_env.ps1"), "-Conda", $Conda,
              "-EnvName", $EnvName)
    if ($NoHardware) { $setupArgs += "-NoHardware" }
    & powershell @setupArgs
    if ($LASTEXITCODE -ne 0) { throw "environment setup failed" }
}
$pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
Ok "interpreter: $py"

# ---------------------------------------------------------------- 4. launcher config
Step "Launcher configuration"
Set-Content -Path $configFile -Value "SAMBA_MINI_PYTHON=$py" -Encoding ascii
Ok ".install_config -> scripts\*.bat use $py"

# ---------------------------------------------------------------- 5. shortcuts
Step "Shortcuts"
$wsh = New-Object -ComObject WScript.Shell
function New-Shortcut($path, $target, $arguments, $icon, $desc) {
    $l = $wsh.CreateShortcut($path)
    $l.TargetPath = $target
    $l.Arguments = $arguments
    $l.WorkingDirectory = $repo
    $l.Description = $desc
    if (Test-Path $icon) { $l.IconLocation = "$icon,0" }
    $l.Save()
    Ok $path
}
New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null
foreach ($s in $shortcuts) {
    $icon = Join-Path $repo "assets\$($s.Icon)"
    New-Shortcut (Join-Path $StartMenuDir "$($s.Name).lnk") $pyw "-m $($s.Module)" $icon $s.Desc
    if (-not $NoDesktop) {
        New-Shortcut (Join-Path $DesktopDir "$($s.Name).lnk") $pyw "-m $($s.Module)" $icon $s.Desc
    }
}
New-Shortcut (Join-Path $StartMenuDir "Samba_mini service console (diagnostics).lnk") $env:ComSpec `
    "/k `"`"$repo\scripts\service_console.bat`"`"" (Join-Path $repo "assets\samba_controls.ico") `
    "Run the device service in a console window (add --mode simulation / --read-only by editing)"

# ---------------------------------------------------------------- done
Write-Host ""
Write-Host "=========================================" -ForegroundColor Green
Write-Host "  Installation complete" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
Write-Host "  Start 'Samba_controls' first (it starts the device service), then 'Samba_mini'."
Write-Host "  First start without a machine configuration: it offers to import MiniMoke's settings."
Write-Host "  Configuration, accounts and logs: $env:LOCALAPPDATA\SambaMini"
Write-Host "  Remove shortcuts again:  install.bat -Uninstall"
