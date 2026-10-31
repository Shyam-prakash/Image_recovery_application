<#
.SYNOPSIS
  Whole flow on Windows: set up the virtual environment (first run), import ZIPs, restore, build the report.

.EXAMPLE
  .\run.ps1 "C:\Users\me\Downloads\photos-001.zip" "C:\Users\me\Downloads\photos-002.zip"
.EXAMPLE
  .\run.ps1 -ZipDir "C:\Users\me\Downloads\drive"
.EXAMPLE
  .\run.ps1                      # no ZIPs: restore what has already been imported
.EXAMPLE
  .\run.ps1 -Ui                  # start the browser UI instead
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Zip = @(),
    [string[]] $ZipDir = @(),
    [string] $RestoreOptions = "",
    [switch] $Ui
)
$ErrorActionPreference = "Stop"
$App = $PSScriptRoot
$Venv = Join-Path $App ".venv"
$Exe = Join-Path $Venv "Scripts\photo-restore.exe"

if (-not (Test-Path $Exe)) {
    Write-Host "First run: creating the virtual environment (a few minutes)..."
    $py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
    & $py -m venv $Venv
    & (Join-Path $Venv "Scripts\python.exe") -m pip install --upgrade pip
    & (Join-Path $Venv "Scripts\python.exe") -m pip install -e "$App[ui]"
}

if ($Ui) { & $Exe ui; exit $LASTEXITCODE }

$zipArgs = @()
foreach ($z in $Zip)    { $zipArgs += @("--zip", $z) }
foreach ($d in $ZipDir) { $zipArgs += @("--zip-dir", $d) }
if ($zipArgs.Count -gt 0) { & $Exe ingest @zipArgs; if ($LASTEXITCODE) { exit $LASTEXITCODE } }

$opts = if ($RestoreOptions) { $RestoreOptions -split " " } else { @() }
& $Exe restore @opts; if ($LASTEXITCODE) { exit $LASTEXITCODE }
& $Exe report
$root = if ($env:PHOTO_RESTORE_ROOT) { $env:PHOTO_RESTORE_ROOT } else { Join-Path $HOME "photo-restore" }
$report = Join-Path $root "output\report\index.html"
Write-Host "Report: $report"
if (Test-Path $report) { Start-Process $report }
