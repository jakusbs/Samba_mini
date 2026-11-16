@echo off
rem Resolve the SambaMini environment's interpreter.
rem Order: SAMBA_MINI_PYTHON, then .install_config written by install.bat, then usual conda locations.
if defined SAMBA_MINI_PYTHON (
    set "SM_PY=%SAMBA_MINI_PYTHON%"
    goto :eof
)
if exist "%~dp0..\.install_config" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0..\.install_config") do (
        if /i "%%A"=="SAMBA_MINI_PYTHON" set "SM_PY=%%B"
    )
)
if defined SM_PY if exist "%SM_PY%" goto :eof
set "SM_PY=%USERPROFILE%\miniconda3\envs\SambaMini\python.exe"
if exist "%SM_PY%" goto :eof
set "SM_PY=%ProgramData%\miniconda3\envs\SambaMini\python.exe"
if exist "%SM_PY%" goto :eof
set "SM_PY=%USERPROFILE%\anaconda3\envs\SambaMini\python.exe"
if exist "%SM_PY%" goto :eof
echo SambaMini Python environment not found. Run install.bat or set SAMBA_MINI_PYTHON.
set "SM_PY="
