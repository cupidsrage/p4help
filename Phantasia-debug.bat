@echo off
REM ===== Run the launcher with a visible console, to see any error =====
cd /d "%~dp0"
echo Starting launcher with a visible console...
echo.
where python >nul 2>nul
if errorlevel 1 (
    py -3 launcher.py
) else (
    python launcher.py
)
echo.
echo ---- launcher exited with code %errorlevel% ----
echo If it failed, copy the text above (and launcher.log if it exists).
pause
