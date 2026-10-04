$ErrorActionPreference = "Stop"

# Keep dependency direction visible in CI until a dedicated import-linter is introduced.
$domain = Get-ChildItem packages/finance-domain/src -Recurse -Filter *.py
if ($domain | Select-String -Pattern '^\s*(from|import)\s+(fastapi|sqlalchemy|celery|httpx)') { throw "Domain imports an outer framework" }
$application = Get-ChildItem packages/finance-application/src -Recurse -Filter *.py
if ($application | Select-String -Pattern '^\s*(from|import)\s+(fastapi|sqlalchemy|celery)') { throw "Application imports a host or adapter framework" }
Write-Output "Architecture dependency checks passed."
