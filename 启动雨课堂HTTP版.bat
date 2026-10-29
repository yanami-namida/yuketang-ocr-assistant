@echo off
setlocal
cd /d "%~dp0"
title Rain Classroom HTTP
set "YKT_PYTHON=%~dp0.venv-http\Scripts\python.exe"
if not exist "%YKT_PYTHON%" set "YKT_PYTHON=python"
if "%~1"=="" goto menu
"%YKT_PYTHON%" -B ykt_http.py %*
exit /b %errorlevel%
:menu
"%YKT_PYTHON%" -B ykt_http_menu.py
exit /b %errorlevel%
