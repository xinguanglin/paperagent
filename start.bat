@echo off
rem paper-agent launcher (public). MySQL must be reachable per config.yaml
rem (docker compose or your own instance). Requires Python env from setup.bat.
cd /d %~dp0

if not exist config.yaml (
    echo [error] config.yaml not found. Run setup.bat first.
    pause
    exit /b 1
)

if not exist web\dist (
    echo [info] Frontend not built yet, building with Node.js + pnpm ...
    call pnpm --dir web install
    call pnpm --dir web build
)

echo Starting paper-agent at http://localhost:8787
start "" http://localhost:8787
cd server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
pause
