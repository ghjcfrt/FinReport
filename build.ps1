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
    [switch]$RequireNativeShim
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
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
            if ($RequireScanner) {
                throw "未找到 cargo，无法构建 Rust Scanner Supervisor。请安装 Rust，或去掉 -RequireScanner 以跳过。"
            }
            Write-Warning "未找到 cargo，已跳过 Rust Scanner Supervisor 构建；如需强制构建请使用 -RequireScanner。"
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

    if (-not $SkipNativeShim) {
        $nativeBuildScript = Join-Path $repoRoot "native\clamav-shim\build-mingw.ps1"
        if (-not (Test-Path -LiteralPath $nativeBuildScript)) {
            if ($RequireNativeShim) { throw "未找到 ClamAV shim 构建脚本。" }
            Write-Warning "未找到 ClamAV shim 构建脚本，已跳过。"
        }
        else {
            try {
                Invoke-Step "构建 ClamAV 原生 shim" { & $nativeBuildScript }
            }
            catch {
                if ($RequireNativeShim) { throw }
                Write-Warning "ClamAV 原生 shim 构建失败，已跳过：$($_.Exception.Message)"
            }
        }
    }

    if (-not $SkipDesktop) {
        if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
            throw "未找到 dotnet。若只需构建 Python 服务，请使用 -SkipDesktop。"
        }

        Invoke-Step "构建桌面端解决方案" { dotnet build FinReport.sln --nologo }
    }

    Invoke-Step "整理统一测试目录" {
        $testBundleDirectory = Join-Path $repoRoot "dist\FinReport-test"
        if (Test-Path -LiteralPath $testBundleDirectory) {
            try {
                foreach ($replaceableDirectory in @("python", "desktop", "scanner", "clamav")) {
                    $replaceablePath = Join-Path $testBundleDirectory $replaceableDirectory
                    if (Test-Path -LiteralPath $replaceablePath) {
                        Remove-Item -LiteralPath $replaceablePath -Recurse -Force -ErrorAction Stop
                    }
                }
                foreach ($generatedFile in @("run-api.cmd", "run-desktop.cmd", "run-all.cmd", "README.txt")) {
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
        $bundleData = Join-Path $testBundleDirectory "data\storage"
        New-Item -ItemType Directory -Force -Path $bundlePython, $bundleData | Out-Null

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
        if (Test-Path -LiteralPath $scannerBinary) {
            New-Item -ItemType Directory -Force -Path $bundleScanner | Out-Null
            Copy-Item -LiteralPath $scannerBinary -Destination $bundleScanner -Force
        }

        $shimDirectory = Join-Path $repoRoot "tmp\clamav-experiment"
        if (Test-Path -LiteralPath $shimDirectory) {
            New-Item -ItemType Directory -Force -Path $bundleClamAv | Out-Null
            Copy-Item -Path (Join-Path $shimDirectory "*") -Destination $bundleClamAv -Recurse -Force
        }

        $repoRootForScript = $repoRoot.Replace("'", "''")
        $runApi = @"
@echo off
setlocal
set "FINREPORT_DATABASE_URL=sqlite:///%~dp0data/finreport.db"
set "FINREPORT_STORAGE_DIR=%~dp0data/storage"
pushd "$repoRootForScript"
uv run uvicorn finreport_api.main:app --host 127.0.0.1 --port 8000 %*
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
