$ErrorActionPreference = "Stop"
$contract = Join-Path $PSScriptRoot "..\contracts\openapi\v1.json"
if (-not (Test-Path $contract)) { throw "OpenAPI contract not found: $contract" }
Write-Output "OpenAPI contract available: $contract"
Write-Output "Use NSwag or Kiota in the deployment environment to regenerate apps/desktop/FinReport.Desktop/Generated."
