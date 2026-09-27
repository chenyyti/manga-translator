function Ensure-FrontendBuild {
    param([string]$RepositoryRoot, [switch]$Force)
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $frontend = Join-Path $RepositoryRoot 'frontend'
    $cache = Join-Path $frontend 'dist\build-inputs.json'
    $paths = @()
    foreach ($name in @('src', 'public')) {
        $directory = Join-Path $frontend $name
        if (Test-Path -LiteralPath $directory) { $paths += Get-ChildItem -LiteralPath $directory -Recurse -File }
    }
    $paths += Get-ChildItem -LiteralPath $frontend -File | Where-Object {
        $_.Name -match '^(index\.html|package\.json|pnpm-lock\.yaml|pnpm-workspace\.yaml|tsconfig.*\.json|vite\.config\..*|\.env.*|\.npmrc)$'
    }
    $entries = @($paths | Sort-Object FullName | ForEach-Object {
        $_.FullName.Substring($frontend.Length) + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    })
    $entries += @(Get-ChildItem Env: | Where-Object Name -Like 'VITE_*' | Sort-Object Name | ForEach-Object { $_.Name + '=' + $_.Value })
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $fingerprint = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($entries -join "`n")))) }
    finally { $sha.Dispose() }
    $valid = $false
    try {
        $previous = Get-Content -LiteralPath $cache -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
        $valid = ($previous.version -eq 1 -and $previous.fingerprint -eq $fingerprint -and $previous.outputs.Count -gt 0)
        foreach ($relative in $previous.outputs) { $valid = $valid -and (Test-Path -LiteralPath (Join-Path $frontend "dist\$relative") -PathType Leaf) }
        $valid = $valid -and (Test-Path -LiteralPath (Join-Path $frontend 'dist\index.html'))
    } catch { $valid = $false }
    if ($valid -and -not $Force) { Write-Host "Frontend cached ($($watch.ElapsedMilliseconds) ms)."; return }
    if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) { throw 'Frontend rebuild required: pnpm is unavailable.' }
    if (-not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules'))) { throw 'Run pnpm --dir frontend install --frozen-lockfile first.' }
    Write-Host 'Building changed frontend...'
    & pnpm --dir $frontend build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed; application was not started.' }
    $dist = Join-Path $frontend 'dist'
    $outputs = @(Get-ChildItem -LiteralPath $dist -Recurse -File | Where-Object Name -NE 'build-inputs.json' | ForEach-Object { $_.FullName.Substring($dist.Length + 1) })
    $record = @{ version = 1; fingerprint = $fingerprint; outputs = $outputs } | ConvertTo-Json -Depth 4
    $temporary = "$cache.$([Guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText($temporary, $record)
    Move-Item -LiteralPath $temporary -Destination $cache -Force
    Write-Host "Frontend build completed ($($watch.ElapsedMilliseconds) ms)."
}
