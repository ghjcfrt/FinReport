param([int]$Port = 18769)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$pidFile = Join-Path $root 'data/local-services/mcp/run/docling.pid'
$venv = Join-Path $root 'data/local-services/mcp/docling-venv/Scripts/python.exe'
$serverScript = Join-Path $PSScriptRoot 'docling_server.py'
$scriptArgumentPattern = '(?i)(?:^|\s)"?' + [regex]::Escape($serverScript) + '"?(?=\s|$)'
$portArgumentPattern = '(?:^|\s)--port(?:\s+|=)' + $Port + '(?=\s|$)'
$targets = @{}
foreach ($listener in @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $launcher = if ($owner) { Get-CimInstance Win32_Process -Filter "ProcessId=$($owner.ParentProcessId)" }
    $parentMatches = $launcher -and $launcher.ExecutablePath -eq $venv -and
        $launcher.CommandLine -match $scriptArgumentPattern -and $launcher.CommandLine -match $portArgumentPattern
    if (-not $owner -or $owner.CommandLine -notmatch $scriptArgumentPattern -or
        $owner.CommandLine -notmatch $portArgumentPattern -or ($owner.ExecutablePath -ne $venv -and -not $parentMatches)) {
        throw "Port $Port belongs to another process; refusing to stop it."
    }
    $targets[$owner.ProcessId] = $owner
    if ($parentMatches) { $targets[$launcher.ProcessId] = $launcher }
}
if (Test-Path -LiteralPath $pidFile) {
    $savedProcessId = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedProcessId)) {
        $saved = Get-CimInstance Win32_Process -Filter "ProcessId=$savedProcessId"
        if ($saved -and $saved.ExecutablePath -eq $venv -and $saved.CommandLine -match $scriptArgumentPattern -and
            $saved.CommandLine -match $portArgumentPattern) { $targets[$saved.ProcessId] = $saved }
    }
}
foreach ($target in ($targets.Values | Sort-Object @{Expression={ $_.ExecutablePath -eq $venv }})) {
    Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile -Force }
