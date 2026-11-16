@echo off
rem Resolve the SambaMini environment's interpreter.  Override with SAMBA_MINI_PYTHON.
if defined SAMBA_MINI_PYTHON (
    set "SM_PY=%SAMBA_MINI_PYTHON%"
    goto :eof
)
set "SM_PY=%USERPROFILE%\miniconda3\envs\SambaMini\python.exe"
if exist "%SM_PY%" goto :eof
set "SM_PY=%ProgramData%\miniconda3\envs\SambaMini\python.exe"
if exist "%SM_PY%" goto :eof
set "SM_PY=%USERPROFILE%\anaconda3\envs\SambaMini\python.exe"
if exist "%SM_PY%" goto :eof
echo SambaMini Python environment not found. Run scripts\setup_env.ps1 or set SAMBA_MINI_PYTHON.
set "SM_PY="
