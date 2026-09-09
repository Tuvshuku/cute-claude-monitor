# Run the collector and widget directly on Windows without WSL.
[CmdletBinding()]
param(
    [switch]$CollectorOnly
)

$ErrorActionPreference = 'Stop'
$collectorPath = Join-Path $PSScriptRoot 'collector.py'
$widgetPath = Join-Path $PSScriptRoot 'widget.ps1'
$dataPath = Join-Path $env:USERPROFILE '.claude-widget\usage.json'

$python = Get-Command 'pythonw.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $python) {
    $python = Get-Command 'python.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
}
if (-not $python) {
    throw 'Python 3 was not found. Install it from python.org and enable "Add Python to PATH".'
}

$quotedCollector = '"{0}" --loop' -f ($collectorPath -replace '"', '\"')
$collectorProcess = Start-Process -FilePath $python.Source -ArgumentList $quotedCollector `
    -WindowStyle Hidden -PassThru

try {
    if ($CollectorOnly) {
        Wait-Process -Id $collectorProcess.Id
    } else {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden `
            -File $widgetPath -DataPath $dataPath -NativeMode -CollectorDataDir $PSScriptRoot
    }
} finally {
    if ($collectorProcess -and -not $collectorProcess.HasExited) {
        Stop-Process -Id $collectorProcess.Id -Force
    }
}

