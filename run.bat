@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === OpenMacDisk ===
echo Native Windows desktop app (wxWidgets). No browser, no local server.
python -c "import wx" 2>nul || (
  echo Installing wxPython...
  python -m pip install -q wxPython || goto :fail
)
python -X utf8 desktop.py %*
if errorlevel 1 goto :fail
exit /b 0
:fail
echo.
echo Failed to start. See messages above.
pause
exit /b 1
