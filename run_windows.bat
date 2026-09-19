@echo off
cd /d %~dp0
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% main.py
if errorlevel 1 pause
