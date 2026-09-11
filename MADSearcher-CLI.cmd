@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Missing Python runtime. Run scripts\setup.ps1 first. 1>&2
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -u "%~dp0scripts\cli.py" %*
exit /b %errorlevel%
