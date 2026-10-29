@echo off
setlocal
cd /d "%~dp0"
title 雨课堂 AI 参考助手

:menu
cls
echo ==================================================
echo    长江雨课堂 · AI 参考助手
echo ==================================================
echo    1. 扫码登录（首次使用 / 登录过期）
echo    2. 开始监控（默认 45 分钟）
echo    3. 按时间窗口监控（例如 08:00-09:40）
echo    4. 本地模拟测试（不用登录，用来验证流程）
echo    5. 调试：保存当前页面快照（抓不到题时用）
echo    0. 退出
echo ==================================================
set "choice="
set /p "choice=请输入数字后回车："
if "%choice%"=="1" goto login
if "%choice%"=="2" goto watch
if "%choice%"=="3" goto window
if "%choice%"=="4" goto mock
if "%choice%"=="5" goto dump
if "%choice%"=="0" goto end
goto menu

:login
cls
echo [登录] 会弹出 Edge，请用手机雨课堂扫码；登录成功后窗口自动关闭。
echo.
python ykt_watcher.py --login
echo.
pause
goto menu

:watch
cls
set "mins="
set /p "mins=监控多少分钟？（直接回车 = 45）："
if "%mins%"=="" set "mins=45"
echo.
echo [监控] 盯 %mins% 分钟。运行期间请不要关闭浏览器窗口。
echo.
python ykt_watcher.py --duration %mins% --kill-stale
echo.
pause
goto menu

:window
cls
set "win="
set /p "win=请输入时间窗口（格式 08:00-09:40）："
if "%win%"=="" goto menu
echo.
echo [监控] 只在 %win% 内盯，到点自动退出。
echo.
python ykt_watcher.py --window %win% --kill-stale
echo.
pause
goto menu

:mock
cls
echo [模拟] 使用本地 mock_quiz.html：第 1 题 45 秒后自动切到第 2 题（多选）。
echo.
python ykt_watcher.py --mock --mock-query "t=45&auto=1" --duration 2
echo.
pause
goto menu

:dump
cls
echo [调试] 浏览器打开后，请自己进入「有题目」的页面，然后回这个窗口按回车。
echo.
python ykt_watcher.py --dump
echo.
pause
goto menu

:end
endlocal
