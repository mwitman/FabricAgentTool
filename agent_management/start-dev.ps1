param(
    [int]$BackendPort = 8092,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$RepoRoot = Split-Path $Root -Parent
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Frontend = Join-Path $Root "frontend"

if (-not (Test-Path $Python)) {
    throw "Python venv not found at $Python"
}

if (-not (Test-Path (Join-Path $Frontend "node_modules"))) {
    Write-Host "Installing frontend packages..."
    Push-Location $Frontend
    npm install
    Pop-Location
}

$BackendCommand = "Set-Location '$Root'; & '$Python' -m uvicorn backend.server:app --host 0.0.0.0 --port $BackendPort --reload"
$FrontendCommand = "Set-Location '$Frontend'; npm run dev -- --port $FrontendPort"

Start-Process powershell -ArgumentList @("-NoExit", "-Command", $BackendCommand) -WindowStyle Normal
Start-Process powershell -ArgumentList @("-NoExit", "-Command", $FrontendCommand) -WindowStyle Normal

Write-Host "Backend API:  http://localhost:$BackendPort"
Write-Host "Frontend dev: http://localhost:$FrontendPort"
Write-Host "Use the frontend dev URL while editing React files; it hot-reloads without npm run build."