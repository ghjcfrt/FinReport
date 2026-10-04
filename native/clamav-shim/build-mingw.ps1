[CmdletBinding()]
param(
    [string]$ClamAvRoot = (Join-Path $PSScriptRoot "..\..\tmp\clamav-install-154"),
    [string]$VcpkgRoot = (Join-Path $PSScriptRoot "..\..\tmp\vcpkg"),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\..\tmp\clamav-experiment")
)

$ErrorActionPreference = "Stop"
$gcc = "E:\mingw64\bin\x86_64-w64-mingw32-gcc.exe"
if (-not (Test-Path $gcc)) { throw "MinGW compiler not found: $gcc" }

$ClamAvRoot = [IO.Path]::GetFullPath($ClamAvRoot)
$VcpkgRoot = [IO.Path]::GetFullPath($VcpkgRoot)
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$vcpkgInclude = Join-Path $VcpkgRoot "installed\x64-mingw-dynamic\include"
$libclamav = Join-Path $ClamAvRoot "libclamav.dll.a"
if (-not (Test-Path (Join-Path $ClamAvRoot "include\clamav.h"))) { throw "ClamAV headers not found: $ClamAvRoot" }
if (-not (Test-Path $libclamav)) { throw "ClamAV import library not found: $libclamav" }
if (-not (Test-Path $vcpkgInclude)) { throw "vcpkg headers not found: $vcpkgInclude" }

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$output = Join-Path $OutputDirectory "finreport_clamav.dll"
& $gcc -shared -O2 -Wall -Wextra -DWIN32_LEAN_AND_MEAN `
    "-I$PSScriptRoot" "-I$(Join-Path $ClamAvRoot 'include')" "-I$vcpkgInclude" `
    (Join-Path $PSScriptRoot "finreport_clamav.c") $libclamav `
    -o $output
if ($LASTEXITCODE -ne 0) { throw "MinGW shim build failed ($LASTEXITCODE)" }

Copy-Item -Force (Join-Path $ClamAvRoot "*.dll") $OutputDirectory
Copy-Item -Force "E:\mingw64\bin\libgcc_s_seh-1.dll", "E:\mingw64\bin\libwinpthread-1.dll" $OutputDirectory
Write-Host "Experimental shim runtime: $OutputDirectory"
