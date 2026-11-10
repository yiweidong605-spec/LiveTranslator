@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "HF_HOME=%~dp0.cache\huggingface"
set "OLLAMA_MODELS=%~dp0models\ollama"
set "PIP_CACHE_DIR=%~dp0.cache\pip"
set MLFLOW_DISABLE_AGENT_HINT=1
title LiveTranslator Eval
set "PY=%~dp0env\python.exe"
if not exist "%PY%" set "PY=%~dp0env\Scripts\python.exe"
if not exist "%PY%" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)

:menu
echo.
echo  ======== LiveTranslator Evaluation ========
echo   1. Install eval tools + download candidate models (once)
echo   2. Translation eval - all candidates, full test set
echo   3. Translation eval - quick test (first 40 sentences)
echo   4. Translation eval + COMET score (needs COMET from step 1)
echo   5. Speech recognition eval (synthetic voice + your recordings)
echo   6. Open MLflow dashboard (platform: http://swt17:5000)
echo   7. Rescore the latest run with the current test set (no re-translation)
echo   0. Exit
echo.
set "CH="
set /p CH="Choose: "
if "%CH%"=="1" "%PY%" scripts\eval_setup.py
if "%CH%"=="2" "%PY%" scripts\eval_mt.py
if "%CH%"=="3" "%PY%" scripts\eval_mt.py --limit 40
if "%CH%"=="4" "%PY%" scripts\eval_mt.py --comet
if "%CH%"=="5" "%PY%" scripts\eval_asr.py --synth
if "%CH%"=="6" "%PY%" scripts\mlflow_ui.py
if "%CH%"=="7" "%PY%" scripts\eval_mt.py --rescore latest
if "%CH%"=="0" exit /b 0
goto menu
