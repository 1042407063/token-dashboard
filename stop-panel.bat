@echo off
rem Stop the running panel. Data and index are kept.
setlocal
cd /d "%~dp0"

if not exist "data\panel.pid" (
  echo Panel is not running.
  exit /b 0
)

set /p PID=<"data\panel.pid"
if "%PID%"=="" (
  del /q "data\panel.pid" >nul 2>nul
  echo Pid file was empty, cleaned up.
  exit /b 0
)

taskkill /PID %PID% /F >nul 2>nul
if errorlevel 1 (
  echo Process %PID% was not running.
) else (
  echo Panel stopped, pid %PID%.
)
del /q "data\panel.pid" >nul 2>nul
exit /b 0
