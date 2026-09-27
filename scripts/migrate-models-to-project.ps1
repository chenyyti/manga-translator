param(
    [ValidateSet('all', 'ocr', 'inpainting', 'yolo')]
    [string]$Only = 'all'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython

Push-Location (Join-Path $repoRoot 'backend')
try {
    & $pythonExe scripts/migrate_models_to_project.py --only $Only
    if ($LASTEXITCODE -ne 0) { throw "模型迁移失败，退出码 $LASTEXITCODE" }
}
finally {
    Pop-Location
}
