@echo off
rem Remove the autostart entry. Does not delete any data.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 not found.
  exit /b 1
)

python "%~dp0tools\autostart.py" uninstall
exit /b 0
