@echo off
REM ===== Phantasia Advisor: one window, game inside =====
cd /d "%~dp0"
if not exist "launcher.py" ( echo ERROR: launcher.py not found in %CD% & pause & exit /b 1 )
REM pythonw = no console window
start "" pythonw launcher.py
if errorlevel 1 start "" python launcher.py
exit
