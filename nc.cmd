@echo off
setlocal
set "NC_DIR=%~dp0"
if exist "%NC_DIR%.venv\Scripts\python.exe" (
  "%NC_DIR%.venv\Scripts\python.exe" "%NC_DIR%nc_console.py" %*
) else (
  py -3 "%NC_DIR%nc_console.py" %*
)
set "NC_RESULT=%errorlevel%"
endlocal & exit /b %NC_RESULT%
