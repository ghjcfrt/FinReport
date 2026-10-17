$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root "data\local-services\unlimited-ocr\.venv\Scripts\python.exe"
$serverScript = Join-Path $PSScriptRoot 'unlimited_ocr_server.py'
$scriptArgumentPattern = '(?i)(?:^|\s)"?' + [regex]::Escape($serverScript) + '"?(?=\s|$)'
$processes = @(Get-CimInstance Win32_Process)
$targets = @($processes | Where-Object {
    $candidate = $_
    $launcher = $processes | Where-Object { $_.ProcessId -eq $candidate.ParentProcessId } | Select-Object -First 1
    $parentMatches = $launcher -and $launcher.ExecutablePath -eq $python -and $launcher.CommandLine -match $scriptArgumentPattern
    $candidate.CommandLine -match $scriptArgumentPattern -and ($candidate.ExecutablePath -eq $python -or $parentMatches)
})
foreach ($target in ($targets | Sort-Object @{Expression={ $_.ExecutablePath -eq $python }})) {
    Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
}
$pidFile = Join-Path $root 'data/local-services/unlimited-ocr/service.pid'
if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile -Force }
