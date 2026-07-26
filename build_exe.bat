@echo off
REM ============================================================
REM  AutoStock Editor - build a standalone .exe with an icon
REM  Run this file once. Result: dist\AutoStock Editor\AutoStock Editor.exe
REM ============================================================
cd /d "%~dp0"

echo Installing PyInstaller...
pip install pyinstaller

echo.
echo Building (this takes several minutes - Whisper/PyTorch are large)...
pyinstaller --noconfirm --noconsole --name "AutoStock Editor" ^
  --icon icon.ico ^
  --collect-all customtkinter ^
  --collect-all whisper ^
  --add-data "icon.ico;." ^
  main.py

echo.
echo ============================================================
echo Done! Your app:  dist\"AutoStock Editor"\"AutoStock Editor.exe"
echo Right-click the .exe ^> Send to ^> Desktop (create shortcut).
echo NOTE: FFmpeg must still be installed system-wide (winget install ffmpeg).
echo ============================================================
pause
