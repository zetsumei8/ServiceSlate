@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" configure_office_network.py
) else (
  python configure_office_network.py
)
echo.
pause
