@echo off
rem Configure the model API key for the Yuketang helper.
rem Double-click this file, paste your key, done. Nothing is uploaded.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY (
  echo.
  echo Python not found in PATH.
  echo Install Python 3.10+ from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

"%PY%" -B "%~dp0tools\set_api_key.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
