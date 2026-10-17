@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install Python 3 and enable Add Python to PATH.
    pause
    exit /b 1
)

echo Installing project dependencies...
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto install_error

echo Installing the Chromium browser...
python -m playwright install chromium
if errorlevel 1 goto install_error

echo.
echo Done. Now run "PageWeaver.bat".
pause
exit /b 0

:install_error
echo.
echo Installation failed. Check your internet connection and try again.
pause
exit /b 1