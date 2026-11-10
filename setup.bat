@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem keep every cache and model inside this folder (not on C:)
set "PIP_CACHE_DIR=%~dp0.cache\pip"
set "HF_HOME=%~dp0.cache\huggingface"
set "OLLAMA_MODELS=%~dp0models\ollama"
set "CONDA_PKGS_DIRS=%~dp0.cache\conda_pkgs"
title LiveTranslator Setup

echo.
echo  ==== LiveTranslator setup (offline real-time translator) ====
echo.

if exist "env\python.exe" goto haveenv
if exist "env\Scripts\python.exe" goto haveenv

set "CONDA="
for %%P in ("E:\miniconda\Scripts\conda.exe" "%USERPROFILE%\miniconda3\Scripts\conda.exe" "%USERPROFILE%\anaconda3\Scripts\conda.exe" "C:\ProgramData\miniconda3\Scripts\conda.exe" "C:\ProgramData\anaconda3\Scripts\conda.exe") do (
  if not defined CONDA if exist "%%~P" set "CONDA=%%~P"
)
if not defined CONDA for /f "delims=" %%C in ('where conda 2^>nul') do if not defined CONDA set "CONDA=%%C"

if defined CONDA (
  echo [1/3] Creating private Python 3.11 environment with conda: %CONDA%
  call "%CONDA%" create -y -p "%~dp0env" -c conda-forge --override-channels python=3.11
) else (
  echo [1/3] conda not found, using python venv
  where py >nul 2>nul && ( py -3.11 -m venv env || py -3 -m venv env ) || ( python -m venv env )
)

:haveenv
set "PY=%~dp0env\python.exe"
if not exist "%PY%" set "PY=%~dp0env\Scripts\python.exe"
if not exist "%PY%" (
  echo.
  echo  [ERROR] Could not create the Python environment.
  echo  Install Miniconda or Python 3.11 from python.org, then run setup.bat again.
  pause
  exit /b 1
)

echo.
echo [2/3] Installing Python packages ...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo  [ERROR] pip install failed. Check your internet connection and run setup.bat again.
  pause
  exit /b 1
)

echo.
echo [3/3] Downloading offline models ...
"%PY%" scripts\setup_wizard.py

echo.
pause
