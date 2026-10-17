"""Audited client for the official Docling MCP conversion tools.

The client only accepts server-discovered tools and converts caller-created
single-page PDFs.  Docling JSON is a structural preview; financial facts still
come from the existing page/hash/audit pipeline.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pymupdf


class DoclingMcpError(RuntimeError):
    """A protocol, service, or provenance validation failure."""


class DoclingMcpClient:
    def __init__(self, *, url: str, output_dir: str | Path, timeout_seconds: float = 60.0):
        self.url = url.rstrip("/")
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self._session_id: str | None = None
        self._request_id = 0
        self._tools: dict[str, dict[str, Any]] | None = None

    def _rpc(self, method: str, params: dict[str, Any] | None = None, *, notification=False) -> dict:
        self._request_id += 1
        request: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            request["params"] = params
        if not notification:
            request["id"] = self._request_id
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "MCP-Protocol-Version": "2025-03-26"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            response = httpx.post(self.url, json=request, headers=headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            if response.headers.get("mcp-session-id"):
                self._session_id = response.headers["mcp-session-id"]
        except httpx.HTTPError as exc:
            raise DoclingMcpError(f"DOCLING_MCP_REQUEST_FAILED:{type(exc).__name__}") from exc
        if notification or response.status_code == 202:
            return {}
        try:
            if "text/event-stream" in response.headers.get("content-type", ""):
                decoded = None
                for event in re.split(r"\r?\n\r?\n", response.text):
                    data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                    if data:
                        candidate = json.loads(data)
                        if candidate.get("id") == request.get("id"):
                            decoded = candidate
                if decoded is None:
                    raise ValueError("missing response event")
            else:
                decoded = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise DoclingMcpError("DOCLING_MCP_INVALID_JSON") from exc
        if not isinstance(decoded, dict) or "error" in decoded:
            raise DoclingMcpError("DOCLING_MCP_PROTOCOL_ERROR")
        result = decoded.get("result", {})
        if not isinstance(result, dict):
            raise DoclingMcpError("DOCLING_MCP_RESULT_INVALID")
        return result

    def _initialize(self) -> None:
        if self._tools is not None:
            return
        self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "FinReport", "version": "docling-mcp-v1"}})
        self._rpc("notifications/initialized", notification=True)
        result = self._rpc("tools/list", {})
        self._tools = {tool["name"]: tool for tool in result.get("tools", [])
                       if isinstance(tool, dict) and isinstance(tool.get("name"), str)}

    def _call(self, name: str, arguments: dict[str, Any]) -> dict:
        self._initialize()
        assert self._tools is not None
        tool = self._tools.get(name)
        if tool is None:
            raise DoclingMcpError(f"DOCLING_MCP_TOOL_NOT_FOUND:{name}")
        properties = tool.get("inputSchema", {}).get("properties", {})
        if not isinstance(properties, dict):
            raise DoclingMcpError(f"DOCLING_MCP_SCHEMA_INVALID:{name}")
        accepted = {key: value for key, value in arguments.items() if key in properties}
        result = self._rpc("tools/call", {"name": name, "arguments": accepted})
        if result.get("isError"):
            raise DoclingMcpError(f"DOCLING_MCP_TOOL_FAILED:{name}")
        return result

    def native_identity(self) -> dict[str, Any]:
        identity = self._structured(self._call("finreport-docling-status", {}))
        if identity.get("conversion_mode") != "local" or identity.get("do_ocr") is not False:
            raise DoclingMcpError("DOCLING_NATIVE_MODE_REQUIRED")
        if identity.get("do_picture_description") is not False:
            raise DoclingMcpError("DOCLING_PICTURE_DESCRIPTION_DISABLED_REQUIRED")
        return identity

    @staticmethod
    def _structured(result: dict) -> dict[str, Any]:
        value = result.get("structuredContent")
        if isinstance(value, dict):
            return value
        for block in result.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    parsed = json.loads(block.get("text", ""))
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
        raise DoclingMcpError("DOCLING_MCP_OUTPUT_INVALID")

    def convert_pages(self, source: str | Path, page_numbers: list[int], *,
                      source_sha256: str | None = None,
                      artifact_dir: str | Path | None = None) -> dict[str, Any]:
        # The official save tool returns server-local paths. Do not silently
        # open those on a different client machine; remote deployments need
        # a separate, explicitly mapped shared-filesystem adapter.
        if urlparse(self.url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise DoclingMcpError("DOCLING_REMOTE_SHARED_PATH_REQUIRED")
        source_path = Path(source).resolve()
        if not source_path.is_file() or not page_numbers:
            raise DoclingMcpError("DOCLING_SOURCE_INVALID")
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if source_sha256 is not None and source_sha256 != digest:
            raise DoclingMcpError("DOCLING_SOURCE_HASH_MISMATCH")
        pages = sorted(set(page_numbers))
        if any(type(page) is not int or page < 1 for page in pages):
            raise DoclingMcpError("DOCLING_PAGE_RANGE_INVALID")
        identity = self.native_identity()
        scope_hash = _hash_json({"version": "docling-native-v1", "source": digest, "pages": pages,
                                 "url": self.url, "native_identity": identity})
        artifact_root = Path(artifact_dir or self.output_dir / "artifacts").resolve()
        artifact = artifact_root / digest / scope_hash
        artifact.mkdir(parents=True, exist_ok=True)
        manifest_path = artifact / "manifest.json"
        if manifest_path.is_file():
            try:
                cached = json.loads(manifest_path.read_text(encoding="utf-8"))
                json_bytes = (artifact / "document.json").read_bytes()
                subset_bytes = (artifact / "source-pages.pdf").read_bytes()
                if (cached.get("source_sha256") == digest and cached.get("page_numbers") == pages
                        and cached.get("native_identity") == identity
                        and cached.get("json_file") == str(artifact / "document.json")
                        and cached.get("md_file") == str(artifact / "document.md")
                        and cached.get("markdown_sha256") == hashlib.sha256((artifact / "document.md").read_bytes()).hexdigest()
                        and cached.get("page_mapping") == {str(index): page for index, page in enumerate(pages, 1)}
                        and cached.get("json_sha256") == hashlib.sha256(json_bytes).hexdigest()
                        and cached.get("subset_sha256") == hashlib.sha256(subset_bytes).hexdigest()):
                    doc = json.loads(json_bytes)
                    _validate_docling_document(doc, artifact / "source-pages.pdf", len(pages))
                    return {**cached, "manifest_file": str(manifest_path), "cached": True,
                            "chunks": _structured_chunks(doc, cached)}
            except (ValueError, OSError, DoclingMcpError):
                pass
        with pymupdf.open(source_path) as document:
            if any(page > len(document) for page in pages):
                raise DoclingMcpError("DOCLING_PAGE_RANGE_INVALID")
            subset = artifact / "source-pages.pdf"
            page_hashes = {str(page): hashlib.sha256(
                (document[page - 1].get_text("text") or "").encode("utf-8")).hexdigest()
                for page in pages}
            with pymupdf.open() as output:
                for page in pages:
                    output.insert_pdf(document, from_page=page - 1, to_page=page - 1)
                output.save(subset)
        subset_hash = hashlib.sha256(subset.read_bytes()).hexdigest()
        converted = self._structured(self._call("convert_document_into_docling_document",
                                                 {"source": str(subset)}))
        key = converted.get("document_key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-fA-F0-9-]{16,128}", key):
            raise DoclingMcpError("DOCLING_DOCUMENT_KEY_INVALID")
        saved = self._structured(self._call("save_docling_document", {"document_key": key}))
        json_file = saved.get("json_file")
        md_file = saved.get("md_file")
        checked: dict[str, str] = {}
        for label, value in (("json_file", json_file), ("md_file", md_file)):
            if not isinstance(value, str):
                raise DoclingMcpError(f"DOCLING_{label.upper()}_INVALID")
            path = Path(value).resolve()
            if self.output_dir not in path.parents:
                raise DoclingMcpError(f"DOCLING_{label.upper()}_OUTSIDE_OUTPUT")
            suffix = ".json" if label == "json_file" else ".md"
            if path.name != key + suffix:
                raise DoclingMcpError("DOCLING_OUTPUT_KEY_MISMATCH")
            if not path.is_file():
                raise DoclingMcpError(f"DOCLING_{label.upper()}_MISSING")
            checked[label] = str(path)
        json_bytes = Path(checked["json_file"]).read_bytes()
        try:
            doc = json.loads(json_bytes)
        except ValueError as exc:
            raise DoclingMcpError("DOCLING_DOCUMENT_JSON_INVALID") from exc
        _validate_docling_document(doc, subset, len(pages))
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != digest:
            raise DoclingMcpError("DOCLING_SOURCE_CHANGED_DURING_CONVERSION")
        (artifact / "document.json").write_bytes(json_bytes)
        md_bytes = Path(checked["md_file"]).read_bytes()
        (artifact / "document.md").write_bytes(md_bytes)
        manifest = {"version": "docling-native-v1", "document_key": key,
                    "source_sha256": digest, "subset_sha256": subset_hash, "page_numbers": pages,
                    "page_mapping": {str(index): page for index, page in enumerate(pages, 1)},
                    "original_page_hashes": page_hashes, "extractor_version": "docling-mcp-3.3.0-native-v1",
                    "json_file": str(artifact / "document.json"),
                    "md_file": str(artifact / "document.md"),
                    "json_sha256": hashlib.sha256(json_bytes).hexdigest(),
                    "markdown_sha256": hashlib.sha256(md_bytes).hexdigest(),
                    "conversion_mode": "local", "do_ocr": False,
                    "server_url": self.url, "scope_hash": scope_hash}
        manifest["native_identity"] = identity
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # In-memory documents are no longer needed after preserving full JSON.
        try:
            self._call("remove_document_from_local_cache", {"document_key": key})
        except DoclingMcpError:
            pass
        return {**manifest, "manifest_file": str(manifest_path), "cached": False,
                "chunks": _structured_chunks(doc, manifest)}


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_docling_document(document: dict, subset: Path, page_count: int) -> None:
    if not isinstance(document, dict) or document.get("schema_name") != "DoclingDocument":
        raise DoclingMcpError("DOCLING_DOCUMENT_SCHEMA_INVALID")
    origin = document.get("origin") or {}
    if origin.get("filename") != subset.name or origin.get("mimetype") != "application/pdf":
        raise DoclingMcpError("DOCLING_DOCUMENT_ORIGIN_MISMATCH")
    if set((document.get("pages") or {}).keys()) != {str(i) for i in range(1, page_count + 1)}:
        raise DoclingMcpError("DOCLING_DOCUMENT_PAGE_MAP_INVALID")
    # Official local converter adds exactly this source marker. It binds the
    # saved document to the request path as well as to its native file origin.
    marker = f"source: {subset}"
    if not any(item.get("text") == marker and item.get("content_layer") == "furniture"
               for item in document.get("texts", []) if isinstance(item, dict)):
        raise DoclingMcpError("DOCLING_DOCUMENT_SOURCE_MARKER_MISMATCH")


def _structured_chunks(document: dict, manifest: dict) -> list[dict]:
    """Traverse Docling refs in reading order; retain raw node/provenance refs."""
    nodes = {item.get("self_ref"): item for key in ("texts", "tables", "groups", "pictures")
             for item in document.get(key, []) if isinstance(item, dict)}
    headings: list[tuple[int, str]] = []
    result: list[dict] = []
    seen: set[str] = set()

    def visit(ref: str) -> None:
        if ref in seen or ref not in nodes:
            return
        seen.add(ref)
        node = nodes[ref]
        if node.get("content_layer") == "furniture":
            return
        label = node.get("label", "")
        text = node.get("text", "")
        if label in {"section_header", "title"} and isinstance(text, str):
            level = int(node.get("level", 1))
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, text))
        if label == "table":
            cells = (node.get("data") or {}).get("table_cells", [])
            text = "\n".join(str(cell.get("text", "")) for cell in cells if isinstance(cell, dict))
        provenance = node.get("prov", [])
        if isinstance(text, str) and text.strip() and provenance:
            original_pages = [manifest["page_mapping"].get(str(p.get("page_no")))
                              for p in provenance if isinstance(p, dict)]
            original_pages = [page for page in original_pages if page is not None]
            if original_pages:
                raw = str(node.get("orig", text))
                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                page_no = original_pages[0]
                mapped_provenance = []
                for prov in provenance:
                    if not isinstance(prov, dict):
                        continue
                    local_page = prov.get("page_no")
                    original_page = manifest["page_mapping"].get(str(local_page))
                    mapped_provenance.append({**prov, "page_no": original_page,
                                               "docling_page_no": local_page})
                page_rect = None
                first_prov = provenance[0] if isinstance(provenance[0], dict) else {}
                box = first_prov.get("bbox", {})
                if isinstance(box, dict) and all(key in box for key in ("l", "t", "r", "b")):
                    height = ((document.get("pages") or {}).get(str(first_prov.get("page_no")), {})
                              .get("size", {}).get("height"))
                    if box.get("coord_origin") == "BOTTOMLEFT" and isinstance(height, (int, float)):
                        page_rect = [box["l"], height - box["t"], box["r"], height - box["b"]]
                    elif box.get("coord_origin") == "TOPLEFT":
                        page_rect = [box["l"], box["t"], box["r"], box["b"]]
                result.append({"page_no": page_no, "page_numbers": original_pages,
                    "page_sha256": manifest["original_page_hashes"][str(page_no)],
                    "text": text, "text_sha256": text_hash, "raw_text_sha256": raw_hash,
                    "section": " / ".join(value for _, value in headings)[:256] or label,
                    "heading_path": [value for _, value in headings], "parent_anchor": node.get("parent"),
                    "docling_ref": ref, "source_sha256": manifest["source_sha256"],
                    "bbox": {"page": page_rect, "page_no": page_no, "coordinate_origin": "TOPLEFT",
                             "provenance": mapped_provenance,
                             "docling_provenance": provenance, "original_page_numbers": original_pages,
                             "heading_path": [value for _, value in headings],
                             "parent_anchor": node.get("parent"), "docling_ref": ref,
                             "docling_json_sha256": manifest["json_sha256"],
                             "docling_json_file": manifest["json_file"], "raw_text_sha256": raw_hash},
                    "extractor_version": manifest["extractor_version"]})
        for child in node.get("children", []):
            if isinstance(child, dict):
                visit(child.get("$ref", ""))

    for child in (document.get("body") or {}).get("children", []):
        if isinstance(child, dict):
            visit(child.get("$ref", ""))
    return result
