@echo off
REM One-time setup: makes the Python environment, installs the packages and the Chromium browser.
cd /d "%~dp0"
py -3.12 -m venv .venv || (echo Python 3.12 not found. Install it first. & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || (echo Package install failed. & pause & exit /b 1)
.venv\Scripts\python -m playwright install chromium || (echo Browser install failed. & pause & exit /b 1)
if not exist .env copy .env.example .env
echo.
echo Setup finished. Next: fill in .env (see README.md, step 4).
pause
