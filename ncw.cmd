@echo off
setlocal
set "NC_DIR=%~dp0"
if exist "%NC_DIR%.venv\Scripts\python.exe" (
  "%NC_DIR%.venv\Scripts\python.exe" "%NC_DIR%nc_twin_run.py" %*
) else (
  py -3.12 "%NC_DIR%nc_twin_run.py" %*
)
set "NC_RESULT=%errorlevel%"
endlocal & exit /b %NC_RESULT%
