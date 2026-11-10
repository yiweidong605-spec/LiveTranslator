@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set "PY=%~dp0env\python.exe"
if not exist "%PY%" set "PY=%~dp0env\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" scripts\package.py
start "" "%~dp0dist"
pause
