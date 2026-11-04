@echo off
REM Runs the 7 safety tests and sends the checklist to your Telegram.
cd /d "%~dp0"
.venv\Scripts\python safety_check.py
pause
