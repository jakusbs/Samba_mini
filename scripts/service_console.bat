@echo off
rem Run the device service in this console (diagnostics).  Ctrl+C stops it cleanly.
rem   service_console.bat --mode simulation
rem   service_console.bat --mode real --no-connect
setlocal
call "%~dp0_python.bat"
if not defined SM_PY ( pause & exit /b 1 )
cd /d "%~dp0.."
"%SM_PY%" -m Samba_controls.service %*
