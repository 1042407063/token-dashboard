@echo off
rem Remove the autostart entry. Does not delete any data.
setlocal
cd /d "%~dp0"

set "PY="
for /d %%d in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
  if not defined PY if exist "%%d\python.exe" set "PY=%%d\python.exe"
)
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do (
  if not defined PY for %%j in ("%%i") do if %%~zj GTR 0 set "PY=%%i"
)
if not defined PY (
  echo Python not found.
  exit /b 1
)

"%PY%" "%~dp0tools\autostart.py" uninstall
exit /b %errorlevel%
