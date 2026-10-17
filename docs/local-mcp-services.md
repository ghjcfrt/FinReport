# Local MCP services

The local services are deliberately isolated from the application environment under
`data/local-services/mcp` (ignored runtime data). Qdrant uses the official
`mcp-server-qdrant==0.8.1` package and CPU FastEmbed `BAAI/bge-small-zh-v1.5`.
Docling uses official `docling-mcp` commit
`5e1551a9b984f9a26349de6d23d02eccf5a69db2` (3.3.0) with `docling-slim[standard]`.
The MCP SDK versions are incompatible, so each service has its own uv venv.

Start and health check services from the repository root:

```powershell
./tools/local-mcp/start-qdrant.ps1
./tools/local-mcp/health-qdrant.ps1
./tools/local-mcp/start-docling.ps1
./tools/local-mcp/health-docling.ps1
```

Stop them with `stop-qdrant.ps1` and `stop-docling.ps1`. Both launchers run
hidden background processes and write logs and PID files under the local service
directory. Docling is explicitly local/native with `DOCLING_MCP_DO_OCR=false`,
CPU device, four threads, and no OCR/VLM model. The adapter creates a PDF subset
from the requested original page numbers. Desktop full-report processing requires
an explicit choice; a selected-page extraction never silently converts other pages.
When a port is already listening, each launcher verifies the owning Python
environment, server script, and an MCP `initialize` response before reusing it;
an unrelated listener or failed identity probe is an error.
The process check matches the absolute project script and isolated Python
environment, including uv's Windows launcher/base-interpreter parent relationship.
Stop scripts verify these identities again before stopping a listener or a saved
launcher PID; stale PID files cannot authorize stopping an unrelated process.

Local endpoints are Qdrant `http://127.0.0.1:18768/mcp/` (trailing slash),
Docling `http://127.0.0.1:18769/mcp`, and the separately installed Unlimited-OCR
`http://127.0.0.1:8765/v1`. The repository `.env` enables these noncredential
settings; restart the API after changing them. `.env.example` documents optional
equivalents without copying account credentials. The app does not launch services.

`DoclingMcpClient.convert_pages` returns a manifest, the complete Docling JSON,
Markdown reading copy, page mapping back to original PDF pages, page/text/raw
hashes, heading path, parent anchor, table provenance, and bounding boxes. It
rejects remote server paths and paths outside the configured output directory.
Markdown is not the authoritative structure. Docling text remains a searchable
candidate and cannot approve strict financial facts.

The Qdrant service was verified with actual CPU embedding weights by
`uv run --no-sync python tools/local-mcp/verify_qdrant_four.py`, which converts
one explicitly selected original page from each independent 2025 Q1, H1, Q3,
and annual report (pages 2, 7, 9, and 11 in the recorded run) and queries each
filing with an explicit metadata filter. The recorded smoke test proves actual
embedding-backed retrieval, isolation and repeatability on those pages; it does
not measure a full-report recall rate. The synthetic protocol probe in `tmp/`
is separate and does not count as semantic validation.

The saved proof is `data/local-services/mcp/verification/four-reports.json`;
the separate SQLite fixture is `evidence.db` in that directory (both ignored
runtime output). It records the Qdrant generation UUID, model/vector identity,
source PDF SHA-256, original page numbers, Docling manifest/JSON hashes,
idempotency, per-filing isolation, explicit four-filing joint mode, review
binding invalidation, and the degraded SQLite result when the configured MCP
URL is absent. `tools/local-mcp/rebuild-qdrant.ps1` stops the service, moves the
old local index to a timestamped sibling, and starts a new generation; it never
deletes the previous index automatically.

The two lock files in `tools/local-mcp` are generated from the isolated uv
environments. `install.ps1` recreates the environments and downloads only the
Docling layout/table models into the project-local model directory. Qdrant's
first start downloads its FastEmbed model there. `model_manifest.py` records
SHA-256 and size for all local weight files; model manifests are included in
Docling conversion cache keys so changing weights cannot reuse old structure.
