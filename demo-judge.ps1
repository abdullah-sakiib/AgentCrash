# AgentCrash offline judge demo: fresh deterministic run + scorecard sidecar.
# Never overwrites history: demo.ps1 always writes a new timestamped file,
# and the scorecard CLI refuses to overwrite existing sidecars.
# No live model calls, no credentials.
$ErrorActionPreference = "Stop"

function Select-NewestDemoReport {
    <#
    .SYNOPSIS
        Newest demo *report* in a directory — never a *.scorecard.json sidecar.
    .DESCRIPTION
        Report selection must exclude scorecard sidecars: "*.scorecard.json"
        also matches the "demo-hackathon-offline-*.json" glob, so without the
        filter a repeat run could feed a sidecar back into the scorecard CLI.
        Comparison inputs are report artifacts; the comparison endpoint builds
        scorecards from them itself.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ArtifactsDir,
        [string[]]$ExcludeFullNames = @()
    )
    Get-ChildItem (Join-Path $ArtifactsDir "demo-hackathon-offline-*.json") -ErrorAction SilentlyContinue |
        Where-Object { ($_.Name -notlike "*.scorecard.json") -and ($ExcludeFullNames -notcontains $_.FullName) } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

if ($MyInvocation.InvocationName -ne '.') {
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

$python = ".\venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error 'Python venv not found at .\venv\Scripts\python.exe. Run: py -3.12 -m venv venv; then .\venv\Scripts\python.exe -m pip install -e ".[test]"'
    exit 1
}

.\demo.ps1
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$newest = Select-NewestDemoReport -ArtifactsDir ".\artifacts"
if (-not $newest) {
    Write-Error "No demo artifact found after running demo.ps1."
    exit 1
}

& $python -m agentcrash.experiments.scorecard --input $newest.FullName
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$sidecar = Join-Path $newest.DirectoryName ($newest.BaseName + ".scorecard.json")

$previous = Select-NewestDemoReport -ArtifactsDir ".\artifacts" -ExcludeFullNames @($newest.FullName)

Write-Host ""
Write-Host "Judge tour (open http://127.0.0.1:8000 - start it with .\start-ui.ps1):"
Write-Host "  1. Dashboard: what AgentCrash measures and recent reports."
Write-Host "  2. Scenarios: the three failure modes, faults, and invariants."
Write-Host ("  3. Report: /experiments/{0} - unsafe vs safe trials, traces, evaluation evidence." -f $newest.Name)
Write-Host ("  4. Scorecard: /scorecards/{0} - recovery, violations, duplicates, sample honesty." -f [System.IO.Path]::GetFileName($sidecar))
if ($previous) {
    $encodedBase = [System.Uri]::EscapeDataString($previous.Name)
    $encodedCand = [System.Uri]::EscapeDataString($newest.Name)
    Write-Host ("  5. Compare: /compare?baseline={0}&candidate={1} - compatible runs, deltas, regression check." -f $encodedBase, $encodedCand)
} else {
    Write-Host "  5. Compare: run this script once more, then compare any two demo reports under /compare."
}
}
