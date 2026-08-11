@echo off
setlocal
cd /d "%~dp0backend"

echo ============================================
echo  Naver Webtoon Translator - first-time setup
echo ============================================
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo Python wasn't found on this computer.
    echo Install it from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH" during install^), then run this again.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Creating a Python environment for the backend...
    py -3 -m venv .venv
)

echo Installing dependencies - this can take a few minutes the first time...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo Something went wrong installing dependencies - scroll up for the error.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" setup_env.py

echo.
echo ============================================
echo  Setup complete!
echo  Next: double-click start.bat, then load the
echo  extension in Chrome - see README.md.
echo ============================================
pause
