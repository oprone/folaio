@echo off
rem Folaio for Windows: double-click to start. The first time, it sets itself up.
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto start
echo Setting up Folaio for the first time. This takes a minute or two...
py -3 -m venv .venv 2>nul || python -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" goto nopython
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto failed
echo Done.

:start
echo Starting Folaio... Keep this window open; close it to stop Folaio.
".venv\Scripts\python.exe" run.py
pause
exit /b 0

:nopython
echo.
echo Python 3 is needed. Download it from https://www.python.org/downloads/
echo When installing, tick "Add python.exe to PATH". Then double-click Start Folaio again.
pause
exit /b 1

:failed
rmdir /s /q .venv
echo.
echo Setup did not finish. Check your internet connection and try again.
pause
exit /b 1
