@echo off
setlocal
cd /d "%~dp0"

rem --- Key injection into most games needs admin. Re-launch elevated if needed.
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%~dp0'"
    exit /b
)

rem --- Pick a Python: prefer the py launcher, fall back to python on PATH.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo Python not found. Install it from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" during setup.
    pause
    exit /b 1
)

rem --- Install missing dependencies on first run.
%PY% -c "import bleak, pydirectinput" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed.
        pause
        exit /b 1
    )
)

%PY% zwift_click_mapper.py
pause
