[CmdletBinding()]
param(
    [switch]$SkipSync,
    [switch]$SkipTests,
    [switch]$SkipOpenApi,
    [switch]$SkipDesktop,
    [switch]$SkipPythonPackages,
    [switch]$SkipScanner,
    [switch]$RequireScanner,
    [switch]$SkipNativeShim,
    [switch]$RequireNativeShim,
    [switch]$AllowExperimentalClamAv
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$clamAvRuntime = if ($env:FINREPORT_CLAMAV_RUNTIME_DIR) { $env:FINREPORT_CLAMAV_RUNTIME_DIR } else { Join-Path $repoRoot "tmp\clamav-runtime-official" }
$clamAvDatabaseRoot = if ($env:FINREPORT_CLAMAV_DATABASE_ROOT) { $env:FINREPORT_CLAMAV_DATABASE_ROOT } else { Join-Path $repoRoot "tmp\clamav-official\staging" }
$clamAvUpdater = if ($env:FINREPORT_CLAMAV_UPDATER_DIR) { $env:FINREPORT_CLAMAV_UPDATER_DIR } else { Join-Path $repoRoot "tmp\clamav-official\install\clamav-1.5.4.win.x64" }
Push-Location $repoRoot

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    Write-Host "`n==> $Name" -ForegroundColor Cyan
    $global:LASTEXITCODE = 0
    & $Action
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "步骤失败: $Name (exit code $LASTEXITCODE)"
    }
}

function Write-CmdFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false))
}

try {
    if ($RequireScanner -and $SkipScanner) {
        throw "-RequireScanner 与 -SkipScanner 不能同时使用。"
    }
    if ($RequireNativeShim -and $SkipNativeShim) {
        throw "-RequireNativeShim 与 -SkipNativeShim 不能同时使用。"
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "未找到 uv。请先安装 uv，并确保它已加入 PATH。"
    }

    if (-not $SkipDesktop) {
        $desktopProjectRoot = Join-Path $repoRoot "apps\desktop\FinReport.Desktop"
        Invoke-Step "清理桌面端旧构建文件" {
            foreach ($buildDirectory in @("bin", "obj")) {
                $path = Join-Path $desktopProjectRoot $buildDirectory
                if (Test-Path -LiteralPath $path) {
                    Remove-Item -LiteralPath $path -Recurse -Force
                }
            }
        }
    }

    if (-not $SkipSync) {
        Invoke-Step "同步 Python 依赖" { uv sync --all-extras }
    }

    Invoke-Step "检查架构依赖方向" { & "$repoRoot\tools\check-architecture.ps1" }
    Invoke-Step "运行 Ruff 检查" { uv run ruff check . }

    if (-not $SkipTests) {
        Invoke-Step "运行 Python 测试" { uv run pytest }
    }

    if (-not $SkipOpenApi) {
        Invoke-Step "生成 OpenAPI 契约" { uv run python tools/generate-openapi.py }
    }

    if (-not $SkipPythonPackages) {
        Invoke-Step "构建 Python 后端分发包" {
            $distDirectory = Join-Path $repoRoot "dist"
            # 不使用 --clear：测试 API 可能正从该工作区运行，Windows 会锁住目录删除操作。
            uv build --all-packages --out-dir $distDirectory
        }
    }

    if (-not $SkipScanner) {
        $cargo = $null
        if ($env:CARGO_HOME) {
            $cargoHomeCargo = Join-Path $env:CARGO_HOME "bin\cargo.exe"
            if (Test-Path -LiteralPath $cargoHomeCargo) {
                $cargo = Get-Command $cargoHomeCargo -ErrorAction SilentlyContinue
            }
        }
        if ($null -eq $cargo) {
            $cargo = Get-Command cargo -ErrorAction SilentlyContinue
        }
        if ($null -eq $cargo) {
            throw "未找到 cargo，无法构建 Rust Scanner Supervisor。若确需跳过，请显式使用 -SkipScanner。"
        }
        else {
            $cargoPath = $cargo.Source
            Write-Host "使用 Rust 工具链: $cargoPath" -ForegroundColor DarkGray
            Invoke-Step "构建 Rust Scanner Supervisor" {
                Push-Location (Join-Path $repoRoot "apps\scanner-supervisor")
                try { & $cargoPath build --release } finally { Pop-Location }
            }
        }
    }

    if (-not $SkipNativeShim -and -not $AllowExperimentalClamAv) {
        Invoke-Step "检查官方 ClamAV runtime 与激活数据库" {
            foreach ($requiredPath in @(
                (Join-Path $clamAvRuntime "finreport_clamav.dll"),
                (Join-Path $clamAvRuntime "libclamav.dll"),
                (Join-Path $clamAvRuntime "certs\clamav.crt"),
                (Join-Path $clamAvDatabaseRoot "current.json")
            )) {
                if (-not (Test-Path -LiteralPath $requiredPath)) {
                    throw "缺少官方 ClamAV runtime/激活数据库: $requiredPath。先准备官方 runtime，或显式使用 -AllowExperimentalClamAv。"
                }
            }
            $currentPath = Join-Path $clamAvDatabaseRoot "current.json"
            try {
                $current = Get-Content -LiteralPath $currentPath -Raw | ConvertFrom-Json
                if ($current.schema -ne "finreport-clamav-current-v1" -or [string]::IsNullOrWhiteSpace([string]$current.database_id)) {
                    throw "current.json schema/database_id 无效"
                }
                $databaseRootFull = [IO.Path]::GetFullPath($clamAvDatabaseRoot)
                $candidateFull = [IO.Path]::GetFullPath((Join-Path $clamAvDatabaseRoot ([string]$current.directory)))
                $manifestFull = [IO.Path]::GetFullPath((Join-Path $clamAvDatabaseRoot ([string]$current.manifest)))
                if (-not $candidateFull.StartsWith($databaseRootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
                    -not $manifestFull.StartsWith($databaseRootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "current.json 路径越界"
                }
                $manifest = Get-Content -LiteralPath $manifestFull -Raw | ConvertFrom-Json
                if ($manifest.schema -ne "finreport-clamav-database-v1" -or
                    $manifest.database_id -ne $current.database_id -or
                    -not [bool]$manifest.official_database -or
                    @($manifest.signature_verification).Count -ne 3 -or
                    $manifest.native_probe.status -ne 0 -or
                    $manifest.native_probe.scan_rc -ne 0) {
                    throw "激活数据库 manifest 未通过官方签名/native-probe 门槛"
                }
            }
            catch {
                throw "官方 ClamAV current.json/manifest 校验失败: $($_.Exception.Message)"
            }
        }
    }
    if (-not $SkipNativeShim -and $AllowExperimentalClamAv) {
        $nativeBuildScript = Join-Path $repoRoot "native\clamav-shim\build-mingw.ps1"
        if (-not (Test-Path -LiteralPath $nativeBuildScript)) {
            throw "未找到 ClamAV shim 构建脚本。若确需跳过，请显式使用 -SkipNativeShim。"
        }
        else {
            Invoke-Step "构建 ClamAV 原生 shim" { & $nativeBuildScript }
        }
    }

    if (-not $SkipScanner -and -not $SkipNativeShim) {
        Invoke-Step "验证 ClamAV native-probe clean/infected 路径" {
            $scannerBinary = Join-Path $repoRoot "apps\scanner-supervisor\target\release\finreport-scanner-supervisor.exe"
            $shimDirectory = if ($AllowExperimentalClamAv) { Join-Path $repoRoot "tmp\clamav-experiment" } else { $clamAvRuntime }
            $shimBinary = Join-Path $shimDirectory "finreport_clamav.dll"
            $databaseDirectory = if ($AllowExperimentalClamAv) { Join-Path $repoRoot "tmp\clamav-experiment-db" } else {
                $pointer = Get-Content -LiteralPath (Join-Path $clamAvDatabaseRoot "current.json") -Raw | ConvertFrom-Json
                Join-Path $clamAvDatabaseRoot $pointer.directory
            }
            $certDirectory = Join-Path $shimDirectory "certs"
            $cleanSample = Join-Path $repoRoot "tmp\clamav-experiment-clean.bin"
            $infectedSample = if ($AllowExperimentalClamAv) { Join-Path $repoRoot "tmp\clamav-experiment-test.bin" } else { Join-Path $repoRoot "tmp\clamav-official\eicar-standard.txt" }
            foreach ($requiredPath in @($scannerBinary, $shimBinary, $databaseDirectory, $certDirectory, $cleanSample, $infectedSample)) {
                if (-not (Test-Path -LiteralPath $requiredPath)) {
                    throw "ClamAV smoke test 依赖不存在: $requiredPath"
                }
            }
            $previousPath = $env:PATH
            try {
                $env:PATH = "$shimDirectory;$previousPath"
                $cleanOutput = & $scannerBinary native-probe --shim $shimBinary --database $databaseDirectory --file $cleanSample --certs $certDirectory --json | Out-String
                if ($LASTEXITCODE -ne 0) { throw "ClamAV clean smoke test 进程失败 ($LASTEXITCODE): $cleanOutput" }
                $cleanResult = $cleanOutput | ConvertFrom-Json
                if ($cleanResult.status -ne 0 -or $cleanResult.scan_rc -ne 0) { throw "ClamAV clean smoke test verdict 错误: $cleanOutput" }
                $infectedOutput = & $scannerBinary native-probe --shim $shimBinary --database $databaseDirectory --file $infectedSample --certs $certDirectory --json | Out-String
                if ($LASTEXITCODE -ne 0) { throw "ClamAV infected smoke test 进程失败 ($LASTEXITCODE): $infectedOutput" }
                $infectedResult = $infectedOutput | ConvertFrom-Json
                if ($infectedResult.status -ne 1 -or $infectedResult.scan_rc -ne 1) { throw "ClamAV infected smoke test verdict 错误: $infectedOutput" }
            }
            finally {
                $env:PATH = $previousPath
            }
        }
        $scannerTestScript = Join-Path $repoRoot "tools\test-scanner-supervisor.py"
        if (-not $SkipTests -and (Test-Path -LiteralPath $scannerTestScript -PathType Leaf)) {
            Invoke-Step "验证正式 ClamAV Supervisor、数据库更新回滚与 API 上传门禁" {
                $databaseArgument = if ($AllowExperimentalClamAv) { @() } else { @("--database-root", $clamAvDatabaseRoot) }
                uv run python $scannerTestScript --runtime $clamAvRuntime @databaseArgument --report tmp/scanner-supervisor-integration-report.json
            }
        }
    }

    if (-not $SkipDesktop) {
        if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
            throw "未找到 dotnet。若只需构建 Python 服务，请使用 -SkipDesktop。"
        }

        Invoke-Step "构建桌面端解决方案" { dotnet build FinReport.sln --nologo }
        $desktopTestProject = Join-Path $repoRoot "tests\desktop\FinReport.Desktop.ContractTests.csproj"
        if (-not $SkipTests -and (Test-Path -LiteralPath $desktopTestProject -PathType Leaf)) {
            Invoke-Step "验证桌面 API 字段与请求契约" {
                dotnet run --project $desktopTestProject
            }
        }
    }

    Invoke-Step "整理统一测试目录" {
        $testBundleDirectory = Join-Path $repoRoot "dist\FinReport-test"
        if (Test-Path -LiteralPath $testBundleDirectory) {
            try {
                foreach ($replaceableDirectory in @("python", "desktop", "scanner", "clamav", "clamav-updater")) {
                    $replaceablePath = Join-Path $testBundleDirectory $replaceableDirectory
                    if (Test-Path -LiteralPath $replaceablePath) {
                        Remove-Item -LiteralPath $replaceablePath -Recurse -Force -ErrorAction Stop
                    }
                }
                foreach ($generatedFile in @("run-api.cmd", "run-api.py", "run-desktop.cmd", "run-all.cmd", "README.txt")) {
                    $generatedPath = Join-Path $testBundleDirectory $generatedFile
                    if (Test-Path -LiteralPath $generatedPath) {
                        Remove-Item -LiteralPath $generatedPath -Force -ErrorAction Stop
                    }
                }
            }
            catch {
                $suffix = Get-Date -Format "yyyyMMdd-HHmmss"
                $testBundleDirectory = Join-Path $repoRoot "dist\FinReport-test-$suffix"
                Write-Warning "现有测试目录正在使用，改用新的测试目录: $testBundleDirectory"
            }
        }
        $bundlePython = Join-Path $testBundleDirectory "python"
        $bundleDesktop = Join-Path $testBundleDirectory "desktop"
        $bundleScanner = Join-Path $testBundleDirectory "scanner"
        $bundleClamAv = Join-Path $testBundleDirectory "clamav"
        $bundleClamAvUpdater = Join-Path $testBundleDirectory "clamav-updater"
        $bundleData = Join-Path $testBundleDirectory "data\storage"
        New-Item -ItemType Directory -Force -Path $bundlePython, $bundleData | Out-Null
        Copy-Item -LiteralPath (Join-Path $repoRoot "tools\run-bundled-api.py") -Destination (Join-Path $testBundleDirectory "run-api.py") -Force

        if (-not $SkipPythonPackages) {
            Get-ChildItem -LiteralPath (Join-Path $repoRoot "dist") -File -Include *.whl, *.tar.gz |
                Copy-Item -Destination $bundlePython -Force
        }

        if (-not $SkipDesktop) {
            $desktopOutput = Join-Path $repoRoot "apps\desktop\FinReport.Desktop\bin\Debug\net10.0-windows"
            if (-not (Test-Path -LiteralPath $desktopOutput)) {
                throw "桌面端构建目录不存在: $desktopOutput"
            }
            New-Item -ItemType Directory -Force -Path $bundleDesktop | Out-Null
            Copy-Item -Path (Join-Path $desktopOutput "*") -Destination $bundleDesktop -Recurse -Force
        }

        $scannerBinary = Join-Path $repoRoot "apps\scanner-supervisor\target\release\finreport-scanner-supervisor.exe"
        if (-not $SkipScanner) {
            if (-not (Test-Path -LiteralPath $scannerBinary)) {
                throw "Scanner Supervisor 构建产物不存在: $scannerBinary"
            }
            New-Item -ItemType Directory -Force -Path $bundleScanner | Out-Null
            Copy-Item -LiteralPath $scannerBinary -Destination $bundleScanner -Force
        }

        $shimDirectory = if ($AllowExperimentalClamAv) { Join-Path $repoRoot "tmp\clamav-experiment" } else { $clamAvRuntime }
        $databaseDirectory = if ($AllowExperimentalClamAv) { Join-Path $repoRoot "tmp\clamav-experiment-db" } else { $clamAvDatabaseRoot }
        if (-not $SkipNativeShim) {
            $shimBinary = Join-Path $shimDirectory "finreport_clamav.dll"
            $certDirectory = Join-Path $shimDirectory "certs"
            if (-not (Test-Path -LiteralPath $shimBinary)) {
                throw "ClamAV shim 构建产物不存在: $shimBinary"
            }
            $databaseFiles = @(Get-ChildItem -LiteralPath $databaseDirectory -File -Recurse -Include *.cvd, *.cld, *.hdb, *.hsb, *.hdu, *.fp, *.crtdb, *.cdb, *.ndb, *.ldb, *.mdb, *.sfp)
            if ($databaseFiles.Count -eq 0) {
                throw "ClamAV 数据库目录为空: $databaseDirectory"
            }
            if (-not (Test-Path -LiteralPath (Join-Path $certDirectory "clamav.crt"))) {
                throw "ClamAV 证书目录不存在或为空: $certDirectory"
            }
            New-Item -ItemType Directory -Force -Path $bundleClamAv | Out-Null
            Copy-Item -Path (Join-Path $shimDirectory "*") -Destination $bundleClamAv -Recurse -Force
            $clamAvInstall = if ($AllowExperimentalClamAv) { Join-Path $repoRoot "tmp\clamav-install-154" } else { $clamAvRuntime }
            Copy-Item -LiteralPath (Join-Path $clamAvInstall "COPYING.txt") -Destination $bundleClamAv -Force
            if (Test-Path -LiteralPath (Join-Path $clamAvInstall "COPYING")) { Copy-Item -LiteralPath (Join-Path $clamAvInstall "COPYING") -Destination $bundleClamAv -Recurse -Force }
            $bundleDatabase = Join-Path $bundleClamAv "db"
            New-Item -ItemType Directory -Force -Path $bundleDatabase | Out-Null
            Copy-Item -Path (Join-Path $databaseDirectory "*") -Destination $bundleDatabase -Recurse -Force
            $bundleProbeFile = Join-Path $testBundleDirectory "clamav-clean-probe.bin"
            $probeSource = Join-Path $repoRoot "tmp\clamav-experiment-clean.bin"
            if (-not (Test-Path -LiteralPath $probeSource -PathType Leaf)) {
                throw "ClamAV 更新探针文件不存在: $probeSource"
            }
            Copy-Item -LiteralPath $probeSource -Destination $bundleProbeFile -Force
            # ClamAV 1.5.4's Windows runtime resolves its compiled-in certs
            # directory beside the probe executable during engine creation.
            $scannerCerts = Join-Path $bundleScanner "certs"
            New-Item -ItemType Directory -Force -Path $scannerCerts | Out-Null
            Copy-Item -Path (Join-Path $certDirectory "*") -Destination $scannerCerts -Recurse -Force
            if (-not $AllowExperimentalClamAv -and (Test-Path -LiteralPath (Join-Path $clamAvUpdater "freshclam.exe"))) {
                New-Item -ItemType Directory -Force -Path $bundleClamAvUpdater | Out-Null
                foreach ($updaterFile in @("freshclam.exe", "libfreshclam.dll", "sigtool.exe", "COPYING.txt")) {
                    $source = Join-Path $clamAvUpdater $updaterFile
                    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $bundleClamAvUpdater -Force }
                }
                Copy-Item -Path (Join-Path $clamAvUpdater "*.dll") -Destination $bundleClamAvUpdater -Force
                if (Test-Path -LiteralPath (Join-Path $clamAvUpdater "COPYING")) { Copy-Item -LiteralPath (Join-Path $clamAvUpdater "COPYING") -Destination $bundleClamAvUpdater -Recurse -Force }
                Copy-Item -LiteralPath (Join-Path $clamAvUpdater "certs") -Destination $bundleClamAvUpdater -Recurse -Force -ErrorAction SilentlyContinue
                # Keep freshclam configuration inside the bundle. The staging
                # helper overrides DatabaseDirectory for each candidate, while
                # the certificate directory remains bundle-local.
                $bundleFreshclamConfig = Join-Path $bundleClamAvUpdater "freshclam.conf"
                $bundleCertPath = (Join-Path $bundleClamAvUpdater "certs").Replace("\", "\\")
                @(
                    "DatabaseDirectory $bundleCertPath",
                    "CVDCertsDirectory $bundleCertPath",
                    "DatabaseMirror database.clamav.net",
                    "Checks 1",
                    "MaxAttempts 2",
                    "ConnectTimeout 30",
                    "ReceiveTimeout 300",
                    "TestDatabases yes",
                    "Bytecode yes",
                    "Foreground yes"
                ) | Set-Content -LiteralPath $bundleFreshclamConfig -Encoding ascii
                Copy-Item -LiteralPath (Join-Path $repoRoot "tools\update-clamav-database.ps1") -Destination $testBundleDirectory -Force
            }
        }

        $repoRootForScript = $repoRoot.Replace("'", "''")
        $runApi = @"
@echo off
setlocal
set "FINREPORT_DATABASE_URL=sqlite:///%~dp0data/finreport.db"
set "FINREPORT_STORAGE_DIR=%~dp0data/storage"
set "FINREPORT_AV_MODE=supervisor"
set "FINREPORT_AV_NATIVE_DATABASE=%~dp0clamav\db"
set "FINREPORT_AV_NATIVE_SHIM=%~dp0clamav\finreport_clamav.dll"
set "FINREPORT_AV_NATIVE_CERTS=%~dp0clamav\certs"
set "FINREPORT_SCANNER_CONNECT_TIMEOUT_SECONDS=90"
set "FINREPORT_AV_UPDATE_ENABLED=true"
set "FINREPORT_AV_UPDATE_SCRIPT=%~dp0update-clamav-database.ps1"
set "FINREPORT_AV_UPDATE_FRESHCLAM=%~dp0clamav-updater\freshclam.exe"
set "FINREPORT_AV_UPDATE_SIGTOOL=%~dp0clamav-updater\sigtool.exe"
set "FINREPORT_AV_UPDATE_CONFIG=%~dp0clamav-updater\freshclam.conf"
set "FINREPORT_AV_UPDATE_SIGNATURE_CERTS=%~dp0clamav-updater\certs"
set "FINREPORT_AV_UPDATE_PROBE_EXECUTABLE=%~dp0scanner\finreport-scanner-supervisor.exe"
set "FINREPORT_AV_UPDATE_PROBE_FILE=%~dp0clamav-clean-probe.bin"
set "PATH=%~dp0clamav;%PATH%"
pushd "$repoRootForScript"
uv run python "%~dp0run-api.py" --bundle "%~dp0." %*
set "exit_code=%ERRORLEVEL%"
popd
exit /b %exit_code%
"@
        $runDesktop = @"
@echo off
start "FinReport Desktop" "%~dp0desktop\FinReport.Desktop.exe"
"@
        $runAll = @"
@echo off
start "FinReport API" cmd /k call "%~dp0run-api.cmd"
timeout /t 2 /nobreak >nul
start "FinReport Desktop" "%~dp0desktop\FinReport.Desktop.exe"
"@
        $bundleReadme = @"
FinReport 测试构建目录

启动 API：双击 run-api.cmd
启动桌面端：双击 run-desktop.cmd（需要先启动 API）

目录：
desktop  WPF 桌面端
python   Python 后端 wheel 和源码包
scanner  Rust Scanner Supervisor
clamav   ClamAV shim 及运行库
data     本地测试数据库和对象存储
"@
        Write-CmdFile (Join-Path $testBundleDirectory "run-api.cmd") $runApi
        Write-CmdFile (Join-Path $testBundleDirectory "run-desktop.cmd") $runDesktop
        Write-CmdFile (Join-Path $testBundleDirectory "run-all.cmd") $runAll
        [IO.File]::WriteAllText((Join-Path $testBundleDirectory "README.txt"), $bundleReadme, [Text.UTF8Encoding]::new($false))
        Write-Host "统一测试目录: $testBundleDirectory" -ForegroundColor Green
    }

    Write-Host "`n构建和测试全部通过。" -ForegroundColor Green
}
finally {
    Pop-Location
}
