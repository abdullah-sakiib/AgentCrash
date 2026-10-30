# AgentCrash hackathon demo (offline, deterministic).
# Runs all three business-safety scenarios with the orchestrated experiment
# runner in default deterministic mode. Makes NO live API calls and never
# touches historical validation artifacts.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = ".\artifacts\demo-hackathon-offline-$stamp.json"
if (Test-Path $out) {
    Write-Error "Refusing to overwrite existing report: $out"
    exit 1
}

.\venv\Scripts\python.exe -m agentcrash.experiments.run_experiment --output $out
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Demo report saved (offline deterministic run):"
Write-Host "  $out"
Write-Host "Inspect per-scenario outcomes, invariant violations, and traces with:"
Write-Host "  Get-Content $out | ConvertFrom-Json | Select-Object -ExpandProperty aggregate"
