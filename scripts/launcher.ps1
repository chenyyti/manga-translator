[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$BackendPort = 8000,
    [switch]$ForceRebuild
)

$ErrorActionPreference = 'Stop'
$launchWatch = [Diagnostics.Stopwatch]::StartNew()
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'ensure-frontend.ps1')
Ensure-FrontendBuild -RepositoryRoot $repoRoot -Force:$ForceRebuild
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython
$frontendIndex = Join-Path $repoRoot 'frontend\dist\index.html'
if (-not (Test-Path -LiteralPath $frontendIndex)) {
    throw 'Frontend is not built. Run the frontend build before launching the managed window.'
}

$server = $null
$sessionId = [Guid]::NewGuid().ToString('N')
$heartbeatIntervalSeconds = 10
$heartbeatTimeoutSeconds = 60
$requestedBackendPort = $BackendPort
$healthUrl = $null
$frontendUrl = $null
$previousSessionId = $env:MANGA_TRANSLATOR_LAUNCHER_SESSION_ID
$previousHeartbeatInterval = $env:MANGA_TRANSLATOR_LAUNCHER_HEARTBEAT_INTERVAL_SECONDS
$previousHeartbeatTimeout = $env:MANGA_TRANSLATOR_LAUNCHER_HEARTBEAT_TIMEOUT_SECONDS
$previousPythonUtf8 = $env:PYTHONUTF8
$launcherLogDirectory = Join-Path ([System.IO.Path]::GetTempPath()) 'MangaTranslatorLauncher'
$backendStdoutLog = Join-Path $launcherLogDirectory "$sessionId-backend.stdout.log"
$backendStderrLog = Join-Path $launcherLogDirectory "$sessionId-backend.stderr.log"
$backendStdoutState = @{ Offset = [long]0 }
$startupSucceeded = $false
$taskLogMarker = '[TASK]'

try {
    Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
}
catch {
    $httpClientType = 'System.Net.Http.HttpClient' -as [type]
    if (-not $httpClientType) {
        throw 'System.Net.Http is unavailable. Windows PowerShell cannot run the launcher health check.'
    }
}

function Get-BackendLogTail {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ''
    }
    try {
        return ((Get-Content -LiteralPath $Path -Tail 80 -ErrorAction Stop) -join [Environment]::NewLine)
    }
    catch {
        return ''
    }
}

function Write-NewTaskProgress {
    param(
        [string]$Path,
        [hashtable]$State
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        if ($stream.Length -lt [long]$State.Offset) {
            $State.Offset = [long]0
        }
        $available = $stream.Length - [long]$State.Offset
        if ($available -le 0) {
            return
        }
        [void]$stream.Seek([long]$State.Offset, [System.IO.SeekOrigin]::Begin)
        $buffer = New-Object byte[] ([int]$available)
        $read = $stream.Read($buffer, 0, $buffer.Length)
        if ($read -le 0) {
            return
        }
        $lastNewline = -1
        for ($index = $read - 1; $index -ge 0; $index--) {
            if ($buffer[$index] -eq 10) {
                $lastNewline = $index
                break
            }
        }
        # Leave a partial UTF-8 line unread until the backend flushes its
        # newline; this avoids corrupting a split Chinese character.
        if ($lastNewline -lt 0) {
            return
        }
        $text = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $lastNewline + 1)
        $State.Offset = [long]$State.Offset + $lastNewline + 1
        foreach ($line in ($text -split "`r?`n")) {
            if ($line.StartsWith($taskLogMarker, [System.StringComparison]::Ordinal)) {
                Write-Host $line.Substring($taskLogMarker.Length).TrimStart()
            }
        }
    }
    catch {
        # Live display is best-effort. The complete backend logs remain
        # available to the existing startup diagnostics path.
    }
    finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Get-BackendDiagnostics {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$StdoutPath,
        [string]$StderrPath
    )
    $lines = @()
    if ($Process) {
        try {
            $Process.Refresh()
            $lines += "Backend process id: $($Process.Id); exited: $($Process.HasExited)."
            if ($Process.HasExited) {
                $lines += "Backend exit code: $($Process.ExitCode)."
            }
        }
        catch {
            $lines += 'Backend process status could not be read.'
        }
    }
    $stderr = Get-BackendLogTail -Path $StderrPath
    $stdout = Get-BackendLogTail -Path $StdoutPath
    if ($stderr) {
        $lines += "Backend stderr (last 80 lines):`n$stderr"
    }
    if ($stdout) {
        $lines += "Backend stdout (last 80 lines):`n$stdout"
    }
    if (-not $stderr -and -not $stdout) {
        $lines += "No backend output was captured. Logs: $StderrPath"
    }
    return ($lines -join [Environment]::NewLine)
}

$sharedHandler = [System.Net.Http.HttpClientHandler]::new()
$sharedHandler.UseProxy = $false
$sharedClient = [System.Net.Http.HttpClient]::new($sharedHandler)
$sharedClient.Timeout = [TimeSpan]::FromMilliseconds(750)

function Test-HttpSuccess {
    param([string]$Url)
    try {
        $response = $sharedClient.GetAsync($Url).GetAwaiter().GetResult()
        try { return $response.IsSuccessStatusCode } finally { $response.Dispose() }
    } catch { return $false }
}

function Wait-Backend {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Url,
        [int]$Port,
        [string]$StdoutPath,
        [string]$StderrPath,
        [hashtable]$StdoutState,
        [int]$TimeoutSeconds = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Write-NewTaskProgress -Path $StdoutPath -State $StdoutState
        $Process.Refresh()
        if ($Process.HasExited) {
            Write-NewTaskProgress -Path $StdoutPath -State $StdoutState
            $diagnostics = Get-BackendDiagnostics -Process $Process -StdoutPath $StdoutPath -StderrPath $StderrPath
            throw "Backend failed to start on 127.0.0.1:$Port.`n$diagnostics"
        }
        if (Test-HttpSuccess -Url $Url) {
            Write-NewTaskProgress -Path $StdoutPath -State $StdoutState
            return
        }
        Start-Sleep -Milliseconds 500
    }
    $diagnostics = Get-BackendDiagnostics -Process $Process -StdoutPath $StdoutPath -StderrPath $StderrPath
    throw "Backend startup timed out on 127.0.0.1:$Port.`n$diagnostics"
}

function Test-PortAvailable {
    param([int]$Port)

    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback,
            $Port
        )
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

function Resolve-BackendPort {
    param(
        [int]$PreferredPort,
        [int]$MaxAttempts = 20
    )

    for ($offset = 0; $offset -lt $MaxAttempts; $offset++) {
        $candidate = $PreferredPort + $offset
        if ($candidate -gt 65535) {
            break
        }
        if (Test-PortAvailable -Port $candidate) {
            return $candidate
        }
    }
    throw "No available localhost port was found from $PreferredPort."
}

function Start-DefaultBrowser {
    param([string]$Url)
    Start-Process -FilePath $Url | Out-Null
}

function Stop-BackendProcessTree {
    param([System.Diagnostics.Process]$Process)
    if (-not $Process) {
        return
    }

    $backendProcessId = [int]$Process.Id
    try {
        $Process.Refresh()
        if ($Process.HasExited) {
            return
        }
    }
    catch {
        if (-not (Get-Process -Id $backendProcessId -ErrorAction SilentlyContinue)) {
            return
        }
    }

    # taskkill removes model workers as well as the Uvicorn parent.  Verify the
    # result because native-command failures do not reliably stop a PowerShell
    # script, especially while the launcher itself is shutting down.
    try {
        & taskkill.exe /PID "$backendProcessId" /T /F 2>$null | Out-Null
    }
    catch { }

    try {
        if ($Process.WaitForExit(5000)) {
            return
        }
    }
    catch { }

    # A direct Stop-Process is the final fallback for a backend that survived
    # taskkill.  This is also the path used to recover from an incomplete
    # native-command invocation.
    Stop-Process -Id $backendProcessId -Force -ErrorAction SilentlyContinue
    try {
        Wait-Process -Id $backendProcessId -Timeout 5 -ErrorAction SilentlyContinue
    }
    catch { }

    if (Get-Process -Id $backendProcessId -ErrorAction SilentlyContinue) {
        throw "Backend process $backendProcessId could not be stopped."
    }
}

function Get-HeartbeatStatus {
    param([string]$SessionId, [string]$Port)
    try {
        $encoded = [Uri]::EscapeDataString($SessionId)
        $response = $sharedClient.GetAsync("http://127.0.0.1:$Port/api/runtime/session/status?session_id=$encoded").GetAwaiter().GetResult()
        try {
            if (-not $response.IsSuccessStatusCode) { return $null }
            $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
            if ($body.success) { return $body.data }
        } finally { $response.Dispose() }
    } catch { return $null }
}

function Wait-Frontend {
    param(
        [System.Diagnostics.Process]$ServerProcess,
        [string]$SessionId,
        [string]$Port,
        [string]$StdoutPath,
        [hashtable]$StdoutState,
        [int]$InitialTimeoutSeconds
    )
    $firstHeartbeatDeadline = (Get-Date).AddSeconds($InitialTimeoutSeconds)
    $receivedHeartbeat = $false
    $lastActiveAt = $null
    while ($true) {
        Write-NewTaskProgress -Path $StdoutPath -State $StdoutState
        $now = Get-Date
        $ServerProcess.Refresh()
        if ($ServerProcess.HasExited) {
            throw 'Backend exited before the frontend page disconnected.'
        }

        $status = Get-HeartbeatStatus -SessionId $SessionId -Port $Port
        if ($status -and $status.managed -and $status.active) {
            if (-not $receivedHeartbeat) { Write-Host "Frontend connected ($($launchWatch.ElapsedMilliseconds) ms)." }
            $receivedHeartbeat = $true
            $lastActiveAt = $now
        }
        elseif ($receivedHeartbeat -and $status -and $status.managed -and -not $status.active) {
            # The backend already applies the configured heartbeat timeout.
            # Return immediately instead of applying the same timeout twice.
            return
        }
        elseif ($receivedHeartbeat -and ($null -ne $lastActiveAt) -and (($now - $lastActiveAt).TotalSeconds -ge $InitialTimeoutSeconds)) {
            return
        }
        elseif (-not $receivedHeartbeat -and $now -ge $firstHeartbeatDeadline) {
            throw 'The frontend heartbeat was not received.'
        }
        Start-Sleep -Seconds 1
    }
}

$BackendPort = Resolve-BackendPort -PreferredPort $requestedBackendPort
if ($BackendPort -ne $requestedBackendPort) {
    Write-Host "Port $requestedBackendPort is occupied; using 127.0.0.1:$BackendPort."
}
$healthUrl = "http://127.0.0.1:$BackendPort/api/health"
$frontendUrl = "http://127.0.0.1:$BackendPort"

$env:MANGA_TRANSLATOR_LAUNCHER_SESSION_ID = $sessionId
$env:MANGA_TRANSLATOR_LAUNCHER_HEARTBEAT_INTERVAL_SECONDS = "$heartbeatIntervalSeconds"
$env:MANGA_TRANSLATOR_LAUNCHER_HEARTBEAT_TIMEOUT_SECONDS = "$heartbeatTimeoutSeconds"
$env:PYTHONUTF8 = '1'

try {
    New-Item -ItemType Directory -Path $launcherLogDirectory -Force | Out-Null
    $server = Start-Process `
        -FilePath $pythonExe `
        -ArgumentList '-u','-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$BackendPort" `
        -WorkingDirectory (Join-Path $repoRoot 'backend') `
        -RedirectStandardOutput $backendStdoutLog `
        -RedirectStandardError $backendStderrLog `
        -PassThru `
        -WindowStyle Hidden
    Wait-Backend -Process $server -Url $healthUrl -Port $BackendPort -StdoutPath $backendStdoutLog -StderrPath $backendStderrLog -StdoutState $backendStdoutState
    Write-Host "Core service ready ($($launchWatch.ElapsedMilliseconds) ms since launcher entry)."
    Start-DefaultBrowser -Url $frontendUrl
    Wait-Frontend `
        -ServerProcess $server `
        -SessionId $sessionId `
        -Port "$BackendPort" `
        -StdoutPath $backendStdoutLog `
        -StdoutState $backendStdoutState `
        -InitialTimeoutSeconds $heartbeatTimeoutSeconds
    $startupSucceeded = $true
}
finally {
    $backendCleanupError = $null
    try {
        Write-NewTaskProgress -Path $backendStdoutLog -State $backendStdoutState
    }
    catch { }
    try {
        # Stop the backend first so later best-effort cleanup can never leave
        # the listening port behind.
        Stop-BackendProcessTree -Process $server
    }
    catch {
        $backendCleanupError = $_
    }
    try { $sharedClient.Dispose() } catch { }
    try { $sharedHandler.Dispose() } catch { }
    if ($startupSucceeded) {
        Remove-Item -LiteralPath $backendStdoutLog -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backendStderrLog -Force -ErrorAction SilentlyContinue
    }
    if ($null -eq $previousSessionId) {
        Remove-Item Env:MANGA_TRANSLATOR_LAUNCHER_SESSION_ID -ErrorAction SilentlyContinue
    }
    else {
        $env:MANGA_TRANSLATOR_LAUNCHER_SESSION_ID = $previousSessionId
    }
    if ($null -eq $previousHeartbeatInterval) {
        Remove-Item Env:MANGA_TRANSLATOR_LAUNCHER_HEARTBEAT_INTERVAL_SECONDS -ErrorAction SilentlyContinue
    }
    else {
        $env:MANGA_TRANSLATOR_LAUNCHER_HEARTBEAT_INTERVAL_SECONDS = $previousHeartbeatInterval
    }
    if ($null -eq $previousHeartbeatTimeout) {
        Remove-Item Env:MANGA_TRANSLATOR_LAUNCHER_HEARTBEAT_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
    }
    else {
        $env:MANGA_TRANSLATOR_LAUNCHER_HEARTBEAT_TIMEOUT_SECONDS = $previousHeartbeatTimeout
    }
    if ($null -eq $previousPythonUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONUTF8 = $previousPythonUtf8
    }
    if ($backendCleanupError) {
        throw $backendCleanupError.Exception
    }
}
