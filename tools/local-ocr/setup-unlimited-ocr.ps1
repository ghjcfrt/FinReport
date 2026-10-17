$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$python = Join-Path $root "data\local-services\unlimited-ocr\.venv\Scripts\python.exe"
uv venv data/local-services/unlimited-ocr/.venv --python 3.11 --allow-existing
if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
uv pip install --python $python torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw "CUDA PyTorch installation failed" }
uv pip install --python $python transformers==4.57.1 accelerate==1.15.0 bitsandbytes==0.50.2 fastapi==0.143.2 uvicorn==0.54.0 einops==0.8.2 addict==2.4.0 easydict==1.13 pymupdf==1.28.2 psutil==7.2.2 requests==2.34.2 matplotlib==3.10.8
if ($LASTEXITCODE -ne 0) { throw "OCR dependency installation failed" }
& $python tools/local-ocr/download_model.py
if ($LASTEXITCODE -ne 0) { throw "Pinned model verification failed" }
