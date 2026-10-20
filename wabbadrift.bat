@echo off
rem Starts the local Wabbadrift server and opens it in your browser.
rem The first run creates a Python environment in .venv and installs the dependencies.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: creating the Python environment...
  python -m venv .venv || goto :setup_failed
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :setup_failed
)
".venv\Scripts\python.exe" -m wabbadrift.server %*
pause
exit /b

:setup_failed
echo.
echo Setup failed. Wabbadrift needs Python 3.10 or newer installed and on PATH.
if exist ".venv" rmdir /s /q ".venv"
pause
exit /b 1
