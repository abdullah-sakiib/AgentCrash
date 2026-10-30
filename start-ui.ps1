# AgentCrash local UI startup (offline demo, single process).
# Starts uvicorn serving the FastAPI simulator + read-only UI API and the
# built React frontend. No live model calls, no credentials, no Node.js
# required at runtime (only to build the frontend, via -Build).
param(
    [switch]$Build,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

$python = ".\venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error 'Python venv not found at .\venv\Scripts\python.exe. Run: py -3.12 -m venv venv; then .\venv\Scripts\python.exe -m pip install -e ".[test]"'
    exit 1
}

$distIndex = ".\frontend\dist\index.html"
if (-not (Test-Path $distIndex)) {
    if ($Build) {
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            Write-Error "npm is not on PATH. Install Node 22 LTS, then run: cd frontend; npm install; npm run build"
            exit 1
        }
        Set-Location ".\frontend"
        npm install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        npm run build
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Set-Location ".."
    } else {
        Write-Error "Frontend build not found at $distIndex. Build it with: cd frontend; npm install; npm run build  (or re-run with -Build)"
        exit 1
    }
} else {
    $newestSrc = Get-ChildItem -Recurse ".\frontend\src" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $builtAt = (Get-Item $distIndex).LastWriteTime
    if ($newestSrc -and $newestSrc.LastWriteTime -gt $builtAt) {
        Write-Host "Warning: frontend/src is newer than the build output. Re-run with -Build to refresh it." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Starting AgentCrash UI on http://127.0.0.1:$Port"
Write-Host "Judge tour: Dashboard -> Scenarios -> Experiments -> report detail -> scorecard -> Compare."
Write-Host "Press Ctrl+C to stop. No live calls are made; all data stays local."
Write-Host ""
& $python -m uvicorn agentcrash.main:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
