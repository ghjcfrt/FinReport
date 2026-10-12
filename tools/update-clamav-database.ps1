[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DatabaseRoot,

    [string]$SourceDatabase,
    [string]$FreshclamPath,
    [string]$FreshclamConfig,
    [string]$CurrentJson = "current.json",
    [string]$DatabaseId,
    [string]$ProbeExecutable,
    [string]$ProbeShim,
    [string]$ProbeFile,
    [string]$ProbeCerts,
    [string]$SigtoolPath,
    [string]$SignatureCerts,
    [string]$FreshclamVersion,
    [switch]$RequireOfficialDatabase,
    [switch]$Activate,
    [switch]$AllowEmptyDatabase
)

$ErrorActionPreference = "Stop"

function Get-FullPath([string]$Path) {
    return [IO.Path]::GetFullPath($Path)
}

function Get-DatabaseFiles([string]$Directory) {
    $allowed = @(
        "*.cvd", "*.cld", "*.hdb", "*.hsb", "*.hdu", "*.fp", "*.crtdb",
        "*.cdb", "*.ndb", "*.ldb", "*.mdb", "*.sfp", "*.sign", "freshclam.dat"
    )
    return @(Get-ChildItem -LiteralPath $Directory -File -Recurse -Include $allowed)
}

function Assert-DatabaseDirectory([string]$Directory) {
    $files = @(Get-DatabaseFiles $Directory)
    if ($files.Count -eq 0 -and -not $AllowEmptyDatabase) {
        throw "候选病毒库为空：$Directory。需要 .cvd/.cld 或签名数据库文件。"
    }
    $zeroLength = @($files | Where-Object { $_.Length -le 0 })
    if ($zeroLength.Count -gt 0) {
        $names = ($zeroLength | ForEach-Object { $_.FullName }) -join ", "
        throw "候选病毒库包含空文件：$names"
    }
    return $files
}

function Copy-DatabaseFiles([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $files = @(Get-DatabaseFiles $Source)
    if ($files.Count -eq 0 -and -not $AllowEmptyDatabase) {
        throw "源病毒库没有可复制的数据库文件：$Source"
    }
    foreach ($file in $files) {
        $relative = [IO.Path]::GetRelativePath($Source, $file.FullName)
        $target = Join-Path $Destination $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
    }
}

function Read-CurrentPointer([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { throw "current.json 无法解析：$Path" }
}

function Invoke-NativeProbe([string]$Executable, [string]$Shim, [string]$Database, [string]$File, [string]$Certs) {
    $arguments = @(
        "native-probe", "--shim", $Shim, "--database", $Database,
        "--file", $File, "--certs", $Certs, "--json"
    )
    $previousPath = $env:PATH
    $shimDirectory = Split-Path -Parent $Shim
    try {
        # Keep DLL resolution inside the trusted runtime directory supplied by
        # the caller. Do not install these DLLs into PATH permanently.
        $env:PATH = "$shimDirectory;$previousPath"
        $output = & $Executable @arguments 2>&1 | Out-String
    }
    finally {
        $env:PATH = $previousPath
    }
    if ($LASTEXITCODE -ne 0) {
        throw "native-probe 启动或初始化失败（$LASTEXITCODE）：$output"
    }
    try { $result = $output | ConvertFrom-Json }
    catch { throw "native-probe 返回了无效 JSON：$output" }
    if ($result.status -ne 0 -or $result.scan_rc -ne 0) {
        throw "候选病毒库验证扫描失败：$output"
    }
    return $result
}

function Invoke-SignatureVerification([string]$Sigtool, [string]$Database, [string]$Certs, [string]$DatabaseFile) {
    $output = & $Sigtool --verify $DatabaseFile --cvdcertsdir $Certs 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "官方病毒库签名验证失败：$DatabaseFile；$output"
    }
    return [ordered]@{
        database = [IO.Path]::GetRelativePath($Database, $DatabaseFile).Replace("\", "/")
        output = $output.Trim()
    }
}

$root = Get-FullPath $DatabaseRoot
New-Item -ItemType Directory -Force -Path $root | Out-Null
$versions = Join-Path $root "versions"
New-Item -ItemType Directory -Force -Path $versions | Out-Null
$pointerPath = if ([IO.Path]::IsPathRooted($CurrentJson)) { Get-FullPath $CurrentJson } else { Join-Path $root $CurrentJson }
$pointerPath = Get-FullPath $pointerPath
$current = Read-CurrentPointer $pointerPath

if ([string]::IsNullOrWhiteSpace($DatabaseId)) {
    $DatabaseId = "db-" + (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss") + "-" + ([guid]::NewGuid().ToString("N").Substring(0, 8))
}
if ($DatabaseId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
    throw "DatabaseId 只能包含字母、数字、点、下划线和短横线。"
}
$candidate = Join-Path $versions $DatabaseId
if (Test-Path -LiteralPath $candidate) {
    throw "候选目录已存在，不覆盖既有病毒库：$candidate"
}

try {
    if ($SourceDatabase -and $FreshclamPath) {
        throw "SourceDatabase 与 FreshclamPath 只能二选一。"
    }
    if (-not $SourceDatabase -and -not $FreshclamPath) {
        throw "必须提供 SourceDatabase 或 FreshclamPath。"
    }
    if ($Activate -and -not $RequireOfficialDatabase) {
        throw "激活生产数据库必须使用 RequireOfficialDatabase，并验证官方 main/daily/bytecode 签名。"
    }
    if ($Activate -and -not ($ProbeExecutable -and $ProbeShim -and $ProbeFile -and $ProbeCerts)) {
        throw "激活数据库前必须提供完整 native-probe 参数；候选 Worker 验证不能跳过。"
    }

    New-Item -ItemType Directory -Force -Path $candidate | Out-Null

    if ($SourceDatabase) {
        $source = Get-FullPath $SourceDatabase
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            throw "源病毒库目录不存在：$source"
        }
        Copy-DatabaseFiles $source $candidate
    }
    else {
        $freshclam = Get-FullPath $FreshclamPath
        if (-not (Test-Path -LiteralPath $freshclam -PathType Leaf)) {
            throw "freshclam.exe 不存在：$freshclam"
        }
        if ($current -and $current.directory) {
            $active = Get-FullPath (Join-Path $root ([string]$current.directory))
            if (Test-Path -LiteralPath $active -PathType Container) {
                Copy-DatabaseFiles $active $candidate
            }
        }
        $freshclamArgs = @("--datadir=$candidate")
        if ($FreshclamConfig) {
            $config = Get-FullPath $FreshclamConfig
            if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
                throw "freshclam 配置不存在：$config"
            }
            $freshclamArgs += "--config-file=$config"
        }
        & $freshclam @freshclamArgs
        if ($LASTEXITCODE -ne 0) {
            throw "freshclam 更新失败，exit code=$LASTEXITCODE"
        }
    }

    $files = @(Assert-DatabaseDirectory $candidate)
    $signatureRecords = @()
    if ($RequireOfficialDatabase) {
        # Freshclam incremental updates can produce CLD files. Each official
        # family must have exactly one CVD/CLD archive and its matching detached
        # signature; do not accidentally verify a stale CVD beside a newer CLD.
        $required = @("main", "daily", "bytecode")
        $officialFiles = @()
        foreach ($name in $required) {
            $archives = @(
                foreach ($extension in @("cvd", "cld")) {
                    $path = Join-Path $candidate "$name.$extension"
                    if (Test-Path -LiteralPath $path -PathType Leaf) { Get-Item -LiteralPath $path }
                }
            )
            if ($archives.Count -ne 1) {
                throw "官方病毒库 $name 必须且只能有一个 .cvd/.cld 文件：$candidate"
            }
            $archive = $archives[0]
            $sign = @(Get-ChildItem -LiteralPath $candidate -File -Filter "$name-*$($archive.Extension).sign")
            if ($sign.Count -ne 1) {
                throw "官方病毒库 $($archive.Name) 必须且只能有一个对应的 .sign 文件：$candidate"
            }
            $officialFiles += $archive.FullName
        }
        if (-not $SigtoolPath -or -not $SignatureCerts) {
            throw "RequireOfficialDatabase 必须同时提供 SigtoolPath 和 SignatureCerts。"
        }
        $sigtool = Get-FullPath $SigtoolPath
        $signatureCerts = Get-FullPath $SignatureCerts
        if (-not (Test-Path -LiteralPath $sigtool -PathType Leaf)) { throw "sigtool 不存在：$sigtool" }
        if (-not (Test-Path -LiteralPath $signatureCerts -PathType Container)) { throw "签名证书目录不存在：$signatureCerts" }
        $signatureRecords = @(
            foreach ($archivePath in $officialFiles) {
                Invoke-SignatureVerification $sigtool $candidate $signatureCerts $archivePath
            }
        )
    }
    $fileRecords = @(
        $files | Sort-Object FullName | ForEach-Object {
            $relative = [IO.Path]::GetRelativePath($candidate, $_.FullName).Replace("\", "/")
            [ordered]@{
                path = $relative
                bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
    )

    $probeResult = $null
    if ($ProbeExecutable -or $ProbeShim -or $ProbeFile -or $ProbeCerts) {
        if (-not ($ProbeExecutable -and $ProbeShim -and $ProbeFile -and $ProbeCerts)) {
            throw "提供 native-probe 验证时必须同时提供 ProbeExecutable、ProbeShim、ProbeFile、ProbeCerts。"
        }
        $probeResult = Invoke-NativeProbe (Get-FullPath $ProbeExecutable) (Get-FullPath $ProbeShim) $candidate (Get-FullPath $ProbeFile) (Get-FullPath $ProbeCerts)
    }

    $activationTime = if ($Activate) { (Get-Date).ToUniversalTime().ToString("o") } else { $null }
    $manifest = [ordered]@{
        schema = "finreport-clamav-database-v1"
        database_id = $DatabaseId
        directory = [IO.Path]::GetRelativePath($root, $candidate).Replace("\", "/")
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        activated_at = $activationTime
        source = if ($SourceDatabase) { "local-copy" } else { "freshclam" }
        freshclam_version = $FreshclamVersion
        files = $fileRecords
        official_database = [bool]$RequireOfficialDatabase
        signature_verification = $signatureRecords
        native_probe = $probeResult
        restrictions = @(
            "The database is activated by pointer swap; active version directories are never overwritten.",
            "The database must be loaded by the matching ClamAV runtime and bundled dependencies used by the native probe.",
            "This script does not install or select the scanner runtime; deployment must supply its ABI-matched shim, certificates, and license notices."
        )
    }

    $manifestPath = Join-Path $candidate "database-manifest.json"
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))

    if ($Activate) {
        $pointer = [ordered]@{
            schema = "finreport-clamav-current-v1"
            database_id = $DatabaseId
            directory = [IO.Path]::GetRelativePath($root, $candidate).Replace("\", "/")
            activated_at = $manifest.activated_at
            manifest = [IO.Path]::GetRelativePath($root, $manifestPath).Replace("\", "/")
        }
        $tmpPointer = "$pointerPath.tmp.$([guid]::NewGuid().ToString('N'))"
        [IO.File]::WriteAllText($tmpPointer, ($pointer | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $pointerPath -PathType Leaf) {
            $backupPointer = "$pointerPath.bak.$([guid]::NewGuid().ToString('N'))"
            [IO.File]::Replace($tmpPointer, $pointerPath, $backupPointer)
            [IO.File]::Delete($backupPointer)
        }
        else {
            [IO.File]::Move($tmpPointer, $pointerPath)
        }
    }

    Write-Output ($manifest | ConvertTo-Json -Depth 8)
}
catch {
    if (Test-Path -LiteralPath $candidate) {
        Remove-Item -LiteralPath $candidate -Recurse -Force
    }
    throw
}
