@echo off
REM Runs all automatic tests (no KDP or Amazon needed). Takes a few minutes.
cd /d "%~dp0"
.venv\Scripts\python -m pytest -q
pause
