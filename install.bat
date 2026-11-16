@echo off
rem Samba_mini installer (Windows): environment + Desktop/Start-menu shortcuts.
rem Options are passed on, e.g.  install.bat -SkipEnv   or   install.bat -Uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
echo.
pause
