@echo off
rem Start ark-image-mcp over HTTP and front it with a Cloudflare Tunnel.
rem Close this window to stop both processes.
rem
rem Machine-specific settings belong in .env.http (gitignored), e.g.:
rem   CLOUDFLARED_BIN=C:\path\to\cloudflared.exe
rem   TUNNEL_CONFIG=C:\Users\you\.cloudflared\config.yml
rem   TUNNEL_NAME=ark-image
setlocal
cd /d "%~dp0"

if not exist ".env.http" (
  echo [error] .env.http not found next to this script.
  echo         Create it with ARK_API_KEY / ARK_TRANSPORT settings first.
  pause
  exit /b 1
)

rem 1) Load credentials, transport, and any tunnel overrides
for /f "usebackq tokens=1,* delims==" %%a in (".env.http") do set "%%a=%%b"

rem 2) Defaults for anything left unset
if "%CLOUDFLARED_BIN%"=="" set "CLOUDFLARED_BIN=cloudflared"
if "%TUNNEL_CONFIG%"=="" set "TUNNEL_CONFIG=%USERPROFILE%\.cloudflared\config.yml"
if "%TUNNEL_NAME%"=="" set "TUNNEL_NAME=ark-image"

rem 3) Start the MCP HTTP server in its own window
start "ark-image-mcp server" "%~dp0.venv\Scripts\python.exe" "%~dp0server.py"

rem 4) Give it a moment, then run the tunnel in this window
timeout /t 3 /nobreak >nul
echo Starting Cloudflare tunnel "%TUNNEL_NAME%" ...
"%CLOUDFLARED_BIN%" tunnel --config "%TUNNEL_CONFIG%" run "%TUNNEL_NAME%"

rem When the tunnel exits, also stop the server window.
taskkill /FI "WINDOWTITLE eq ark-image-mcp server*" /F >nul 2>&1
