@echo off
rem Start the daily WeCom push loop. Close this window to stop.
rem ASCII only: cmd reads .bat as ANSI, UTF-8 Chinese breaks parsing.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if exist "data\notify.json" goto :run
echo.
echo notify.json not found. Configure the bot first:
echo     python notify.py --set-webhook YOUR_WEBHOOK_URL
echo.
echo Webhook comes from WeCom group -^> group robot -^> copy webhook.
echo.
pause
exit /b 1

:run
set "PY="
rem 1) WorkBuddy bundled interpreter
for /d %%d in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
  if not defined PY if exist "%%d\python.exe" set "PY=%%d\python.exe"
)
rem 2) python from PATH, skipping the 0-byte store alias stub
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do (
  if not defined PY for %%j in ("%%i") do if %%~zj GTR 0 set "PY=%%i"
)
if not defined PY goto :nopython

echo Interpreter: %PY%
start "token-push" "%PY%" "%~dp0notify.py" --loop
exit /b 0

:nopython
echo python.exe not found.
pause
exit /b 1
