"""Official Docling MCP server plus a read-only native-mode receipt."""
from __future__ import annotations

import hashlib
import importlib.metadata
import os
from pathlib import Path

from docling_mcp.settings.service_client import settings
from docling_mcp.shared import mcp

ROOT = Path(__file__).resolve().parents[2]


@mcp.tool(name="finreport-docling-status")
def native_status() -> dict:
    """Return the effective configuration used by the official converter."""
    weights = ROOT / "data/local-services/mcp/model-manifest.json"
    return {"conversion_mode": settings.conversion_mode.value, "do_ocr": settings.do_ocr,
            "do_table_structure": settings.do_table_structure,
            "do_picture_description": settings.do_picture_description,
            "docling_mcp_version": importlib.metadata.version("docling-mcp"),
            "docling_version": importlib.metadata.version("docling-slim"),
            "docling_core_version": importlib.metadata.version("docling-core"),
            "torch_version": importlib.metadata.version("torch"),
            "device": os.environ.get("DOCLING_DEVICE", "auto"),
            "weight_manifest_sha256": hashlib.sha256(weights.read_bytes()).hexdigest() if weights.is_file() else None}


if __name__ == "__main__":
    from docling_mcp.servers.mcp_server import app
    app()
