@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -m dark_terminal_chat.server %*
) else (
  python -m dark_terminal_chat.server %*
)
if errorlevel 1 pause
