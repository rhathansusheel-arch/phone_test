@echo off
REM One-time setup: makes the Python environment and installs the packages in requirements.txt.
cd /d "%~dp0"
py -3.12 -m venv .venv || (echo Python 3.12 not found. Install it first. & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
echo.
echo Setup finished. Next: fill in .env (see the steps Claude gave you).
pause
