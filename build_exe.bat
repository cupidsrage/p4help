@echo off
REM ===== Build Phantasia.exe (one file, no console) =====
REM Needs: pip install pyinstaller
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --noconsole --name Phantasia ^
  --add-data "ui.html;." launcher.py
if errorlevel 1 ( echo Build failed. & pause & exit /b 1 )
echo.
echo   Built dist\Phantasia.exe
echo   Put it anywhere (a desktop shortcut works) and double-click it.
echo   It saves its settings to launcher.json next to the .exe.
echo.
pause
