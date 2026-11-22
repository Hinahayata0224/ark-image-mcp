@echo off
rem Start ark-image-mcp over streamable HTTP so phones on the same Wi-Fi can
rem reach it. Reads credentials from .env.http (gitignored).
setlocal
cd /d "%~dp0"
if not exist ".env.http" (
  echo [error] .env.http not found next to this script.
  exit /b 1
)
for /f "usebackq tokens=1,* delims==" %%a in (".env.http") do set "%%a=%%b"
echo Serving on http://0.0.0.0:%ARK_PORT%/mcp  (token auth required)
"%~dp0.venv\Scripts\python.exe" server.py
