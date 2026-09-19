@echo off
cd /d %~dp0
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
%PY% -m playwright install chromium
echo.
echo Install finished. Now double-click  run_windows.bat  to start the tool.
pause
