@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Testing the updated PageWeaver in all three modes (split, full, reopen).
echo It can take about 15 minutes. Please do not close this window.
echo.
python test_modes.py %*
echo.
echo Finished. You can close this window.
pause
