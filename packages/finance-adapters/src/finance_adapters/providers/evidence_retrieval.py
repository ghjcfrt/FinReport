"""Auditable evidence retrieval for multi-round financial-report analysis.

The retrieval boundary deliberately keeps strict facts and searchable prose
separate.  Qdrant is an optional primary vector index; SQLite remains the
authoritative source and deterministic fallback.  Every public operation
records an audit event containing request/result hashes, backend status and
selected source identifiers, while never trusting arbitrary remote payloads.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

import httpx
import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.models import (
    AnalysisRun,
    AuditEvent,
    DocumentTextChunk,
    ExtractionJob,
    FilingVersion,
    FinancialFact,
    OcrCall,
)
from finance_adapters.security.file_gate import scanned_object_path

RETRIEVAL_VERSION = "evidence-retrieval-v1"
DEFAULT_CHUNK_CHARACTERS = 2400
MAX_QUERY_CHARACTERS = 2000
MAX_RESULT_LIMIT = 100
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z][a-zA-Z0-9_.%-]*|\d+(?:\.\d+)?")


class QdrantBackendError(RuntimeError):
    """A retriable or configuration error from the optional Qdrant service."""


@dataclass(frozen=True)
class RetrievedChunk:
    id: int
    filing_version_id: int
    page_no: int
    page_sha256: str
    section: str
    text: str
    text_sha256: str
    source_sha256: str
    extraction_method: str
    extractor_version: str
    bounding_box: dict | list | None
    related_fact_ids: tuple[int, ...]
    score: float
    backend: str
    review_status: str

    def as_dict(self) -> dict[str, Any]:
        checked = re.findall(r"[☑✓✔√\uf052\uf0fe][\s|]*(不适用|适用|否|是)", self.text)
        question = bool(checked or "是否" in self.text)
        return {
            "id": self.id,
            "filing_version_id": self.filing_version_id,
            "page_no": self.page_no,
            "section": self.section,
            "text": self.text,
            "text_sha256": self.text_sha256,
            "source_sha256": self.source_sha256,
            "extraction_method": self.extraction_method,
            "extractor_version": self.extractor_version,
            "bounding_box": self.bounding_box,
            "related_fact_ids": list(self.related_fact_ids),
            "score": round(float(self.score), 8),
            "backend": self.backend,
            "source_id": f"text-chunk:{self.id}",
            "line_ids": [f"text-chunk:{self.id}"],
            "context_line_ids": [f"text-chunk:{self.id}"],
            "page_sha256": self.page_sha256,
            "bbox": self.bounding_box,
            "review_status": self.review_status,
            "question_completeness": "incomplete_question" if question else "not_a_checkbox",
            "selected_answer": checked[0] if len(checked) == 1 else None,
            "selection_status": "selected" if len(checked) == 1 else "ambiguous"
            if checked else "not_a_checkbox",
        }


@dataclass(frozen=True)
class _VectorHit:
    point_id: int
    score: float
    payload: dict[str, Any]


class _QdrantMcpClient:
    """MCP streamable-HTTP client for the official Qdrant MCP tools.

    The server owns the semantic embedding model.  Tool discovery avoids
    assuming whether collection_name/query_filter are exposed by its runtime
    configuration.  Qdrant results carry metadata, but are always re-read from
    the local authoritative SQLite rows before being returned to the agent.
    """

    def __init__(self, *, url: str, token: str, collection: str, timeout_seconds: float):
        # FastMCP 2.x mounts the streamable endpoint with a trailing slash.
        # Normalizing locally avoids a 307 response being mistaken for JSON.
        self.url = url.rstrip("/") + "/"
        self.token = token
        self.collection = collection
        self.timeout_seconds = timeout_seconds
        self._session_id: str | None = None
        self._request_id = 0
        self._tools: dict[str, dict] | None = None

    def _rpc(self, method: str, params: dict | None = None,
             *, notification: bool = False) -> dict:
        self._request_id += 1
        request: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            request["params"] = params
        if not notification:
            request["id"] = self._request_id
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", "MCP-Protocol-Version": "2025-03-26"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            response = httpx.post(self.url, json=request, headers=headers,
                                  timeout=self.timeout_seconds)
        except httpx.HTTPError as exc:
            raise QdrantBackendError(f"QDRANT_MCP_REQUEST_FAILED:{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise QdrantBackendError(f"QDRANT_MCP_HTTP_{response.status_code}")
        if response.headers.get("mcp-session-id"):
            self._session_id = response.headers["mcp-session-id"]
        if notification or response.status_code == 202:
            return {}
        try:
            if "text/event-stream" in response.headers.get("content-type", ""):
                messages = []
                for event in re.split(r"\r?\n\r?\n", response.text):
                    data = "\n".join(line[5:].lstrip() for line in event.splitlines()
                                     if line.startswith("data:"))
                    if data:
                        decoded = json.loads(data)
                        if isinstance(decoded, dict) and decoded.get("id") == request.get("id"):
                            messages.append(decoded)
                decoded = messages[-1] if messages else None
            else:
                decoded = response.json()
        except ValueError as exc:
            raise QdrantBackendError("QDRANT_MCP_INVALID_JSON") from exc
        if not isinstance(decoded, dict) or "error" in decoded:
            raise QdrantBackendError("QDRANT_MCP_PROTOCOL_ERROR")
        result = decoded.get("result", {})
        if not isinstance(result, dict):
            raise QdrantBackendError("QDRANT_MCP_RESULT_INVALID")
        return result

    def _initialize(self) -> None:
        if self._tools is not None:
            return
        self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "FinReport", "version": RETRIEVAL_VERSION},
        })
        self._rpc("notifications/initialized", notification=True)
        result = self._rpc("tools/list", {})
        self._tools = {item["name"]: item for item in result.get("tools", [])
                       if isinstance(item, dict) and isinstance(item.get("name"), str)}

    def _call_tool(self, name: str, arguments: dict) -> dict:
        self._initialize()
        assert self._tools is not None
        if name not in self._tools:
            raise QdrantBackendError(f"QDRANT_MCP_TOOL_NOT_FOUND:{name}")
        schema = self._tools[name].get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        accepted = {key: value for key, value in arguments.items() if key in properties}
        result = self._rpc("tools/call", {"name": name, "arguments": accepted})
        if result.get("isError"):
            raise QdrantBackendError(f"QDRANT_MCP_TOOL_FAILED:{name}")
        return result

    def store(self, information: str, metadata: dict) -> None:
        self._initialize()
        assert self._tools is not None
        store_tool = "finreport-store" if "finreport-store" in self._tools else "qdrant-store"
        self._call_tool(store_tool, {
            "information": information, "metadata": metadata, "collection_name": self.collection,
        })

    def index_identity(self) -> dict:
        self._initialize()
        assert self._tools is not None
        if "finreport-index-status" not in self._tools:
            return {"generation": "external-unverified", "collection": self.collection}
        result = self._call_tool("finreport-index-status", {})
        identity = result.get("structuredContent")
        if not isinstance(identity, dict):
            for item in result.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    try:
                        identity = json.loads(item.get("text", ""))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(identity, dict):
                        break
        if not isinstance(identity, dict) or not identity.get("generation"):
            raise QdrantBackendError("QDRANT_INDEX_IDENTITY_INVALID")
        if identity.get("collection") != self.collection:
            raise QdrantBackendError("QDRANT_COLLECTION_MISMATCH")
        return identity

    def search(self, query: str, *, filing_id: int, limit: int) -> list[_VectorHit]:
        self._initialize()
        assert self._tools is not None
        find_tool = self._tools.get("qdrant-find", {})
        if "query_filter" not in find_tool.get("inputSchema", {}).get("properties", {}):
            raise QdrantBackendError("QDRANT_MCP_FILTER_REQUIRED")
        result = self._call_tool("qdrant-find", {
            "query": query,
            "collection_name": self.collection,
            "query_filter": {"must": [
                {"key": "metadata.filing_version_id", "match": {"value": filing_id}}
            ]},
        })
        hits = []
        seen_chunk_ids: set[int] = set()
        for block in result.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text", "")
            # FastMCP 2.7 serializes a list[str] return value as JSON in one
            # text block.  Decode that envelope before parsing entry metadata;
            # other releases may already return one text block per entry.
            try:
                decoded_text = json.loads(text)
            except (TypeError, ValueError):
                decoded_text = None
            entries = (decoded_text if isinstance(decoded_text, list) else [text])
            # This is the official server's format_entry wire shape.  Treat
            # malformed/foreign entries as unusable rather than evidence.
            for entry in entries:
                if not isinstance(entry, str):
                    continue
                for match in re.finditer(r"<entry>.*?<metadata>(.*?)</metadata></entry>",
                                         entry, re.DOTALL):
                    try:
                        metadata = json.loads(match.group(1))
                        chunk_id = int(metadata["chunk_id"])
                    except (ValueError, TypeError, KeyError):
                        continue
                    if metadata.get("filing_version_id") != filing_id or chunk_id in seen_chunk_ids:
                        continue
                    seen_chunk_ids.add(chunk_id)
                    hits.append(_VectorHit(point_id=chunk_id, score=1.0 / (len(hits) + 1),
                                           payload=metadata))
        return hits[:limit]


def _tokens(value: str) -> list[str]:
    """Use a stable, dependency-free tokenizer that works for Chinese text."""

    return _TOKEN_RE.findall(value.casefold())


def _hashed_embedding(value: str, dimensions: int) -> list[float]:
    """Build a reproducible lexical vector when no embedding service is configured.

    This is intentionally not presented as a semantic embedding.  It allows a
    Qdrant deployment to use cosine retrieval without introducing a second
    provider whose model/version would otherwise need to be audited.  The
    SQLite scorer is still applied to the returned candidates.
    """

    if dimensions <= 0:
        raise ValueError("QDRANT_VECTOR_SIZE_INVALID")
    vector = [0.0] * dimensions
    for token in _tokens(value):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
        # Add a second bucket to reduce collisions for a short fixed vector.
        index2 = int.from_bytes(digest[5:9], "big") % dimensions
        vector[index2] += sign * 0.5
    norm = math.sqrt(sum(item * item for item in vector))
    return [item / norm for item in vector] if norm else vector


def _lexical_score(query: str, text: str) -> float:
    query_tokens = _tokens(query)
    text_tokens = _tokens(text)
    if not query_tokens or not text_tokens:
        return 0.0
    counts: dict[str, int] = {}
    for token in text_tokens:
        counts[token] = counts.get(token, 0) + 1
    matched = sum(1 for token in query_tokens if token in counts)
    if matched == 0:
        # Exact substring matching gives useful behavior for multi-character
        # financial terms that were split into individual Han characters.
        return 1.0 if query.casefold() in text.casefold() else 0.0
    unique_match = len({token for token in query_tokens if token in counts})
    density = sum(min(counts[token], 3) for token in set(query_tokens) if token in counts)
    phrase_bonus = 0.25 if query.strip().casefold() in text.casefold() else 0.0
    return min(1.0, 0.45 * matched / len(query_tokens)
               + 0.35 * unique_match / len(set(query_tokens))
               + 0.10 * min(1.0, density / max(1, len(query_tokens)))
               + phrase_bonus)


def _bounded_limit(limit: int) -> int:
    if limit <= 0:
        raise ValueError("RETRIEVAL_LIMIT_INVALID")
    return min(limit, MAX_RESULT_LIMIT)


def _chunk_source_filter(filing_id: int, source_sha256: str | None = None):
    conditions = [DocumentTextChunk.filing_version_id == filing_id]
    if source_sha256:
        conditions.append(DocumentTextChunk.source_sha256 == source_sha256)
    return conditions


class _QdrantHttpClient:
    """Small REST client using only the already-pinned httpx dependency."""

    def __init__(self, *, url: str, api_key: str, collection: str,
                 timeout_seconds: float, vector_size: int):
        if not url:
            raise QdrantBackendError("QDRANT_URL_NOT_CONFIGURED")
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.collection = collection
        self.timeout_seconds = timeout_seconds
        self.vector_size = vector_size

    @property
    def _collection_path(self) -> str:
        return f"/collections/{quote(self.collection, safe='')}"

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        try:
            response = httpx.request(
                method,
                f"{self.url}{path}",
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise QdrantBackendError(f"QDRANT_REQUEST_FAILED:{type(exc).__name__}") from exc
        if response.status_code >= 400:
            detail = response.text[:240].replace("\n", " ")
            raise QdrantBackendError(f"QDRANT_HTTP_{response.status_code}:{detail}")
        try:
            decoded = response.json()
        except ValueError as exc:
            raise QdrantBackendError("QDRANT_INVALID_JSON") from exc
        if not isinstance(decoded, dict):
            raise QdrantBackendError("QDRANT_INVALID_RESPONSE")
        return decoded

    def ensure_collection(self) -> None:
        try:
            self._request("GET", self._collection_path)
            return
        except QdrantBackendError as exc:
            if "QDRANT_HTTP_404" not in str(exc):
                raise
        self._request(
            "PUT",
            self._collection_path,
            {"vectors": {"size": self.vector_size, "distance": "Cosine"}},
        )

    def upsert(self, points: Sequence[dict[str, Any]]) -> None:
        self.ensure_collection()
        self._request(
            "PUT",
            f"{self._collection_path}/points?wait=true",
            {"points": list(points)},
        )

    def search(self, vector: list[float], *, filing_id: int, limit: int) -> list[_VectorHit]:
        self.ensure_collection()
        body = {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
            "filter": {"must": [{"key": "filing_version_id", "match": {"value": filing_id}}]},
        }
        try:
            response = self._request("POST", f"{self._collection_path}/points/search", body)
        except QdrantBackendError as exc:
            # Qdrant 1.10+ exposes /points/query as the preferred endpoint,
            # while older deployments expose /points/search.  Supporting both
            # keeps a self-hosted and managed deployment interchangeable.
            if "QDRANT_HTTP_404" not in str(exc):
                raise
            response = self._request(
                "POST",
                f"{self._collection_path}/points/query",
                {"query": vector, "limit": limit, "with_payload": True,
                 "filter": body["filter"]},
            )
        rows = response.get("result", [])
        if isinstance(rows, dict):
            rows = rows.get("points", [])
        if not isinstance(rows, list):
            raise QdrantBackendError("QDRANT_SEARCH_RESULT_INVALID")
        hits = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            try:
                point_id = int(item["id"])
                score = float(item.get("score", 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            payload = item.get("payload")
            hits.append(_VectorHit(point_id=point_id, score=score,
                                   payload=payload if isinstance(payload, dict) else {}))
        return hits


class EvidenceRetriever:
    """Read-only evidence operations plus explicit chunk indexing."""

    def __init__(self, configured_settings=settings):
        self.settings = configured_settings

    def _qdrant(self) -> _QdrantHttpClient | None:
        if not getattr(self.settings, "qdrant_enabled", False):
            return None
        url = getattr(self.settings, "qdrant_url", "")
        if not url:
            return None
        return _QdrantHttpClient(
            url=url,
            api_key=getattr(self.settings, "qdrant_api_key", ""),
            collection=getattr(self.settings, "qdrant_collection", "finreport_document_chunks"),
            timeout_seconds=float(getattr(self.settings, "qdrant_timeout_seconds", 5.0)),
            vector_size=int(getattr(self.settings, "qdrant_vector_size", 256)),
        )

    def _qdrant_mcp(self) -> _QdrantMcpClient | None:
        if not getattr(self.settings, "qdrant_enabled", False):
            return None
        url = getattr(self.settings, "qdrant_mcp_url", "")
        if not url:
            return None
        return _QdrantMcpClient(
            url=url,
            token=getattr(self.settings, "qdrant_mcp_token", ""),
            collection=getattr(self.settings, "qdrant_collection", "finreport_document_chunks"),
            timeout_seconds=float(getattr(self.settings, "qdrant_timeout_seconds", 5.0)),
        )

    @staticmethod
    def _audit(session: Session, filing_id: int, operation: str, request: dict,
               result: dict, *, status: str) -> None:
        record_audit_event(
            session,
            filing_id,
            category="retrieval",
            action="evidence_retrieval",
            status=status,
            source_ref=f"filing:{filing_id}",
            tool_name="evidence_retriever",
            tool_version=RETRIEVAL_VERSION,
            input_hash=stable_hash({"operation": operation, **request}),
            output_hash=stable_hash(result),
            detail={
                "operation": operation,
                "request": request,
                "query_sha256": stable_hash(request.get("query", "")),
                "backend": result.get("backend"),
                "hit_ids": result.get("hit_ids", []),
                "result_count": result.get("count", 0),
                "degraded": result.get("degraded", False),
                "result_snapshot": result,
            },
        )

    def document_coverage(self, session: Session, filing_id: int) -> dict[str, Any]:
        filing = session.get(FilingVersion, filing_id)
        if filing is None:
            raise ValueError("FILING_NOT_FOUND")
        chunks = list(session.scalars(select(DocumentTextChunk).where(
            *_chunk_source_filter(filing_id, filing.file_sha256)
        ).order_by(DocumentTextChunk.page_no, DocumentTextChunk.id)))
        pages = sorted({chunk.page_no for chunk in chunks})
        methods = sorted({chunk.extraction_method for chunk in chunks})
        latest_materialization = session.scalar(select(AuditEvent).where(
            AuditEvent.filing_version_id == filing_id,
            AuditEvent.action == "document_chunks_materialized",
        ).order_by(AuditEvent.id.desc()).limit(1))
        materialization = latest_materialization.detail if latest_materialization is not None else {}
        total_pages = materialization.get("page_count") if (
            materialization.get("source_sha256") == filing.file_sha256
        ) else None
        result = {
            "filing_version_id": filing_id,
            "source_sha256": filing.file_sha256,
            "status": "available" if chunks else "empty",
            "chunk_count": len(chunks),
            "page_count": len(pages),
            "total_pages": total_pages,
            "pages_without_text": max(0, total_pages - len(pages))
            if isinstance(total_pages, int) else None,
            "page_numbers": pages,
            "extraction_methods": methods,
            "extractor_versions": sorted({chunk.extractor_version for chunk in chunks}),
            "source_hashes": sorted({chunk.source_sha256 for chunk in chunks}),
            "review_status_counts": {
                status: sum(1 for chunk in chunks if _current_review_status(chunk) == status)
                for status in ("unreviewed", "confirmed", "rejected")
            },
            "text_review_complete": bool(chunks) and all(
                _current_review_status(chunk) == "confirmed" for chunk in chunks
            ),
            "invalid_chunk_count": sum(1 for chunk in chunks if not _chunk_integrity(chunk)),
            "backend": "sqlite",
            "degraded": False,
        }
        self._audit(session, filing_id, "document_coverage", {},
                    {"backend": result["backend"], "count": result["chunk_count"],
                     "hit_ids": [chunk.id for chunk in chunks]}, status="completed")
        return result

    def read_chunks(
        self,
        session: Session,
        filing_id: int,
        *,
        chunk_ids: Iterable[int] | None = None,
        page_numbers: Iterable[int] | None = None,
        section: str | None = None,
        limit: int = MAX_RESULT_LIMIT,
    ) -> list[dict[str, Any]]:
        filing = session.get(FilingVersion, filing_id)
        if filing is None:
            raise ValueError("FILING_NOT_FOUND")
        limit = _bounded_limit(limit)
        conditions = _chunk_source_filter(filing_id, filing.file_sha256)
        selected_ids = [int(value) for value in chunk_ids] if chunk_ids is not None else []
        selected_pages = [int(value) for value in page_numbers] if page_numbers is not None else []
        if selected_ids:
            conditions.append(DocumentTextChunk.id.in_(selected_ids))
        if selected_pages:
            conditions.append(DocumentTextChunk.page_no.in_(selected_pages))
        if section:
            conditions.append(DocumentTextChunk.section == section[:256])
        chunks = list(session.scalars(select(DocumentTextChunk).where(*conditions)
                                     .order_by(DocumentTextChunk.page_no, DocumentTextChunk.id)
                                     .limit(limit)))
        chunks = [chunk for chunk in chunks if _chunk_integrity(chunk)
                  and _current_review_status(chunk) != "rejected"]
        rows = [self._chunk_result(chunk, score=1.0, backend="sqlite").as_dict()
                for chunk in chunks]
        result = {"backend": "sqlite", "count": len(rows), "hit_ids": [item["id"] for item in rows]}
        self._audit(session, filing_id, "read_chunks", {
            "chunk_ids": selected_ids, "page_numbers": selected_pages, "section": section,
            "limit": limit,
        }, result, status="completed")
        return rows

    def list_review_chunks(
        self,
        session: Session,
        filing_id: int,
        *,
        page_numbers: Iterable[int] | None = None,
        review_status: str | None = None,
        include_rejected: bool = True,
        limit: int = MAX_RESULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """List source text for human review, including reversible rejections.

        This operation is deliberately separate from ``read_chunks`` and is
        not registered among the analysis Agent tools.  Reviewers may inspect
        and reverse a rejection while model retrieval continues to exclude it.
        """

        filing = session.get(FilingVersion, filing_id)
        if filing is None:
            raise ValueError("FILING_NOT_FOUND")
        if review_status not in {None, "unreviewed", "confirmed", "rejected"}:
            raise ValueError("TEXT_REVIEW_STATUS_INVALID")
        limit = _bounded_limit(limit)
        selected_pages = sorted({int(value) for value in page_numbers or []})
        conditions = _chunk_source_filter(filing_id, filing.file_sha256)
        if selected_pages:
            conditions.append(DocumentTextChunk.page_no.in_(selected_pages))
        rows = []
        for chunk in session.scalars(select(DocumentTextChunk).where(*conditions)
                                      .order_by(DocumentTextChunk.page_no, DocumentTextChunk.id)):
            status = _current_review_status(chunk)
            if not _chunk_integrity(chunk) or (status == "rejected" and not include_rejected):
                continue
            if review_status is not None and status != review_status:
                continue
            rows.append(self._chunk_result(chunk, score=1.0, backend="sqlite_review").as_dict())
            if len(rows) >= limit:
                break
        self._audit(session, filing_id, "list_review_chunks", {
            "page_numbers": selected_pages, "review_status": review_status,
            "include_rejected": include_rejected, "limit": limit,
        }, {"backend": "sqlite_review", "count": len(rows),
            "hit_ids": [item["id"] for item in rows], "chunks": rows}, status="completed")
        return rows

    def search_text(self, session: Session, filing_id: int, query: str,
                    *, limit: int = 8, page_numbers: Iterable[int] | None = None,
                    section: str | None = None) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("RETRIEVAL_QUERY_REQUIRED")
        if len(query) > MAX_QUERY_CHARACTERS:
            raise ValueError("RETRIEVAL_QUERY_TOO_LONG")
        filing = session.get(FilingVersion, filing_id)
        if filing is None:
            raise ValueError("FILING_NOT_FOUND")
        limit = _bounded_limit(limit)
        page_filter = {int(value) for value in page_numbers} if page_numbers is not None else set()
        conditions = _chunk_source_filter(filing_id, filing.file_sha256)
        if page_filter:
            conditions.append(DocumentTextChunk.page_no.in_(page_filter))
        if section:
            conditions.append(DocumentTextChunk.section == section[:256])
        chunks = [chunk for chunk in session.scalars(select(DocumentTextChunk).where(*conditions)
                  .order_by(DocumentTextChunk.page_no, DocumentTextChunk.id))
                  if _chunk_integrity(chunk) and _current_review_status(chunk) != "rejected"]
        if not chunks:
            result = {"backend": "sqlite", "degraded": False, "count": 0,
                      "hit_ids": [], "query": query, "chunks": [],
                      "coverage_status": "empty"}
            self._audit(session, filing_id, "search_text", {
                "query": query, "limit": limit, "page_numbers": sorted(page_filter), "section": section,
            }, result, status="completed")
            return result

        lexical = {chunk.id: _lexical_score(query, chunk.text) for chunk in chunks}
        backend = "sqlite"
        degraded = False
        vector_hits: list[_VectorHit] = []
        mcp = self._qdrant_mcp()
        qdrant = self._qdrant() if mcp is None else None
        if mcp is None and qdrant is None and getattr(self.settings, "qdrant_enabled", False):
            degraded = True
        if mcp is not None or qdrant is not None:
            try:
                if mcp is not None:
                    identity_probe = getattr(mcp, "index_identity", None)
                    if callable(identity_probe):
                        identity_probe()
                    vector_hits = mcp.search(query, filing_id=filing_id, limit=max(limit * 3, limit))
                    backend = "qdrant_mcp_hybrid"
                else:
                    assert qdrant is not None
                    vector_hits = qdrant.search(
                        _hashed_embedding(query, qdrant.vector_size),
                        filing_id=filing_id,
                        limit=max(limit * 3, limit),
                    )
                    backend = "qdrant_rest_lexical"
            except (QdrantBackendError, ValueError):
                degraded = True

        local_chunks = {chunk.id: chunk for chunk in chunks}
        vector_scores = {
            hit.point_id: max(0.0, min(1.0, hit.score)) for hit in vector_hits
            if hit.point_id in local_chunks
            and hit.payload.get("text_sha256") == local_chunks[hit.point_id].text_sha256
            and hit.payload.get("source_sha256") == local_chunks[hit.point_id].source_sha256
        }
        if backend in {"qdrant_mcp_hybrid", "qdrant_rest_lexical"} and not vector_scores:
            backend, degraded = "sqlite", True
        candidates = []
        for chunk in chunks:
            lexical_score = lexical[chunk.id]
            vector_score = vector_scores.get(chunk.id, 0.0)
            if backend in {"qdrant_mcp_hybrid", "qdrant_rest_lexical"}:
                score = 0.65 * vector_score + 0.35 * lexical_score
            else:
                score = lexical_score
            if score > 0:
                candidates.append((score, lexical_score, chunk))
        candidates.sort(key=lambda item: (-item[0], item[2].page_no, item[2].id))
        rows = [self._chunk_result(chunk, score=score, backend=backend).as_dict()
                for score, _, chunk in candidates[:limit]]
        result = {
            "backend": backend,
            "degraded": degraded,
            "count": len(rows),
            "hit_ids": [item["id"] for item in rows],
            "query": query,
            "coverage_status": "available" if chunks else "empty",
            "vector_hit_count": len(vector_scores),
            "chunks": rows,
        }
        self._audit(session, filing_id, "search_text", {
            "query": query, "limit": limit, "page_numbers": sorted(page_filter), "section": section,
        }, result, status="degraded" if degraded else "completed")
        return result

    def list_facts(self, session: Session, filing_id: int, *,
                   concept_codes: Iterable[str] | None = None,
                   verified_only: bool = False, limit: int = MAX_RESULT_LIMIT) -> list[dict[str, Any]]:
        if session.get(FilingVersion, filing_id) is None:
            raise ValueError("FILING_NOT_FOUND")
        limit = _bounded_limit(limit)
        conditions = [FinancialFact.filing_version_id == filing_id,
                      FinancialFact.review_status != "superseded",
                      FinancialFact.quality_status != "superseded"]
        if verified_only:
            conditions.extend([
                FinancialFact.quality_status == "facts_verified",
                FinancialFact.review_status == "verified",
            ])
        codes = [str(code)[:96] for code in concept_codes or []]
        if codes:
            conditions.append(FinancialFact.concept_code.in_(codes))
        facts = list(session.scalars(select(FinancialFact).where(*conditions)
                                     .order_by(FinancialFact.id).limit(limit)))
        rows = [self._fact_result(fact) for fact in facts]
        result = {"backend": "sqlite", "count": len(rows), "hit_ids": [item["id"] for item in rows]}
        self._audit(session, filing_id, "list_facts", {
            "concept_codes": codes, "verified_only": verified_only, "limit": limit,
        }, result, status="completed")
        return rows

    def index_chunks(self, session: Session, filing_id: int, *,
                     chunk_ids: Iterable[int] | None = None) -> dict[str, Any]:
        """Upsert current-source chunks and return explicit degradation status."""

        filing = session.get(FilingVersion, filing_id)
        if filing is None:
            raise ValueError("FILING_NOT_FOUND")
        conditions = _chunk_source_filter(filing_id, filing.file_sha256)
        selected_ids = [int(value) for value in chunk_ids] if chunk_ids is not None else []
        if selected_ids:
            conditions.append(DocumentTextChunk.id.in_(selected_ids))
        chunks = list(session.scalars(select(DocumentTextChunk).where(*conditions)
                                     .order_by(DocumentTextChunk.id)))
        mcp = self._qdrant_mcp()
        qdrant = self._qdrant() if mcp is None else None
        if qdrant is None and mcp is None:
            misconfigured = bool(getattr(self.settings, "qdrant_enabled", False))
            result = {"backend": "sqlite", "status": "degraded" if misconfigured else "disabled",
                      "count": len(chunks), "hit_ids": [chunk.id for chunk in chunks],
                      "degraded": misconfigured}
            if misconfigured:
                result["error_code"] = "QDRANT_URL_NOT_CONFIGURED"
            self._audit(session, filing_id, "index_chunks", {"chunk_ids": selected_ids}, result,
                        status="completed")
            return result
        try:
            if mcp is not None:
                identity_probe = getattr(mcp, "index_identity", None)
                identity = identity_probe() if callable(identity_probe) else {
                    "generation": "external-unverified", "collection": mcp.collection,
                }
                indexed = 0
                for chunk in chunks:
                    index_input_hash = stable_hash({
                        "chunk_id": chunk.id, "text_sha256": chunk.text_sha256,
                        "source_sha256": chunk.source_sha256,
                        "mcp_url": mcp.url, "collection": mcp.collection,
                        "version": RETRIEVAL_VERSION,
                        "index_identity": identity,
                    })
                    already = session.scalar(select(AuditEvent.id).where(
                        AuditEvent.filing_version_id == filing_id,
                        AuditEvent.action == "qdrant_chunk_indexed",
                        AuditEvent.input_hash == index_input_hash,
                    ).limit(1))
                    if already:
                        continue
                    metadata = {
                        "filing_version_id": filing_id, "chunk_id": chunk.id,
                        "chunk_key": chunk.chunk_key, "page_no": chunk.page_no,
                        "section": chunk.section, "text_sha256": chunk.text_sha256,
                        "source_sha256": chunk.source_sha256,
                        "extraction_method": chunk.extraction_method,
                        "extractor_version": chunk.extractor_version,
                    }
                    mcp.store(chunk.text, metadata)
                    record_audit_event(
                        session, filing_id, category="retrieval", action="qdrant_chunk_indexed",
                        status="completed", source_ref=f"text-chunk:{chunk.id}",
                        tool_name="qdrant-store", tool_version=RETRIEVAL_VERSION,
                        input_hash=index_input_hash, output_hash=stable_hash(metadata),
                        detail={"chunk_id": chunk.id, "text_sha256": chunk.text_sha256,
                                "collection": mcp.collection},
                    )
                    indexed += 1
                result = {"backend": "qdrant_mcp", "status": "indexed", "count": len(chunks),
                          "newly_indexed_count": indexed,
                          "hit_ids": [chunk.id for chunk in chunks], "degraded": False}
                result["index_identity"] = identity
                self._audit(session, filing_id, "index_chunks", {"chunk_ids": selected_ids}, result,
                            status="completed")
                return result
            assert qdrant is not None
            points = [{
                "id": chunk.id,
                "vector": _hashed_embedding(chunk.text, qdrant.vector_size),
                "payload": {
                    "filing_version_id": filing_id,
                    "chunk_id": chunk.id,
                    "chunk_key": chunk.chunk_key,
                    "page_no": chunk.page_no,
                    "section": chunk.section,
                    "text_sha256": chunk.text_sha256,
                    "source_sha256": chunk.source_sha256,
                    "extraction_method": chunk.extraction_method,
                    "extractor_version": chunk.extractor_version,
                },
            } for chunk in chunks]
            if points:
                qdrant.upsert(points)
            result = {"backend": "qdrant", "status": "indexed", "count": len(chunks),
                      "hit_ids": [chunk.id for chunk in chunks], "degraded": False}
            self._audit(session, filing_id, "index_chunks", {"chunk_ids": selected_ids}, result,
                        status="completed")
            return result
        except (QdrantBackendError, ValueError) as exc:
            result = {"backend": "sqlite", "status": "degraded", "count": len(chunks),
                      "hit_ids": [chunk.id for chunk in chunks], "degraded": True,
                      "error_code": str(exc).split(":", 1)[0]}
            self._audit(session, filing_id, "index_chunks", {"chunk_ids": selected_ids}, result,
                        status="degraded")
            return result

    @staticmethod
    def _chunk_result(chunk: DocumentTextChunk, *, score: float, backend: str) -> RetrievedChunk:
        related = tuple(int(value) for value in (chunk.related_fact_ids or [])
                        if isinstance(value, (int, str)) and str(value).isdigit())
        return RetrievedChunk(
            id=chunk.id, filing_version_id=chunk.filing_version_id, page_no=chunk.page_no,
            page_sha256=chunk.page_sha256,
            section=chunk.section, text=chunk.text, text_sha256=chunk.text_sha256,
            source_sha256=chunk.source_sha256, extraction_method=chunk.extraction_method,
            extractor_version=chunk.extractor_version, bounding_box=chunk.bounding_box,
            related_fact_ids=related, score=score, backend=backend,
            review_status=_current_review_status(chunk),
        )

    @staticmethod
    def _fact_result(fact: FinancialFact) -> dict[str, Any]:
        return {
            "id": fact.id,
            "filing_version_id": fact.filing_version_id,
            "concept_code": fact.concept_code,
            "concept_name": fact.concept_name,
            "statement_type": fact.statement_type,
            "period_start": _iso_date(fact.period_start),
            "period_end": _iso_date(fact.period_end),
            "value": str(fact.value),
            "currency": fact.currency,
            "unit_scale": fact.unit_scale,
            "flow_or_stock": fact.flow_or_stock,
            "quality_status": fact.quality_status,
            "review_status": fact.review_status,
            "comparative_period": fact.comparative_period,
            "is_restatement": fact.is_restatement,
        }


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _chunk_integrity(chunk: DocumentTextChunk) -> bool:
    return bool(chunk.text_sha256 and _text_hash(chunk.text) == chunk.text_sha256)


def _chunk_review_digest(chunk: DocumentTextChunk) -> str:
    return stable_hash({
        "filing_version_id": chunk.filing_version_id,
        "chunk_key": chunk.chunk_key,
        "text_sha256": chunk.text_sha256,
        "source_sha256": chunk.source_sha256,
        "page_no": chunk.page_no,
        "page_sha256": chunk.page_sha256,
        "extraction_method": chunk.extraction_method,
        "extractor_version": chunk.extractor_version,
        "bounding_box": chunk.bounding_box,
    })


def _current_review_status(chunk: DocumentTextChunk) -> str:
    if (chunk.review_status in {"confirmed", "rejected"}
            and chunk.review_digest == _chunk_review_digest(chunk)):
        return chunk.review_status
    return "unreviewed"


def _split_text(text: str, max_characters: int) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    chunks: list[str] = []
    current = ""
    for line in normalized.split("\n"):
        line = line.strip()
        if not line:
            if current and not current.endswith("\n"):
                current += "\n"
            continue
        while len(line) > max_characters:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.append(line[:max_characters])
            line = line[max_characters:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_characters and current:
            chunks.append(current.strip())
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def materialize_document_text_chunks(
    session: Session,
    filing_id: int,
    *,
    max_characters: int = DEFAULT_CHUNK_CHARACTERS,
) -> dict[str, Any]:
    """Persist all available page text for later agent retrieval.

    The function intentionally stores OCR responses and native PDF text as
    separate extraction methods.  It does not promote text to a financial fact
    and does not require a PDF coordinate for OCR-originated chunks.
    """

    if max_characters <= 0 or max_characters > 10000:
        raise ValueError("CHUNK_SIZE_INVALID")
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise ValueError("FILING_NOT_FOUND")
    job = session.scalar(select(ExtractionJob).where(
        ExtractionJob.filing_version_id == filing_id,
    ).order_by(ExtractionJob.id.desc()).limit(1))
    extraction_method = job.extraction_method if job is not None else "pdf_text"
    configured_pages = getattr(job, "page_numbers", None) if job is not None else None
    selected_pages = sorted({int(value) for value in (configured_pages or [])
                             if isinstance(value, (int, str)) and str(value).isdigit() and int(value) > 0})
    if selected_pages and getattr(job, "page_scope", "selected") == "all":
        selected_pages = []
    if (extraction_method != "ocr" and getattr(settings, "docling_enabled", False)
            and getattr(settings, "docling_mcp_url", "")):
        try:
            return _materialize_docling_text_chunks(
                session, filing, selected_pages=selected_pages,
                max_characters=max_characters,
            )
        except (RuntimeError, ValueError) as exc:
            # Docling is an optional structural text layer. Preserve the
            # native PDF path as the explicit fallback and expose the failure
            # in the materialization result/audit rather than claiming Docling.
            docling_error = str(exc).split(":", 1)[0]
    else:
        docling_error = None
    pages: list[tuple[int, str, str, str, list[float] | None]] = []
    if extraction_method == "ocr":
        if job is None:
            return {"status": "empty", "reason": "OCR_JOB_NOT_FOUND", "chunk_count": 0}
        calls = list(session.scalars(select(OcrCall).where(
            OcrCall.extraction_job_id == job.id,
            OcrCall.status == "completed",
        ).order_by(OcrCall.page_no, OcrCall.id)))
        latest: dict[int, OcrCall] = {}
        for call in calls:
            current_attempt = job.started_at is None or (call.started_at is not None
                                                        and call.started_at >= job.started_at)
            if (current_attempt and call.response_text
                    and call.response_hash == _text_hash(call.response_text)):
                latest[call.page_no] = call
        for page_no, call in sorted(latest.items()):
            if selected_pages and page_no not in selected_pages:
                continue
            pages.append((page_no, call.response_text or "", call.page_sha256,
                          f"{call.provider}:{call.model}", None))
    else:
        try:
            path = scanned_object_path(session, filing)
        except ValueError as exc:
            if str(exc) != "FILE_SCAN_REQUIRED":
                raise
            return {"status": "unavailable", "reason": "FILE_SCAN_REQUIRED", "chunk_count": 0}
        with pymupdf.open(path) as document:
            extractor_version = f"pymupdf-text-{pymupdf.VersionBind}"
            for page_no, page in enumerate(document, 1):
                if selected_pages and page_no not in selected_pages:
                    continue
                text = page.get_text("text") or ""
                page_hash = _text_hash(text)
                rect = page.rect
                pages.append((page_no, text, page_hash, extractor_version,
                              [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]))

    records: list[DocumentTextChunk] = []
    for page_no, text, page_hash, extractor_version, bbox in pages:
        for chunk_no, chunk_text in enumerate(_split_text(text, max_characters), 1):
            records.append(DocumentTextChunk(
                filing_version_id=filing_id,
                chunk_key=f"{extraction_method}-p{page_no:04d}-{page_hash[:16]}-"
                          f"{_text_hash(text)[:16]}-{chunk_no:04d}",
                page_no=page_no,
                page_sha256=page_hash,
                section=(chunk_text.splitlines()[0].strip() or "page")[:256],
                text=chunk_text,
                text_sha256=_text_hash(chunk_text),
                source_sha256=filing.file_sha256,
                extraction_method=extraction_method,
                extractor_version=extractor_version,
                bounding_box={"page": bbox} if bbox else None,
                related_fact_ids=[],
            ))
    existing = {
            chunk.chunk_key: chunk for chunk in session.scalars(select(DocumentTextChunk).where(
                DocumentTextChunk.filing_version_id == filing_id,
                DocumentTextChunk.source_sha256 == filing.file_sha256,
            ))
        }
    _invalidate_changed_text_snapshots(session, filing.id, existing.values(), records)
    if records:
        for record in records:
            current = existing.get(record.chunk_key)
            if current is None:
                session.add(record)
            else:
                current.page_no = record.page_no
                current.page_sha256 = record.page_sha256
                current.section = record.section
                current.text = record.text
                current.text_sha256 = record.text_sha256
                current.extraction_method = record.extraction_method
                current.extractor_version = record.extractor_version
                current.bounding_box = record.bounding_box
    desired_keys = {record.chunk_key for record in records}
    for key, obsolete in existing.items():
        if key not in desired_keys:
            session.delete(obsolete)
    session.flush()
    result = {
        "status": "available" if records else "empty",
        "filing_version_id": filing_id,
        "source_sha256": filing.file_sha256,
        "extraction_method": extraction_method,
        "page_count": len(pages),
        "chunk_count": len(records),
        "page_numbers": sorted({page_no for page_no, *_ in pages}),
        "chunk_keys": [record.chunk_key for record in records],
        "page_scope": "selected" if selected_pages else "all",
        "docling_status": "failed" if docling_error else "disabled",
        "docling_error": docling_error,
    }
    record_audit_event(
        session,
        filing_id,
        category="retrieval",
        action="document_chunks_materialized",
        status="completed",
        source_ref=f"filing:{filing_id}",
        tool_name="document_chunk_materializer",
        tool_version=RETRIEVAL_VERSION,
        input_hash=stable_hash({"filing_id": filing_id, "source_sha256": filing.file_sha256,
                                "extraction_method": extraction_method}),
        output_hash=stable_hash(result),
        detail={key: value for key, value in result.items() if key not in {"chunk_keys"}},
    )
    return result


def get_evidence_retriever() -> EvidenceRetriever:
    return EvidenceRetriever(settings)


def ensure_text_chunks(session: Session, filing_id: int, *,
                       max_characters: int = DEFAULT_CHUNK_CHARACTERS) -> dict[str, Any]:
    """Populate the chosen PDF/OCR source and attempt the configured MCP index."""

    materialized = materialize_document_text_chunks(
        session, filing_id, max_characters=max_characters,
    )
    if materialized["status"] == "unavailable":
        return materialized
    materialized["vector_index"] = get_evidence_retriever().index_chunks(session, filing_id)
    return materialized


def _materialize_docling_text_chunks(session: Session, filing: FilingVersion, *,
                                     selected_pages: list[int], max_characters: int) -> dict[str, Any]:
    """Materialize a bounded Docling native-text pass with original page refs."""
    from finance_adapters.providers.docling_mcp import DoclingMcpClient

    path = scanned_object_path(session, filing)
    with pymupdf.open(path) as document:
        requested = selected_pages or list(range(1, len(document) + 1))
        if any(page > len(document) for page in requested):
            raise ValueError("DOCLING_PAGE_RANGE_INVALID")
    output_dir = getattr(settings, "docling_mcp_cache_dir", "data/local-services/mcp/docling-output")
    result = DoclingMcpClient(
        url=settings.docling_mcp_url,
        output_dir=output_dir,
        timeout_seconds=float(getattr(settings, "docling_mcp_timeout_seconds", 300.0)),
    ).convert_pages(path, requested, source_sha256=filing.file_sha256,
                    artifact_dir=getattr(settings, "docling_artifact_dir", None))
    records: list[DocumentTextChunk] = []
    for number, item in enumerate(result.get("chunks", []), 1):
        text = str(item.get("text", ""))
        if not text.strip():
            continue
        page_no = int(item["page_no"])
        for offset in range(0, len(text), max_characters):
            chunk_text = text[offset:offset + max_characters]
            bbox = {**(item.get("bbox") or {}), "text_span": [offset, offset + len(chunk_text)],
                    "node_text_sha256": item["text_sha256"]}
            records.append(DocumentTextChunk(
                filing_version_id=filing.id,
                chunk_key=f"docling-p{page_no:04d}-{item['text_sha256'][:16]}-{number:04d}-{offset:06d}",
                page_no=page_no, page_sha256=str(item["page_sha256"]),
                section=str(item.get("section", "page"))[:256], text=chunk_text,
                text_sha256=_text_hash(chunk_text), source_sha256=filing.file_sha256,
                extraction_method="docling",
                extractor_version=str(item.get("extractor_version", "docling")),
                bounding_box=bbox, related_fact_ids=[],
            ))
    # Docling can separate a heading, checkbox options and explanatory prose
    # into different nodes. Preserve the full selected native page alongside
    # those nodes so the existing closed-question validator can inspect the
    # actual boundaries. OCR never enters this native-only branch.
    native_question_pages = []
    with pymupdf.open(path) as document:
        for page_no in requested:
            page = document[page_no - 1]
            native_text = page.get_text("text") or ""
            if not re.search(r"是否|[□☐☑✓✔√\uf052\uf0fe]", native_text):
                continue
            digest = _text_hash(native_text)
            records.append(DocumentTextChunk(
                filing_version_id=filing.id,
                chunk_key=f"native-question-page-v1-p{page_no:04d}-{digest}",
                page_no=page_no, page_sha256=digest, section="原页问项与勾选上下文",
                text=native_text, text_sha256=digest, source_sha256=filing.file_sha256,
                extraction_method="pdf_text", extractor_version="native-question-page-v1",
                bounding_box={"page": list(page.rect), "coord_origin": "TOPLEFT",
                              "context_kind": "complete_native_page_not_preapproved_question",
                              "docling_json_sha256": result.get("json_sha256")},
                related_fact_ids=[],
            ))
            native_question_pages.append(page_no)
    existing = {chunk.chunk_key: chunk for chunk in session.scalars(select(DocumentTextChunk).where(
        DocumentTextChunk.filing_version_id == filing.id,
        DocumentTextChunk.source_sha256 == filing.file_sha256,
    ))}
    _invalidate_changed_text_snapshots(session, filing.id, existing.values(), records)
    for record in records:
        current = existing.get(record.chunk_key)
        if current is None:
            session.add(record)
        else:
            current.page_no, current.page_sha256, current.section = record.page_no, record.page_sha256, record.section
            current.text, current.text_sha256, current.extraction_method = record.text, record.text_sha256, record.extraction_method
            current.extractor_version, current.bounding_box = record.extractor_version, record.bounding_box
    desired = {record.chunk_key for record in records}
    for key, obsolete in existing.items():
        if key not in desired:
            session.delete(obsolete)
    session.flush()
    materialized = {"status": "available" if records else "empty", "filing_version_id": filing.id,
            "source_sha256": filing.file_sha256, "extraction_method": "docling",
            "page_count": len(result.get("page_numbers", requested)), "page_numbers": requested,
            "chunk_count": len(records), "chunk_keys": [record.chunk_key for record in records],
            "native_question_page_numbers": native_question_pages,
            "docling_status": "completed", "docling_manifest": result.get("manifest_file"),
            "docling_json_sha256": result.get("json_sha256")}
    record_audit_event(
        session, filing.id, category="retrieval", action="document_chunks_materialized",
        status="completed", source_ref=f"filing:{filing.id}", tool_name="docling-mcp",
        tool_version=result["extractor_version"],
        input_hash=stable_hash({"source_sha256": filing.file_sha256, "page_numbers": requested}),
        output_hash=stable_hash(materialized),
        detail={key: value for key, value in materialized.items() if key != "chunk_keys"},
    )
    return materialized


def _invalidate_changed_text_snapshots(session: Session, filing_id: int,
                                       previous: Iterable[DocumentTextChunk],
                                       current: Iterable[DocumentTextChunk]) -> None:
    before = sorted(_chunk_review_digest(chunk) for chunk in previous)
    after = sorted(_chunk_review_digest(chunk) for chunk in current)
    if not before or before == after:
        return
    for run in session.scalars(select(AnalysisRun).where(
        AnalysisRun.invalidated_at.is_(None),
        AnalysisRun.status.in_(["queued", "running", "completed", "partial_result",
                                "waiting_for_documents"]),
    )):
        trace = run.tool_trace if isinstance(run.tool_trace, dict) else {}
        output = run.output if isinstance(run.output, dict) else {}
        snapshots = [value.get("document_snapshot", [])
                     for value in (trace.get("agent"), output.get("agent_workflow"))
                     if isinstance(value, dict)]
        if run.filing_version_id != filing_id and not any(
                isinstance(item, dict) and item.get("filing_version_id") == filing_id
                for documents in snapshots if isinstance(documents, list) for item in documents):
            continue
        run.invalidated_at = datetime.now(UTC)
        run.status = "invalidated"
        run.failure_reason = "DOCUMENT_TEXT_SNAPSHOT_CHANGED"
        record_audit_event(session, run.filing_version_id, category="generation",
            action="analysis_text_snapshot_invalidated", status="invalidated",
            source_ref=f"filing:{filing_id}", tool_name="document_chunk_materializer",
            tool_version=RETRIEVAL_VERSION, input_hash=stable_hash(before), output_hash=stable_hash(after),
            detail={"analysis_run_id": run.id, "source_filing_version_id": filing_id,
                    "reason": "Document text snapshot changed; historical output retained."})


def review_text_chunk(session: Session, filing_id: int, chunk_id: int, *, decision: str,
                      reviewer_id: str, reason: str, expected_text_sha256: str) -> dict[str, Any]:
    """Confirm/reject one immutable text snapshot without approving strict facts."""

    if decision not in {"confirmed", "rejected"}:
        raise ValueError("TEXT_REVIEW_DECISION_INVALID")
    if not reviewer_id.strip() or not reason.strip():
        raise ValueError("TEXT_REVIEW_IDENTITY_AND_REASON_REQUIRED")
    filing = session.get(FilingVersion, filing_id)
    chunk = session.get(DocumentTextChunk, chunk_id)
    if filing is None:
        raise ValueError("FILING_NOT_FOUND")
    if (chunk is None or chunk.filing_version_id != filing_id
            or chunk.source_sha256 != filing.file_sha256):
        raise ValueError("TEXT_CHUNK_NOT_FOUND")
    if not _chunk_integrity(chunk) or chunk.text_sha256 != expected_text_sha256:
        raise ValueError("TEXT_REVIEW_SNAPSHOT_CHANGED")
    previous_status = _current_review_status(chunk)
    chunk.review_status = decision
    chunk.review_digest = _chunk_review_digest(chunk)
    chunk.reviewer_id = reviewer_id.strip()[:128]
    chunk.reviewed_at = datetime.now(UTC)
    record_audit_event(
        session, filing_id, category="review", action="document_text_reviewed", status=decision,
        source_ref=f"text-chunk:{chunk.id}", tool_name="human_text_review",
        tool_version=RETRIEVAL_VERSION, input_hash=chunk.review_digest,
        output_hash=stable_hash({"decision": decision, "reviewer_id": chunk.reviewer_id,
                                 "reason": reason.strip()}),
        detail={"chunk_id": chunk.id, "text_sha256": chunk.text_sha256,
                "source_sha256": chunk.source_sha256, "page_no": chunk.page_no,
                "extraction_method": chunk.extraction_method, "decision": decision,
                "reviewer_id": chunk.reviewer_id, "reason": reason.strip()},
    )
    if previous_status != decision:
        # A supplementary filing can support an analysis anchored to another
        # report. Invalidate that dependency as well as the filing's own runs;
        # retain the original output and trace as historical execution evidence.
        for run in session.scalars(select(AnalysisRun).where(
            AnalysisRun.invalidated_at.is_(None),
            AnalysisRun.status.in_(["queued", "running", "completed", "partial_result",
                                    "waiting_for_documents"]),
        )):
            trace = run.tool_trace if isinstance(run.tool_trace, dict) else {}
            output = run.output if isinstance(run.output, dict) else {}
            snapshots = [
                value.get("document_snapshot", [])
                for value in (trace.get("agent"), output.get("agent_workflow"))
                if isinstance(value, dict)
            ]
            depends_on_filing = run.filing_version_id == filing_id or any(
                isinstance(document, dict) and document.get("filing_version_id") == filing_id
                for documents in snapshots if isinstance(documents, list)
                for document in documents
            )
            if not depends_on_filing:
                continue
            prior_run_status = run.status
            run.invalidated_at = datetime.now(UTC)
            run.status = "invalidated"
            run.failure_reason = "DOCUMENT_TEXT_REVIEW_CHANGED"
            record_audit_event(
                session, run.filing_version_id, category="generation",
                action="analysis_text_evidence_invalidated", status="invalidated",
                source_ref=f"text-chunk:{chunk.id}", tool_name="human_text_review",
                tool_version=RETRIEVAL_VERSION, input_hash=run.input_snapshot_hash,
                detail={"analysis_run_id": run.id, "source_filing_version_id": filing_id,
                        "chunk_id": chunk.id, "previous_review_status": previous_status,
                        "review_status": decision, "previous_analysis_status": prior_run_status,
                        "reason": "Document text review changed; historical output retained."},
            )
    session.flush()
    return get_evidence_retriever()._chunk_result(chunk, score=1.0, backend="sqlite").as_dict()


def list_documents(session: Session, *, filing_ids: Iterable[int] | None = None,
                   company_id: int | None = None) -> list[dict[str, Any]]:
    query = select(FilingVersion)
    selected_ids = [int(value) for value in filing_ids] if filing_ids is not None else []
    if selected_ids:
        query = query.where(FilingVersion.id.in_(selected_ids))
    if company_id is not None:
        query = query.where(FilingVersion.company_id == company_id)
    return [{
        "id": filing.id, "filing_version_id": filing.id, "company_id": filing.company_id,
        "report_type": filing.report_type, "period_start": _iso_date(filing.period_start),
        "period_end": _iso_date(filing.period_end), "period_basis": filing.period_basis,
        "scope": filing.scope, "status": filing.status, "source_sha256": filing.file_sha256,
        "coverage": get_evidence_retriever().document_coverage(session, filing.id),
    } for filing in session.scalars(query.order_by(FilingVersion.period_end, FilingVersion.id))]


def snapshot(session: Session, filing_ids: Iterable[int]) -> dict[str, Any]:
    """Freeze local retrieval inputs; no timestamps or remote rank are included."""

    selected_ids = sorted({int(value) for value in filing_ids})
    filings = list(session.scalars(select(FilingVersion).where(
        FilingVersion.id.in_(selected_ids)
    ).order_by(FilingVersion.id)))
    if len(filings) != len(selected_ids):
        raise ValueError("FILING_NOT_FOUND")
    documents = []
    retriever = get_evidence_retriever()
    for filing in filings:
        chunks = [chunk for chunk in session.scalars(select(DocumentTextChunk).where(
            *_chunk_source_filter(filing.id, filing.file_sha256),
        ).order_by(DocumentTextChunk.page_no, DocumentTextChunk.id)) if _chunk_integrity(chunk)]
        documents.append({
            "filing_version_id": filing.id, "source_sha256": filing.file_sha256,
            "period_start": _iso_date(filing.period_start), "period_end": _iso_date(filing.period_end),
            "report_type": filing.report_type, "period_basis": filing.period_basis,
            "scope": filing.scope,
            "chunks": [retriever._chunk_result(chunk, score=0.0, backend="sqlite").as_dict()
                       for chunk in chunks],
        })
    content = {"version": RETRIEVAL_VERSION, "filing_ids": selected_ids, "documents": documents}
    return {**content, "snapshot_hash": stable_hash(content)}


def document_coverage(session: Session, filing_id: int) -> dict[str, Any]:
    return get_evidence_retriever().document_coverage(session, filing_id)


def search_text(session: Session, filing_id: int, query: str, *, limit: int = 8,
                page_numbers: Iterable[int] | None = None, section: str | None = None) -> dict[str, Any]:
    return get_evidence_retriever().search_text(
        session, filing_id, query, limit=limit, page_numbers=page_numbers, section=section,
    )


def read_chunks(session: Session, filing_id: int, *, chunk_ids: Iterable[int] | None = None,
                page_numbers: Iterable[int] | None = None, section: str | None = None,
                limit: int = MAX_RESULT_LIMIT) -> list[dict[str, Any]]:
    return get_evidence_retriever().read_chunks(
        session, filing_id, chunk_ids=chunk_ids, page_numbers=page_numbers,
        section=section, limit=limit,
    )


def list_review_chunks(session: Session, filing_id: int, *,
                       page_numbers: Iterable[int] | None = None, review_status: str | None = None,
                       include_rejected: bool = True, limit: int = MAX_RESULT_LIMIT) -> list[dict[str, Any]]:
    """Human-review surface only; never use this as an analysis retrieval tool."""

    return get_evidence_retriever().list_review_chunks(
        session, filing_id, page_numbers=page_numbers, review_status=review_status,
        include_rejected=include_rejected, limit=limit,
    )


def list_facts(session: Session, filing_id: int, *, concept_codes: Iterable[str] | None = None,
               verified_only: bool = False, limit: int = MAX_RESULT_LIMIT) -> list[dict[str, Any]]:
    return get_evidence_retriever().list_facts(
        session, filing_id, concept_codes=concept_codes, verified_only=verified_only, limit=limit,
    )

