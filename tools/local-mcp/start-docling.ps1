param([int]$Port = 18769, [int]$ReadyTimeoutSeconds = 60)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$serviceRoot = Join-Path $root 'data/local-services/mcp'
$null = New-Item -ItemType Directory -Force -Path $serviceRoot, (Join-Path $serviceRoot 'logs'), (Join-Path $serviceRoot 'run'), (Join-Path $serviceRoot 'models'), (Join-Path $serviceRoot 'docling-output')
$existing = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
$venv = Join-Path $root 'data/local-services/mcp/docling-venv/Scripts/python.exe'
$serverScript = Join-Path $PSScriptRoot 'docling_server.py'
$scriptArgumentPattern = '(?i)(?:^|\s)"?' + [regex]::Escape($serverScript) + '"?(?=\s|$)'
$portArgumentPattern = '(?:^|\s)--port(?:\s+|=)' + $Port + '(?=\s|$)'
$alreadyRunning = $false
if ($existing) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($existing[0].OwningProcess)"
    $launcher = if ($owner) { Get-CimInstance Win32_Process -Filter "ProcessId=$($owner.ParentProcessId)" }
    $environmentMatches = $owner -and ($owner.ExecutablePath -eq $venv -or (
        $launcher -and $launcher.ExecutablePath -eq $venv -and
        $launcher.CommandLine -match $scriptArgumentPattern -and $launcher.CommandLine -match $portArgumentPattern
    ))
    if (-not $environmentMatches -or $owner.CommandLine -notmatch $scriptArgumentPattern -or $owner.CommandLine -notmatch $portArgumentPattern) { throw "Port $Port is occupied by a process that is not this Docling MCP instance; refusing to reuse it." }
    $proc = Get-Process -Id $owner.ProcessId -ErrorAction Stop
    $alreadyRunning = $true
}
$env:DOCLING_MCP_CONVERSION_MODE = 'local'
$env:DOCLING_MCP_DO_OCR = 'false'
$env:DOCLING_MCP_DO_TABLE_STRUCTURE = 'true'
$env:DOCLING_MCP_KEEP_IMAGES = 'false'
$env:HF_HOME = Join-Path $root 'data/local-services/mcp/models/huggingface'
$env:DOCLING_ARTIFACTS_PATH = Join-Path $root 'data/local-services/mcp/models/docling'
$env:OMP_NUM_THREADS = '4'
$env:DOCLING_NUM_THREADS = '4'
$env:DOCLING_DEVICE = 'cpu'
$env:CACHE_DIR = Join-Path $root 'data/local-services/mcp/docling-output'
$log = Join-Path $serviceRoot 'logs/docling.log'
if (-not $alreadyRunning -and -not (Test-Path -LiteralPath $venv)) {
    throw "Docling environment is missing: $venv. Run tools/local-mcp/install.ps1 first."
}
if (-not $alreadyRunning) {
    $proc = Start-Process -FilePath $venv -ArgumentList ('"{0}"' -f $serverScript),'--transport','streamable-http','--host','127.0.0.1','--port',"$Port" -WorkingDirectory $root -RedirectStandardOutput $log -RedirectStandardError ($log + '.error') -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content (Join-Path $serviceRoot 'run/docling.pid')
}

$body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"FinReport-start-check","version":"1"}}}'
$deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
$lastError = ''
do {
    if ($proc.HasExited) {
        throw "Docling MCP process exited before becoming ready (exit code $($proc.ExitCode)). See $($log).error"
    }
    try {
        $response = Invoke-WebRequest "http://127.0.0.1:$Port/mcp" -UseBasicParsing -Method Post -ContentType 'application/json' -Headers @{Accept='application/json, text/event-stream'} -Body $body -TimeoutSec 5
        if ($response.StatusCode -eq 200) {
            if ($alreadyRunning) { Write-Output "verified docling MCP pid=$($proc.Id) at http://127.0.0.1:$Port/mcp" }
            else { Write-Output "docling MCP ready pid=$($proc.Id) at http://127.0.0.1:$Port/mcp" }
            exit 0
        }
        $lastError = "HTTP $($response.StatusCode)"
    } catch {
        $lastError = $_.Exception.Message
    }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)
throw "Timed out waiting for Docling MCP readiness after $ReadyTimeoutSeconds seconds. $lastError See $($log).error"
