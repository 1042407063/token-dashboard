@echo off
rem Single-window launcher.
rem ASCII only: cmd reads .bat as ANSI(GBK); UTF-8 Chinese would be garbled and
rem can even be executed by mistake (hit twice already). All Chinese text lives
rem in tools\launch.py, which is UTF-8 and printed by Python.
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

"%PY%" "%~dp0tools\launch.py"
exit /b %errorlevel%
