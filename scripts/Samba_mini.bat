@echo off
rem Start the Samba_mini measurement application (needs the device service; see Samba_controls.bat).
setlocal
call "%~dp0_python.bat"
if not defined SM_PY ( pause & exit /b 1 )
cd /d "%~dp0.."
set "SM_PYW=%SM_PY:python.exe=pythonw.exe%"
if exist "%SM_PYW%" (
    start "" "%SM_PYW%" -m Samba_mini %*
) else (
    "%SM_PY%" -m Samba_mini %*
)
