@echo off
setlocal
cd /d "%~dp0"
echo.
echo  ServiceSlate Setup
echo  -----------------
echo  This sets up ServiceSlate on this computer.
echo  Your company data is stored separately so app updates do not erase it.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\Install-ServiceSlate.ps1"
if errorlevel 1 (
  echo.
  echo ServiceSlate setup could not finish. No company data was deleted.
  echo.
  pause
  exit /b 1
)
echo.
echo ServiceSlate is ready.
echo.
pause
endlocal
