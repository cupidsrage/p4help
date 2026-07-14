@echo off
REM ===== Phantasia Advisor =====
cd /d "%~dp0"

if not exist "app.py"     ( echo ERROR: app.py not found in %CD% & pause & exit /b 1 )
if not exist "overlay.py" ( echo ERROR: overlay.py not found in %CD% & pause & exit /b 1 )

echo Starting proxy...
start "Phantasia Proxy" cmd /k python app.py -h 178.63.136.86
timeout /t 2 >nul

echo Starting overlay...
REM pythonw = no console window for the overlay
start "" pythonw overlay.py
if errorlevel 1 start "" python overlay.py

echo.
echo   Overlay is up (top-left). Drag its title bar over the game.
echo   F8 hide/show  ·  right-click for opacity  ·  Esc to close.
echo.
echo   Full dashboard if you want it: http://127.0.0.1:8420
echo.
exit
