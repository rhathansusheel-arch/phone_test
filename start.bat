@echo off
REM Starts the KDP Agent and restarts it if it ever crashes. Close this window to stop it.
cd /d "%~dp0"
title KDP Agent
:loop
.venv\Scripts\python main.py
if exist stop.flag (del stop.flag & exit /b 0)
echo The agent stopped. Restarting in 30 seconds... (close this window to stop it for good)
timeout /t 30 /nobreak >nul
goto loop
