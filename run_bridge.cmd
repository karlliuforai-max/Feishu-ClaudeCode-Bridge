@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

cd /d "%~dp0"
title Feishu Agent Gateway

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

echo ================================================
echo   Feishu Agent Gateway
echo   Directory: %CD%
echo   Stop: close this window, or press Ctrl-C
echo ================================================
echo.

if not exist "config.json" (
  echo [ERROR] Missing config.json in this directory.
  echo Please create it from config.example.json first.
  echo.
  pause
  exit /b 1
)

set "PYTHON_BIN="
if exist "C:\Python314\python.exe" set "PYTHON_BIN=C:\Python314\python.exe"
if not defined PYTHON_BIN (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_BIN=python"
)
if not defined PYTHON_BIN (
  where py >nul 2>nul
  if not errorlevel 1 set "PYTHON_BIN=py -3"
)
if not defined PYTHON_BIN (
  echo [ERROR] Python was not found.
  echo Install Python, or add python.exe to PATH.
  echo.
  pause
  exit /b 1
)

set "PID_FILE=logs\bridge.pid"
if exist "%PID_FILE%" (
  set /p OLD_PID=<"%PID_FILE%"
  if defined OLD_PID (
    set "BRIDGE_RUNNING="
    for /f "usebackq delims=" %%R in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p = Get-Process -Id !OLD_PID! -ErrorAction Stop; if ($p.ProcessName -like 'python*') { 'running' } } catch {}"`) do set "BRIDGE_RUNNING=%%R"
    if defined BRIDGE_RUNNING (
      echo Existing background bridge process detected: PID !OLD_PID!
      choice /C YN /M "Stop it before starting this foreground window"
      if errorlevel 2 (
        echo Cancelled. Existing service is still running.
        pause
        exit /b 1
      )
      powershell -NoProfile -ExecutionPolicy Bypass -Command "Stop-Process -Id !OLD_PID! -Force -ErrorAction SilentlyContinue" >nul 2>nul
      timeout /t 2 /nobreak >nul
      echo Stopped old background process.
      echo.
    )
  )
)

:run
echo Starting bridge with: %PYTHON_BIN% -u src\feishu_agent_bridge.py
echo.
%PYTHON_BIN% -u src\feishu_agent_bridge.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
rem No-restart codes: 0=normal 2=fatal config/deps 130=Ctrl-C 9009=python not found.
rem Code 1 (unhandled crash / network layer) is NOT here on purpose: that one auto-restarts.
if "%EXIT_CODE%"=="0" goto stopped
if "%EXIT_CODE%"=="2" goto stopped
if "%EXIT_CODE%"=="130" goto stopped
if "%EXIT_CODE%"=="9009" goto stopped

echo Bridge exited unexpectedly with code %EXIT_CODE%.
echo Restarting in 3 seconds. Close this window to stop.
timeout /t 3 /nobreak >nul
echo.
goto run

:stopped
echo Bridge stopped with code %EXIT_CODE%.
echo Press any key to close this window.
pause >nul
