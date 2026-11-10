@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem keep every cache and model inside this folder (not on C:)
set "HF_HOME=%~dp0.cache\huggingface"
set "OLLAMA_MODELS=%~dp0models\ollama"
set HF_HUB_OFFLINE=1
title LiveTranslator
set "PY=%~dp0env\python.exe"
if not exist "%PY%" set "PY=%~dp0env\Scripts\python.exe"
if not exist "%PY%" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
"%PY%" -m app.server
if errorlevel 1 pause
