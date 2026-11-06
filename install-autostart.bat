@echo off
rem Register the panel to start at logon.
rem Puts a small launcher into the current user's Startup folder.
rem No admin rights, no registry access.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 not found. Install it first.
  pause
  exit /b 1
)

python "%~dp0tools\autostart.py" install
if errorlevel 1 (
  echo Install failed.
  pause
  exit /b 1
)
exit /b 0
