@echo off
REM Makes the KDP Agent start by itself every time you log in to Windows.
REM (Same as doing it by hand in Task Scheduler - see README.md "Auto-start".)
cd /d "%~dp0"
schtasks /Create /TN "KDP Agent" /TR "\"%~dp0start.bat\"" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (echo Could not create the task. Try right-clicking this file and "Run as administrator". & pause & exit /b 1)
echo.
echo Done! The KDP Agent will start by itself when you log in.
echo To remove it later, run: schtasks /Delete /TN "KDP Agent" /F
pause
