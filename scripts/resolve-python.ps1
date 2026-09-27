function Get-ProjectPython {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $cacheRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'MangaTranslator\launcher'
    $cacheFile = Join-Path $cacheRoot 'python-path.txt'
    function Save-PythonCache([string]$CandidatePath) {
        try {
            New-Item -ItemType Directory -Path $cacheRoot -Force -ErrorAction Stop | Out-Null
            [IO.File]::WriteAllText($cacheFile, $CandidatePath)
        } catch { Write-Warning 'Python cache could not be saved; continuing with the verified interpreter.' }
    }
    $candidates = @($env:MANGA_TRANSLATOR_PYTHON)
    try {
        if (Test-Path -LiteralPath $cacheFile) { $candidates += (Get-Content -LiteralPath $cacheFile -Raw -Encoding UTF8 -ErrorAction Stop).Trim() }
    } catch { Write-Warning 'Python cache is unreadable; resolving the interpreter again.' }
    $current = Get-Command python -ErrorAction SilentlyContinue
    if ($current) { $candidates += $current.Source }
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            & $candidate -c "import sys, importlib.util; sys.exit(0 if sys.version_info[:2] == (3, 11) and all(importlib.util.find_spec(m) for m in ('uvicorn', 'sqlalchemy', 'alembic')) else 1)" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0) {
            Save-PythonCache $candidate
            Write-Host "Python resolved ($($timer.ElapsedMilliseconds) ms)."
            return $candidate
        }
    }
    $conda = Get-Command conda -ErrorAction SilentlyContinue
    if ($conda) {
        $resolved = & $conda.Source run -n manga-translator python -c "import sys, uvicorn, sqlalchemy; from alembic import command; assert sys.version_info[:2] == (3, 11); print(sys.executable)"
        $candidate = $resolved | Where-Object { $_ -match 'python\.exe$' } | Select-Object -Last 1
        if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate)) {
            Save-PythonCache $candidate
            Write-Host "Python resolved through Conda ($($timer.ElapsedMilliseconds) ms)."
            return $candidate
        }
    }
    throw 'Project Python 3.11 was not found. Run conda env create -f environment.yml first.'
}
