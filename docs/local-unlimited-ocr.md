# Local Unlimited-OCR deployment

This deployment uses Baidu's official `baidu/Unlimited-OCR` checkpoint from Hugging Face revision `8249d20b2822a7d8a4c22c8d8c9069b5ac36bda3` (MIT license, GitHub commit `92ad94b53a51950fea0416aced42331dfb4dd59e`). The model has 3,336,106,240 BF16 parameters; the safetensors artifact is 6,672,547,120 bytes. The SHA-256 is `2bc48a7a110061ea58fff65d3169367eebe3aee371ca6968dc2219c1b2855fc6`. The local artifact manifest records file sizes, hashes, and the source patch in `data/local-services/unlimited-ocr/evidence/artifact-manifest.json`.

Windows PowerShell setup uses Python 3.11 and an isolated uv environment. The service uses the official Transformers remote-code implementation, CUDA BF16 compute, and bitsandbytes NF4 loading to fit an RTX 4060 Laptop GPU. The service is an OpenAI-compatible HTTP boundary and exposes `GET /health`, `GET /v1/models`, and `POST /v1/chat/completions`. It only accepts a single `data:image/*;base64` image and returns real model output; no mock fallback exists.

```powershell
powershell -ExecutionPolicy Bypass -File tools/local-ocr/setup-unlimited-ocr.ps1
powershell -ExecutionPolicy Bypass -File tools/local-ocr/start-unlimited-ocr.ps1
Invoke-RestMethod http://127.0.0.1:8765/health
```

启动器遇到占用端口时会核对监听进程、脚本路径、Python 环境、端口参数和 `/health` 中的模型 revision、权重 SHA-256；任何一项不匹配都会拒绝复用。`health-unlimited-ocr.ps1` 同样会拒绝未证明身份的 ready 响应。

Windows 上 uv 的虚拟环境启动器可能把监听交给基础 Python 子进程；启动和停止脚本会核对完整项目脚本路径与虚拟环境父进程。模型加载失败或进程提前退出会立即报告。每个推理请求使用项目 `data/local-services/unlimited-ocr/tmp` 下的独立临时目录，成功和失败后均自动清理。

Configure FinReport's existing model client with provider `baidu-unlimited-ocr`, model `Unlimited-OCR`, base URL `http://127.0.0.1:8765/v1`, protocol `openai_compatible`, timeout `1200`, and API key `local-only` (the loopback service does not require authentication). The existing OpenAI payload path is compatible with this endpoint. Start/stop scripts run the service in a hidden process and keep logs under `data/local-services/unlimited-ocr`.

```powershell
powershell -ExecutionPolicy Bypass -File tools/local-ocr/health-unlimited-ocr.ps1
powershell -ExecutionPolicy Bypass -File tools/local-ocr/stop-unlimited-ocr.ps1
```

The setup script uses the existing Python 3.11 interpreter and downloads the pinned checkpoint into project-local ignored data. No global dependency installation is needed. Installed dependency versions are preserved in `data/local-services/unlimited-ocr/evidence/requirements-freeze.txt`. Official README and GitHub/Hugging Face metadata are preserved alongside them. The remote-code model includes Apache-2.0 DeepSeek-derived implementation headers in addition to the checkpoint repository's MIT license.

The official CUDA inference implementation encountered `masked_scatter_: expected self and source to have same dtypes but got BFloat16 and Float` after NF4 loading. `download_model.py` makes one explicit dtype conversion at that scatter boundary in the downloaded local source. The checkpoint weights remain byte-identical. The first failed stage and successful configuration are recorded in `data/local-services/unlimited-ocr/evidence/deployment.json`.

The first real probe used BYD 2025 Q1 filing, physical page 7, the consolidated income statement. Evidence is in `tmp/local-integration-20261016/ocr/byd-2025q1-page7.result.json` with rendered image SHA-256 `AE4D25ACFB203A7ED14DB7FD1C4DAD1A7997E7FC9AD82249A6E3B973F8CF38FE`. The service reached `ready` in 17.537 seconds; OCR took 89.733 seconds, used 3,043,663,360 bytes peak CUDA allocation, and returned Chinese table hierarchy, unit `元`, and values including `营业总收入 170,360,448,000.00 / 124,944,397,000.00` and `净利润 9,443,140,000.00 / 4,770,879,000.00`. The output is stored both as JSON and UTF-8 text.

The second real probe, physical page 2, took 64.257 seconds. Both probes retain `filing_version_id=byd-2025q1`, physical page, 144-DPI screenshot hash, PDF hash, output, and timing. `validation.json` compares every comma-formatted amount cell including duplicate occurrences against the PDF's embedded text: page 2 has 20 matching cells; page 7 has 70 matching cells; neither has missing or extra amounts. This validates those sampled amounts, not full-document accuracy. Page 7's raw model HTML omits one closing `</td>`; downstream consumers need tolerant HTML parsing. GPU memory observed by `nvidia-smi` was approximately 5.8 GiB including other Windows GPU use; the CUDA peak above measures PyTorch allocations only.

Only selected pages should be probed. For example:

```powershell
data/local-services/unlimited-ocr/.venv/Scripts/python.exe tools/local-ocr/probe_unlimited_ocr.py docs/比亚迪：2025年一季度报告.pdf --page 7 --filing-id byd-2025q1
```

The official documented inference entry point calls CUDA and BF16 explicitly; a CPU backend has not been validated here. NF4 is a local resource adaptation, not a changed checkpoint. The service currently serializes requests and caps generation at 8,192 tokens. It does not process complete filings or upload any document. The HTML preserves textual markers such as `其中` and `(一)`, but does not encode every indentation-derived parent relationship as structured metadata. Financial hierarchy therefore still requires downstream validation.
