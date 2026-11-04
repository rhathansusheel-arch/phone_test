@echo off
REM Starts the KDP Agent. Keep this window open. Press Ctrl+C to stop it.
cd /d "%~dp0"
.venv\Scripts\python main.py
pause
