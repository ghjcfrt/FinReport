param([int]$Port = 18768, [int]$ReadyTimeoutSeconds = 60)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$serviceRoot = Join-Path $root 'data/local-services/mcp'
$null = New-Item -ItemType Directory -Force -Path $serviceRoot, (Join-Path $serviceRoot 'logs'), (Join-Path $serviceRoot 'run'), (Join-Path $serviceRoot 'qdrant-data')
$existing = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
$venv = Join-Path $root 'data/local-services/mcp/qdrant-venv/Scripts/python.exe'
$serverScript = Join-Path $PSScriptRoot 'qdrant_server.py'
$scriptArgumentPattern = '(?i)(?:^|\s)"?' + [regex]::Escape($serverScript) + '"?(?=\s|$)'
$alreadyRunning = $false
if ($existing) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($existing[0].OwningProcess)"
    $launcher = if ($owner) { Get-CimInstance Win32_Process -Filter "ProcessId=$($owner.ParentProcessId)" }
    $environmentMatches = $owner -and ($owner.ExecutablePath -eq $venv -or (
        $launcher -and $launcher.ExecutablePath -eq $venv -and $launcher.CommandLine -match $scriptArgumentPattern
    ))
    if (-not $environmentMatches -or $owner.CommandLine -notmatch $scriptArgumentPattern) { throw "Port $Port is occupied by a process that is not this Qdrant MCP instance; refusing to reuse it." }
    $proc = Get-Process -Id $owner.ProcessId -ErrorAction Stop
    $alreadyRunning = $true
}
$env:QDRANT_LOCAL_PATH = Join-Path $root 'data/local-services/mcp/qdrant-data'
$env:COLLECTION_NAME = 'finreport_document_chunks'
$env:QDRANT_ALLOW_ARBITRARY_FILTER = 'true'
$env:QDRANT_SEARCH_LIMIT = '30'
$env:EMBEDDING_MODEL = 'BAAI/bge-small-zh-v1.5'
$env:MCP_QDRANT_PORT = "$Port"
$log = Join-Path $serviceRoot 'logs/qdrant.log'
if (-not $alreadyRunning -and -not (Test-Path -LiteralPath $venv)) {
    throw "Qdrant environment is missing: $venv. Run tools/local-mcp/install.ps1 first."
}
if (-not $alreadyRunning) {
    $proc = Start-Process -FilePath $venv -ArgumentList ('"{0}"' -f $serverScript) -WorkingDirectory $root -RedirectStandardOutput $log -RedirectStandardError ($log + '.error') -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content (Join-Path $serviceRoot 'run/qdrant.pid')
}

$body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"FinReport-start-check","version":"1"}}}'
$deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
$lastError = ''
do {
    if ($proc.HasExited) {
        throw "Qdrant MCP process exited before becoming ready (exit code $($proc.ExitCode)). See $($log).error"
    }
    try {
        $response = Invoke-WebRequest "http://127.0.0.1:$Port/mcp/" -UseBasicParsing -Method Post -ContentType 'application/json' -Headers @{Accept='application/json, text/event-stream'} -Body $body -TimeoutSec 5
        if ($response.StatusCode -eq 200) {
            if ($alreadyRunning) { Write-Output "verified qdrant MCP pid=$($proc.Id) at http://127.0.0.1:$Port/mcp/" }
            else { Write-Output "qdrant MCP ready pid=$($proc.Id) at http://127.0.0.1:$Port/mcp/" }
            exit 0
        }
        $lastError = "HTTP $($response.StatusCode)"
    } catch {
        $lastError = $_.Exception.Message
    }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)
throw "Timed out waiting for Qdrant MCP readiness after $ReadyTimeoutSeconds seconds. $lastError See $($log).error"
