"""Optional Semantica provenance graph for cited financial-report candidates.

This adapter records *citation and derivation*, not semantic truth. Every graph
is scoped to one issuer and one frozen filing; it never promotes a candidate
to a FinancialFact or substitutes graph connectivity for disclosure coverage.
Semantica performs lineage storage and checksum verification without any LLM,
embedding service, model download, or shared global storage.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

GRAPH_VERSION = "financial_evidence_provenance_v1"
SEMANTICA_VERSION = "0.7.0"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    return dict(dump(mode="json")) if callable(dump) else {}


def _load_semantica() -> tuple[Any, Any, str]:
    # Keep imports lazy: the financial workflow remains usable without this extra.
    provenance = importlib.import_module("semantica.provenance")
    return (
        provenance.ProvenanceManager,
        provenance.InMemoryStorage,
        importlib.metadata.version("semantica"),
    )


def _source_records(sources: Mapping[str, Any] | Sequence[Any]) -> list[dict[str, Any]]:
    if isinstance(sources, Mapping):
        records = []
        for key, value in sources.items():
            record = _mapping(value)
            record.setdefault("id", str(key))
            records.append(record)
        return records
    return [_mapping(item) for item in sources]


def _source_error(
    source: dict[str, Any], issuer_id: str, filing_version_id: int, source_sha256: str,
) -> str | None:
    if source.get("filing_version_id") != filing_version_id:
        return "source_outside_filing_scope"
    for field in ("company_id", "issuer_id"):
        if source.get(field) is not None and str(source[field]) != issuer_id:
            return "source_outside_issuer_scope"
    source_id = source.get("id") or source.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        return "source_id_missing"
    if source.get("source_sha256") != source_sha256:
        return "source_document_hash_mismatch"
    text = source.get("text")
    if not isinstance(text, str) or not text:
        return "source_text_missing"
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != source.get("text_sha256"):
        return "source_text_hash_mismatch"
    if not isinstance(source.get("page_sha256"), str) or not _SHA256.fullmatch(
        source["page_sha256"]
    ):
        return "source_page_hash_missing"
    page_no = source.get("page_no")
    if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no <= 0:
        return "source_page_invalid"
    return None


def _finish(result: dict[str, Any]) -> dict[str, Any]:
    # Hash only stable graph semantics, excluding vendor wall-clock timestamps.
    result["graph_sha256"] = _hash(result)
    return result


def build_semantic_evidence_graph(
    *, issuer_id: str | int, filing_version_id: int, source_sha256: str,
    sources: Mapping[str, Any] | Sequence[Any], findings: Sequence[Any], enabled: bool = True,
) -> dict[str, Any]:
    """Build one version-isolated, candidate-only citation/provenance graph.

    ``issuer_id`` and ``source_sha256`` must come from the frozen document
    snapshot, rather than a generated finding. Cross-filing findings are
    rejected from this graph instead of silently joining different reports.
    The result is JSON serializable and suitable for a frozen tool trace.
    A ``ready`` status certifies graph/lineage construction only: it does not
    certify any statement's financial meaning, source completeness, or truth.
    """
    if (
        isinstance(issuer_id, bool) or not isinstance(issuer_id, (str, int))
        or not str(issuer_id).strip()
        or isinstance(filing_version_id, bool) or not isinstance(filing_version_id, int)
        or filing_version_id <= 0 or not isinstance(source_sha256, str)
        or not _SHA256.fullmatch(source_sha256)
    ):
        raise ValueError("SEMANTIC_GRAPH_SCOPE_INVALID")
    issuer = str(issuer_id)
    scope = {"issuer_id": issuer, "filing_version_id": filing_version_id,
             "source_sha256": source_sha256}
    result: dict[str, Any] = {
        "version": GRAPH_VERSION, "backend": "semantica.provenance",
        "backend_version": None, "status": "disabled", "scope": scope,
        "strict_fact_eligible": False, "semantic_validation_performed": False,
        "nodes": [], "edges": [], "candidate_lineages": [],
        "rejected_sources": [], "rejected_candidates": [],
    }
    if not enabled:
        return _finish(result)
    try:
        manager_type, storage_type, version = _load_semantica()
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        result.update(status="dependency_missing", error_code=type(exc).__name__)
        return _finish(result)
    except Exception as exc:  # noqa: BLE001 - optional vendor import boundary
        result.update(status="backend_error", error_code=type(exc).__name__)
        return _finish(result)
    result["backend_version"] = version
    if version != SEMANTICA_VERSION:
        result.update(status="unsupported_version", expected_version=SEMANTICA_VERSION)
        return _finish(result)

    scope_id = "filing:" + _hash(scope)
    nodes: list[dict[str, Any]] = [{"id": scope_id, "type": "filing", **scope}]
    edges: list[dict[str, Any]] = []
    valid_sources: dict[str, dict[str, Any]] = {}
    conflicted_ids: set[str] = set()
    for source in _source_records(sources):
        source_id = source.get("id") or source.get("source_id")
        error = _source_error(source, issuer, filing_version_id, source_sha256)
        if not error and source_id in valid_sources and source != valid_sources[source_id]:
            error = "source_id_conflict"
            conflicted_ids.add(source_id)
        if error:
            result["rejected_sources"].append({"source_id": source_id, "reason": error})
        elif source_id not in conflicted_ids:
            valid_sources[source_id] = source
    for source_id in conflicted_ids:
        valid_sources.pop(source_id, None)

    source_node_ids: dict[str, str] = {}
    for source_id, source in sorted(valid_sources.items()):
        node_id = "source:" + _hash({"scope": scope, "source_id": source_id})
        source_node_ids[source_id] = node_id
        nodes.append({
            "id": node_id, "type": "source", "source_id": source_id, **scope,
            "page_no": source["page_no"], "page_sha256": source["page_sha256"],
            "text_sha256": source["text_sha256"], "text": source["text"],
            "extraction_method": source.get("extraction_method"),
            "extractor_version": source.get("extractor_version"),
        })
        edges.append({"from": node_id, "to": scope_id, "relation": "extracted_from"})

    candidate_ids: set[str] = set()
    for raw in findings:
        candidate = _mapping(raw)
        candidate_key = _hash(candidate)
        source_ids = candidate.get("source_ids") or []
        if (
            not isinstance(source_ids, list)
            or not all(isinstance(item, str) and item for item in source_ids)
        ):
            error = "candidate_source_ids_invalid"
        elif not source_ids:
            error = "candidate_has_no_text_sources"
        elif len(source_ids) != len(set(source_ids)):
            error = "candidate_source_ids_duplicate"
        elif any(item not in valid_sources for item in source_ids):
            error = "candidate_source_outside_valid_scope"
        elif candidate.get("filing_version_id", filing_version_id) != filing_version_id:
            error = "candidate_outside_filing_scope"
        elif any(candidate.get(field) is not None and str(candidate[field]) != issuer
                 for field in ("company_id", "issuer_id")):
            error = "candidate_outside_issuer_scope"
        else:
            error = None
        if error:
            result["rejected_candidates"].append({"candidate_sha256": candidate_key,
                                                  "reason": error})
            continue
        node_id = "candidate:" + _hash({"scope": scope, "candidate_sha256": candidate_key})
        if node_id in candidate_ids:
            continue
        candidate_ids.add(node_id)
        nodes.append({"id": node_id, "type": "candidate", **scope,
                      "candidate_sha256": candidate_key, "candidate": candidate,
                      "strict_fact_eligible": False})
        for source_id in sorted(source_ids):
            edges.append({"from": node_id, "to": source_node_ids[source_id],
                          "relation": "cites", "semantic_support_verified": False})
    nodes.sort(key=lambda item: item["id"])
    edges.sort(key=lambda item: (item["from"], item["to"], item["relation"]))

    try:
        # Explicit per-call storage prevents data leaking across issuers/reports.
        manager = manager_type(storage=storage_type())
        node_map = {node["id"]: node for node in nodes}
        ordered_nodes = [node_map[scope_id]] + [node for node in nodes if node["type"] == "source"]
        ordered_nodes += [node for node in nodes if node["type"] == "candidate"]
        for node in ordered_nodes:
            used = sorted(edge["to"] for edge in edges if edge["from"] == node["id"])
            entry = manager.track_entity(
                entity_id=node["id"], source=scope_id, metadata=node,
                entity_type=node["type"], used_entities=used,
                parent_entity_id=scope_id if node["id"] != scope_id else None,
                source_location=f"page:{node['page_no']}" if "page_no" in node else None,
                source_quote=node.get("text"), bundle_id=scope_id,
                activity_id=GRAPH_VERSION, agent_id="finreport-semantic-graph",
                # Provenance confidence is deliberately not a truth probability.
                confidence=0.0,
            )
            if entry is None:
                raise RuntimeError("SEMANTICA_ENTITY_NOT_STORED")
        for edge in edges:
            entry = manager.track_relationship(
                relationship_id="edge:" + _hash(edge), source=scope_id,
                metadata={**edge, **scope, "strict_fact_eligible": False},
                bundle_id=scope_id, activity_id=GRAPH_VERSION,
                agent_id="finreport-semantic-graph", confidence=0.0,
            )
            if entry is None:
                raise RuntimeError("SEMANTICA_RELATIONSHIP_NOT_STORED")
        lineages = []
        for node_id in sorted(candidate_ids):
            lineage = manager.get_lineage(node_id)
            if lineage.get("integrity_verified") is not True:
                raise RuntimeError("SEMANTICA_LINEAGE_INTEGRITY_FAILED")
            ancestor_ids = sorted(item["entity_id"] for item in lineage["lineage_chain"])
            expected = {scope_id, node_id} | {
                edge["to"] for edge in edges if edge["from"] == node_id
            }
            if set(ancestor_ids) != expected:
                raise RuntimeError("SEMANTICA_LINEAGE_MEMBERS_MISMATCH")
            lineages.append({"candidate_node_id": node_id, "ancestor_node_ids": ancestor_ids,
                             "integrity_verified": True})
        result.update(status="ready", nodes=nodes, edges=edges, candidate_lineages=lineages)
    except Exception as exc:  # noqa: BLE001 - isolate optional backend failure
        # Never publish a partly constructed graph as successfully verified.
        result.update(status="backend_error", error_code=type(exc).__name__)
    result["rejected_sources"].sort(key=lambda item: json.dumps(item, sort_keys=True))
    result["rejected_candidates"].sort(key=lambda item: json.dumps(item, sort_keys=True))
    return _finish(result)
