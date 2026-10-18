@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo PageWeaver diagnostics. It can take 10-20 minutes. Please do not close this window.
echo pageweaver.py is not changed; results go to the run_... folder here.
echo.
set DEBUG=pw:api
python diag.py %* 2> playwright_debug_log.txt
echo.
echo Finished. You can close this window.
pause
