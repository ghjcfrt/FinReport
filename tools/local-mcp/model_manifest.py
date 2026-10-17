"""Write a deterministic SHA-256 inventory of project-local model files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
base = ROOT / "data/local-services/mcp"
files = []
for path in sorted((base / "models").rglob("*")):
    if not path.is_file() or any(part in {".cache", ".locks", "logs", "xet"} for part in path.parts):
        continue
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    files.append({"path": path.relative_to(base).as_posix(), "size": path.stat().st_size,
                  "sha256": digest})
manifest = {"version": "local-mcp-weights-v1", "files": files}
target = base / "model-manifest.json"
target.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"files={len(files)} manifest_sha256={hashlib.sha256(target.read_bytes()).hexdigest()}")
