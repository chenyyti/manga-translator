$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ensure-frontend.ps1')
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$fixture = Join-Path $temporaryRoot ('manga-build-test-' + [Guid]::NewGuid().ToString('N'))
$script:buildCount = 0
$script:failBuild = $false
function pnpm {
    param($directoryFlag, $directory, $action)
    if ($action -ne 'build') { throw 'Unexpected pnpm invocation' }
    $script:buildCount++
    $global:LASTEXITCODE = 0
    if ($script:failBuild) { $global:LASTEXITCODE = 1; return }
    New-Item -ItemType Directory -Path (Join-Path $directory 'dist') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $directory 'dist/index.html'), '<html>test</html>')
}
function Assert-BuildCount([int]$Expected) {
    if ($script:buildCount -ne $Expected) { throw "Expected $Expected builds, got $script:buildCount" }
}
try {
    $frontend = Join-Path $fixture 'frontend'
    foreach ($name in @('src', 'public', 'node_modules')) {
        New-Item -ItemType Directory -Path (Join-Path $frontend $name) -Force | Out-Null
    }
    $source = Join-Path $frontend 'src/main.ts'
    [IO.File]::WriteAllText($source, 'initial')
    Ensure-FrontendBuild $fixture
    Assert-BuildCount 1
    Remove-Item -LiteralPath (Join-Path $frontend 'node_modules')
    Ensure-FrontendBuild $fixture
    Assert-BuildCount 1
    New-Item -ItemType Directory -Path (Join-Path $frontend 'node_modules') | Out-Null
    [IO.File]::WriteAllText($source, 'changed')
    Ensure-FrontendBuild $fixture
    Assert-BuildCount 2
    Remove-Item -LiteralPath $source
    Ensure-FrontendBuild $fixture
    Assert-BuildCount 3
    [IO.File]::WriteAllText((Join-Path $frontend 'dist/build-inputs.json'), 'broken cache')
    Ensure-FrontendBuild $fixture
    Assert-BuildCount 4
    Ensure-FrontendBuild $fixture -Force
    Assert-BuildCount 5
    $script:failBuild = $true
    $caught = $false
    try { Ensure-FrontendBuild $fixture -Force } catch { $caught = $true }
    if (-not $caught) { throw 'Failed build was not rejected' }
    Write-Host 'Frontend cache checks passed.'
} finally {
    $resolved = [IO.Path]::GetFullPath($fixture)
    if ($resolved.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($resolved).StartsWith('manga-build-test-')) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
