@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0install_nc.py" %*
) else (
  python "%~dp0install_nc.py" %*
)
set "NC_RESULT=%errorlevel%"
if not "%NC_RESULT%"=="0" echo NC installation failed with exit code %NC_RESULT%.
endlocal & exit /b %NC_RESULT%
