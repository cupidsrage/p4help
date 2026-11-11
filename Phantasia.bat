@echo off
REM ===== Phantasia Advisor: one window, game inside =====
cd /d "%~dp0"
if not exist "launcher.py" (
    echo ERROR: launcher.py not found in %CD%
    pause
    exit /b 1
)

REM pythonw = no console window. Errors pop up and are saved to launcher.log.
where pythonw >nul 2>nul
if not errorlevel 1 goto pythonw
where pyw >nul 2>nul
if not errorlevel 1 goto pyw
where python >nul 2>nul
if not errorlevel 1 goto python
where py >nul 2>nul
if not errorlevel 1 goto py

echo Python was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
pause
exit /b 1

:pythonw
start "" pythonw launcher.py
exit /b 0

:pyw
start "" pyw -3 launcher.py
exit /b 0

:python
REM no pythonw: keep a console so any error stays readable
python launcher.py
if errorlevel 1 pause
exit /b

:py
py -3 launcher.py
if errorlevel 1 pause
exit /b
