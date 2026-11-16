@echo off
rem Start Samba_controls (it starts or attaches to the device service).
setlocal
call "%~dp0_python.bat"
if not defined SM_PY ( pause & exit /b 1 )
cd /d "%~dp0.."
set "SM_PYW=%SM_PY:python.exe=pythonw.exe%"
if exist "%SM_PYW%" (
    start "" "%SM_PYW%" -m Samba_controls %*
) else (
    "%SM_PY%" -m Samba_controls %*
)
