@echo off
REM B2Wave AI agent worker (LiveKit Agents + OpenAI Realtime)
cd /d "%~dp0"
if not exist ".env" (
  echo.
  echo  agent\.env is missing. Open B2Wave ^> AI Agent, enter your LiveKit and OpenAI keys and click Save.
  echo.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Setting up the agent environment, first run only...
  py -3.12 -m venv .venv 2>nul || python -m venv .venv || goto :err
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || goto :err
)
echo.
echo  B2Wave AI agent is running. Keep this window open while making AI calls.
echo.
".venv\Scripts\python.exe" agent.py start
goto :eof
:err
echo.
echo Setup failed. Install Python 3.10-3.12 and try again.
pause
