@echo off
REM ===== Phantasia Advisor: one window, game inside =====
cd /d "%~dp0"
if not exist "launcher.py" (
    echo ERROR: launcher.py not found in %CD%
    pause
    exit /b 1
)

REM Use the SAME Python that Phantasia-debug.bat uses, just its no-console
REM twin (pythonw.exe in the same folder). A separate "pythonw" on PATH can
REM belong to a different Python install that's missing pieces.
set "PYW="
where python >nul 2>nul
if errorlevel 1 goto try_py
for /f "usebackq delims=" %%i in (`python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"`) do set "PYW=%%i"
if defined PYW if exist "%PYW%" goto run_pyw
REM no pythonw next to python: run python with its console minimized
start "Phantasia Advisor" /min python launcher.py
exit /b 0

:try_py
where py >nul 2>nul
if errorlevel 1 goto no_python
for /f "usebackq delims=" %%i in (`py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"`) do set "PYW=%%i"
if defined PYW if exist "%PYW%" goto run_pyw
start "Phantasia Advisor" /min py -3 launcher.py
exit /b 0

:run_pyw
start "" "%PYW%" launcher.py
exit /b 0

:no_python
echo Python was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
pause
exit /b 1
