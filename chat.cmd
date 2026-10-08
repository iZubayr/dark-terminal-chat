@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -m dark_terminal_chat %*
) else (
  python -m dark_terminal_chat %*
)
if errorlevel 1 pause
