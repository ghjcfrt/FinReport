$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$base = Join-Path $root 'data/local-services/mcp'
New-Item -ItemType Directory -Force $base,(Join-Path $base 'logs'),(Join-Path $base 'run') | Out-Null
foreach ($service in @('qdrant','docling')) {
    $venv = Join-Path $base ($service + '-venv')
    if (-not (Test-Path (Join-Path $venv 'Scripts/python.exe'))) { & uv venv --python 3.11 $venv; if ($LASTEXITCODE -ne 0) { throw "uv venv failed: $service" } }
    $python = Join-Path $venv 'Scripts/python.exe'
    & uv pip sync --python $python (Join-Path $PSScriptRoot ($service + '-requirements.lock')) --index https://download.pytorch.org/whl/cpu --index https://pypi.org/simple --index-strategy unsafe-best-match
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed: $service" }
}
$env:HF_HOME = Join-Path $base 'models/huggingface'
& (Join-Path $base 'docling-venv/Scripts/docling-tools.exe') models download layout tableformer --output-dir (Join-Path $base 'models/docling')
if ($LASTEXITCODE -ne 0) { throw 'Docling model download failed' }
Write-Output 'Installed isolated services. Start Qdrant once to populate its FastEmbed cache, then run model_manifest.py with the project Python.'
