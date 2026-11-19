@echo off
rem Register the panel to start at logon.
rem Puts a small launcher into the current user's Startup folder.
rem No admin rights, no registry access.
setlocal
cd /d "%~dp0"

set "PY="
rem 1) WorkBuddy bundled interpreter
for /d %%d in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
  if not defined PY if exist "%%d\python.exe" set "PY=%%d\python.exe"
)
rem 2) python from PATH, skipping the 0-byte Microsoft Store alias stub
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do (
  if not defined PY for %%j in ("%%i") do if %%~zj GTR 0 set "PY=%%i"
)
if not defined PY (
  echo.
  echo   Python not found. Install Python 3, or reinstall WorkBuddy.
  echo.
  pause
  exit /b 1
)

rem Pass the interpreter explicitly: the autostart entry must not inherit
rem whatever python happened to launch this script.
"%PY%" "%~dp0tools\autostart.py" install --python "%PY%"
if errorlevel 1 (
  echo Install failed.
  pause
  exit /b 1
)
echo.
pause
exit /b 0
