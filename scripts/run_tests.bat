@echo off
rem Run the automated tests (simulation and fake drivers only; no hardware is touched).
setlocal
call "%~dp0_python.bat"
if not defined SM_PY ( pause & exit /b 1 )
cd /d "%~dp0.."
set QT_QPA_PLATFORM=offscreen
"%SM_PY%" -m pytest tests -q --timeout 300 %*
