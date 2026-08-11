@echo off
setlocal
cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
    echo Backend isn't set up yet - double-click setup.bat first.
    pause
    exit /b 1
)

echo Starting the translator backend...
echo Keep this window open while you're reading webtoons.
echo Close it ^(or press Ctrl+C^) when you're done.
echo.
".venv\Scripts\python.exe" main.py
pause
