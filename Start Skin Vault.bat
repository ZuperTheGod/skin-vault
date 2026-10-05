@echo off
setlocal
title Skin Vault - LoL Skin Mod Manager
cd /d "%~dp0"

rem ---- find Python 3.9+ (prefer the py launcher) ----
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo  Python 3.9 or newer was not found.
  echo  Opening the download page - install it, tick "Add python.exe to PATH", then run this file again.
  echo.
  start "" https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

rem ---- one-time dependencies (quiet; skipped if already installed) ----
%PY% -c "import zstandard, xxhash" >nul 2>nul || (
  echo Installing dependencies ^(one-time^)...
  %PY% -m pip install --user --quiet --disable-pip-version-check -r requirements.txt
)

echo Starting Skin Vault... your browser will open at http://127.0.0.1:8765
echo Close this window to stop it.
%PY% skin_manager.py %*
if errorlevel 1 (
  echo.
  echo Skin Vault stopped with an error. See the logs folder, or open an issue on GitHub with the message above.
  pause
)
