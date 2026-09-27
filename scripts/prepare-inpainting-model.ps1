param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython
Push-Location (Join-Path $repoRoot 'backend')
try {
    $arguments = @('scripts/prepare_inpainting_model.py')
    if ($Force) { $arguments += '--force' }
    & $pythonExe @arguments
}
finally {
    Pop-Location
}
