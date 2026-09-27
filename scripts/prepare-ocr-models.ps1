param(
    [ValidateSet('all', 'mangaocr', 'paddleocr')]
    [string]$Provider = 'all',
    [ValidateSet('auto', 'cpu', 'cuda')]
    [string]$Device = 'auto'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython

Push-Location (Join-Path $repoRoot 'backend')
try {
    & $pythonExe scripts/prepare_ocr_models.py --provider $Provider --device $Device
}
finally {
    Pop-Location
}
