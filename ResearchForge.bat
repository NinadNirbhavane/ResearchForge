@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo ResearchForge cannot start: .venv\Scripts\python.exe was not found.
  echo Follow the setup section of USER_GUIDE.md, then run this file again.
  echo.
  pause
  exit /b 1
)

.venv\Scripts\python.exe -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=1)" >nul 2>&1
if %errorlevel%==0 (
  echo ResearchForge is already running. Opening the workspace...
  start "" "http://127.0.0.1:8765/"
  exit /b 0
)

echo.
echo Starting ResearchForge. A browser window will open.
echo Keep this window open while you work.
echo Press Ctrl+C, or close this window, to stop the local server.
echo.
.venv\Scripts\python.exe -m app.main ui --app-window
set EXITCODE=%errorlevel%
echo.
echo ResearchForge has stopped.
if not "%EXITCODE%"=="0" echo Startup returned code %EXITCODE%. See USER_GUIDE.md troubleshooting.
pause
exit /b %EXITCODE%
