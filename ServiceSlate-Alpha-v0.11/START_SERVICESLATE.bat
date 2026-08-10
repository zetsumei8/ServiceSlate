@echo off
setlocal
cd /d "%~dp0"
set APPVER=0.9.0

where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set PY=python
  ) else (
    echo.
    echo ServiceSlate needs Python 3.11 or newer for this ServiceSlate preview.
    echo Install Python once, then double-click this file again.
    echo.
    pause
    exit /b 1
  )
)

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo This computer has an older version of Python.
  echo ServiceSlate needs Python 3.11 or newer.
  echo Your data has not been changed.
  echo.
  pause
  exit /b 1
)

if not exist .venv (
  echo.
  echo Setting up ServiceSlate for the first time...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)

call .venv\Scripts\activate.bat
if not exist .venv\.serviceslate-%APPVER%-ready (
  echo Preparing this ServiceSlate version...
  python -m pip install --disable-pip-version-check -q -e .
  if errorlevel 1 goto :fail
  type nul > .venv\.serviceslate-%APPVER%-ready
)

echo Opening ServiceSlate...
python run_serviceslate.py
goto :end

:fail
echo.
echo ServiceSlate could not finish setup. Your local data was not deleted.
echo If this is the first setup, make sure this computer can reach the internet once,
echo then double-click START_SERVICESLATE again.
echo.
pause
:end
endlocal
