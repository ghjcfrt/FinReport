param([int]$Port = 8765, [int]$ReadyTimeoutSeconds = 300)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\.." )).Path
$serviceRoot = Join-Path $root "data\local-services\unlimited-ocr"
$null = New-Item -ItemType Directory -Force -Path $serviceRoot, (Join-Path $serviceRoot "hf-cache"), (Join-Path $serviceRoot "tmp")
$python = Join-Path $root "data\local-services\unlimited-ocr\.venv\Scripts\python.exe"
$serverScript = Join-Path $PSScriptRoot "unlimited_ocr_server.py"
$scriptArgumentPattern = '(?i)(?:^|\s)"?' + [regex]::Escape($serverScript) + '"?(?=\s|$)'
$portArgumentPattern = '(?:^|\s)--port(?:\s+|=)' + $Port + '(?=\s|$)'
$env:UNLIMITED_OCR_MODEL_DIR = Join-Path $root "data\local-services\unlimited-ocr\model"
$env:UNLIMITED_OCR_TMP = Join-Path $serviceRoot "tmp"
$env:HF_HOME = Join-Path $root "data\local-services\unlimited-ocr\hf-cache"
$env:PYTHONIOENCODING = "utf-8"
$log = Join-Path $root "data\local-services\unlimited-ocr\unlimited-ocr.log"
$errlog = Join-Path $root "data\local-services\unlimited-ocr\unlimited-ocr.stderr.log"
$existing = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $scriptArgumentPattern -and $_.ExecutablePath -eq $python }
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
    # uv's Windows venv launcher may delegate the listening process to the
    # base interpreter. Verify the parent launcher as well as the exact script.
    $launcher = if ($owner) { Get-CimInstance Win32_Process -Filter "ProcessId=$($owner.ParentProcessId)" }
    $environmentMatches = $owner -and ($owner.ExecutablePath -eq $python -or (
        $launcher -and $launcher.ExecutablePath -eq $python -and
        $launcher.CommandLine -match $scriptArgumentPattern -and $launcher.CommandLine -match $portArgumentPattern
    ))
    if (-not $environmentMatches -or $owner.CommandLine -notmatch $scriptArgumentPattern -or $owner.CommandLine -notmatch $portArgumentPattern) {
        throw "Port $Port is occupied by a process that is not this Unlimited-OCR instance; refusing to reuse it."
    }
    $process = Get-Process -Id $owner.ProcessId -ErrorAction Stop
    $alreadyRunning = $true
} else {
    if ($existing) { throw "Unlimited-OCR process exists but does not own port $Port; refusing duplicate startup." }
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Unlimited-OCR environment is missing: $python. Run tools/local-ocr/setup-unlimited-ocr.ps1 first."
    }
    $process = Start-Process -FilePath $python -ArgumentList @(('"{0}"' -f $serverScript), "--port", "$Port") -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $errlog -PassThru
    $process.Id | Set-Content (Join-Path $serviceRoot "service.pid")
    $alreadyRunning = $false
}
$deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
$lastError = ""
do {
    if ($process.HasExited) {
        throw "Unlimited-OCR process exited before becoming ready (exit code $($process.ExitCode)). See $errlog"
    }
    $health = $null
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5
    } catch {
        $lastError = $_.Exception.Message
    }
    if ($health -and $health.status -eq "error") {
        throw "Unlimited-OCR model load failed: $($health.error). See $errlog"
    }
    if ($health -and $health.status -eq "ready") {
        if ($health.model -ne "baidu/Unlimited-OCR" -or $health.backend -ne "transformers-cuda-nf4" -or $health.model_revision -ne "8249d20b2822a7d8a4c22c8d8c9069b5ac36bda3" -or $health.model_sha256 -ne "2bc48a7a110061ea58fff65d3169367eebe3aee371ca6968dc2219c1b2855fc6") {
            throw "Unlimited-OCR became ready with an unexpected model identity. See $errlog"
        }
        if ($alreadyRunning) {
            Write-Output "already running verified Unlimited-OCR pid=$($process.Id)"
        } else {
            Write-Output "ready http://127.0.0.1:$Port/v1; load=$($health.load_seconds)s; log=$log"
        }
        exit 0
    }
    if ($health -and $health.status -and $health.status -ne "loading") {
        throw "Unlimited-OCR returned unexpected health status '$($health.status)'. See $errlog"
    }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)
throw "Timed out waiting for Unlimited-OCR readiness after $ReadyTimeoutSeconds seconds. $lastError See $errlog"
