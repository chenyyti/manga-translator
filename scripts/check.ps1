$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython
$env:MANGA_TRANSLATOR_PYTHON = $pythonExe
$pytestTempRoot = Join-Path $repoRoot '.test-tmp'
New-Item -ItemType Directory -Path $pytestTempRoot -Force | Out-Null
$env:PYTEST_DEBUG_TEMPROOT = $pytestTempRoot

function Invoke-Checked {
    param([scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Verification command failed with exit code $LASTEXITCODE"
    }
}

Push-Location (Join-Path $repoRoot 'backend')
try {
    Invoke-Checked { & $pythonExe -m ruff check --no-cache app tests scripts }
    Invoke-Checked { & $pythonExe -m pytest }
}
finally {
    Pop-Location
}

Push-Location (Join-Path $repoRoot 'frontend')
try {
    Invoke-Checked { pnpm format:check }
    Invoke-Checked { pnpm typecheck }
    Invoke-Checked { pnpm test }
    Invoke-Checked { pnpm build }
    Invoke-Checked { pnpm e2e }
}
finally {
    Pop-Location
}
