@echo off
REM B2Wave - one-click launcher (creates a venv on first run)
cd /d "%~dp0app"
if not exist ".venv\Scripts\python.exe" (
  echo Setting up Python environment, first run only...
  python -m venv .venv || goto :err
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || goto :err
)
".venv\Scripts\python.exe" gsm_app.py %*
goto :eof
:err
echo.
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
