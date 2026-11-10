@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PIP_CACHE_DIR=%~dp0.cache\pip"
title LiveTranslator Sync
set "PY=%~dp0env\python.exe"
if not exist "%PY%" set "PY=%~dp0env\Scripts\python.exe"
if not exist "%PY%" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
rem Runs only when you start it: no background process, no scheduled task.
"%PY%" -c "import boto3" 2>nul || (
  echo Installing boto3 ^(one time^) ...
  "%PY%" -m pip install -q boto3
)

:menu
echo.
echo  ======== LiveTranslator Sync (to the mini PC) ========
echo   1. Sync now (upload conversations, start processing, pull backup)
echo   2. Preview: what would be uploaded
echo   3. Check connection and permissions
echo   4. Set up backups to this PC (one time)
echo   0. Exit
echo.
set "CH="
set /p CH="Choose [1]: "
if "%CH%"=="" set "CH=1"
if "%CH%"=="1" "%PY%" scripts\sync_logs.py
if "%CH%"=="2" "%PY%" scripts\sync_logs.py --dry-run
if "%CH%"=="3" "%PY%" scripts\sync_logs.py --check
if "%CH%"=="4" "%PY%" scripts\backup_pull.py --setup
if "%CH%"=="0" exit /b 0
goto menu
