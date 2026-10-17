$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$base = (Resolve-Path (Join-Path $root 'data/local-services/mcp')).Path
$target = Join-Path $base 'qdrant-data'
$backup = Join-Path $base ('qdrant-data-backup-' + (Get-Date -Format 'yyyyMMddHHmmss'))
if ((Split-Path -Parent $target) -ne $base -or (Split-Path -Parent $backup) -ne $base) { throw 'Unsafe index directory' }
& (Join-Path $PSScriptRoot 'stop-qdrant.ps1')
if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $backup }
& (Join-Path $PSScriptRoot 'start-qdrant.ps1')
Write-Output "Old rebuildable index retained: $backup. New generation will force the next explicit FinReport indexing operation to reimport SQLite snapshots."
