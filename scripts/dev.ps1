$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython
$backend = Start-Process -FilePath $pythonExe -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000','--reload' -WorkingDirectory (Join-Path $repoRoot 'backend') -PassThru -WindowStyle Hidden

function Test-BackendHealth {
    $handler = $null
    $client = $null
    try {
        # Do not inherit a system proxy for the loopback health check. A
        # proxy can turn a short startup race into a misleading 500 page.
        $handler = [System.Net.Http.HttpClientHandler]::new()
        $handler.UseProxy = $false
        $client = [System.Net.Http.HttpClient]::new($handler)
        $client.Timeout = [TimeSpan]::FromMilliseconds(750)
        $response = $client.GetAsync('http://127.0.0.1:8000/api/health').GetAwaiter().GetResult()
        return $response.IsSuccessStatusCode
    }
    catch {
        return $false
    }
    finally {
        if ($client) { $client.Dispose() }
        if ($handler) { $handler.Dispose() }
    }
}

function Wait-BackendHealth {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutSeconds = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            throw '后端启动失败：127.0.0.1:8000 可能已被其他进程占用，请先关闭旧服务后重试。'
        }
        if (Test-BackendHealth) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw '后端启动超时，请检查 127.0.0.1:8000 是否被其他进程占用。'
}

try {
    Wait-BackendHealth -Process $backend
    pnpm --dir (Join-Path $repoRoot 'frontend') dev
}
finally {
    if (-not $backend.HasExited) {
        Stop-Process -Id $backend.Id
    }
}
