"""Build bounded, replayable context from frozen document text.

Page adjacency and declared links establish where context came from. They do
not establish semantic entailment or prove that a selected source is an
independent disclosure. This module never accepts a model's ``supports`` flag.
Missing pages are local-read work, not an instruction to request a new filing.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from finance_adapters.persistence.audit import stable_hash

CONTEXT_VERSION = "frozen-evidence-context-v1"
_LINK_KINDS = frozenset({
    "table_continuation", "unit_scope", "period_scope", "footnote_scope", "explicit_context",
})
_CONTEXT_KINDS = ("unit_scope", "period_scope", "footnote_scope", "table_continuation")
_HASH = re.compile(r"[0-9a-f]{64}")


def _fail(code: str) -> None:
    raise ValueError(f"EVIDENCE_CONTEXT_{code}")


def _positive_int(value: object, code: str) -> int:
    if type(value) is not int or value < 1:
        _fail(code)
    return value


def _hash(value: object, code: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail(code)
    return value


def _document_hash(value: Mapping[str, Any]) -> str:
    left, right = value.get("source_sha256"), value.get("document_sha256")
    if left is not None and right is not None and left != right:
        _fail("DOCUMENT_HASH_CONFLICT")
    return _hash(left if left is not None else right, "DOCUMENT_HASH_INVALID")


def _scope(value: Mapping[str, Any]) -> tuple[int, str]:
    return (_positive_int(value.get("filing_version_id"), "FILING_VERSION_INVALID"),
            _document_hash(value))


def _source_rows(sources: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]]) -> list:
    if isinstance(sources, Mapping):
        rows = []
        for key, source in sources.items():
            if not isinstance(source, Mapping):
                _fail("SOURCE_INVALID")
            row = copy.deepcopy(dict(source))
            if "id" not in row and "source_id" not in row:
                row["id"] = key
            if key != row.get("source_id", row.get("id")):
                _fail("SOURCE_ID_CONFLICT")
            rows.append(row)
        return rows
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        _fail("SOURCES_INVALID")
    return list(sources)


def _index_sources(sources: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]]) -> dict:
    result = {}
    for source in _source_rows(sources):
        if not isinstance(source, Mapping):
            _fail("SOURCE_INVALID")
        value = copy.deepcopy(dict(source))
        source_id = value.get("source_id", value.get("id"))
        if not isinstance(source_id, str) or not source_id.strip():
            _fail("SOURCE_ID_INVALID")
        if value.get("id", source_id) != source_id:
            _fail("SOURCE_ID_CONFLICT")
        if source_id in result:
            _fail("SOURCE_ID_DUPLICATED")
        text = value.get("text")
        if not isinstance(text, str) or not text:
            _fail("SOURCE_TEXT_INVALID")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if value.get("text_sha256", digest) != digest:
            _fail("SOURCE_TEXT_HASH_MISMATCH")
        _scope(value)
        _positive_int(value.get("page_no"), "PAGE_INVALID")
        if value.get("page_sha256") is not None:
            _hash(value["page_sha256"], "PAGE_HASH_INVALID")
        value.update(source_id=source_id, text_sha256=digest,
                     document_sha256=_document_hash(value), source_sha256=_document_hash(value))
        result[source_id] = value
    return result


def _anchor(value: Mapping[str, Any], index: Mapping[str, dict]) -> dict:
    if not isinstance(value, Mapping):
        _fail("ANCHOR_INVALID")
    source_id = value.get("source_id")
    if source_id not in index:
        _fail("ANCHOR_SOURCE_MISSING")
    source = index[source_id]
    start, end = value.get("start"), value.get("end")
    if (type(start) is not int or type(end) is not int or start < 0
            or end <= start or end > len(source["text"])):
        _fail("ANCHOR_SPAN_INVALID")
    if value.get("text_sha256") != source["text_sha256"]:
        _fail("ANCHOR_HASH_MISMATCH")
    quote = source["text"][start:end]
    quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    if "quote" not in value and "quote_sha256" not in value:
        _fail("ANCHOR_QUOTE_MISSING")
    if ("quote" in value and value["quote"] != quote
            or "quote_sha256" in value and value["quote_sha256"] != quote_hash):
        _fail("ANCHOR_QUOTE_MISMATCH")
    return {"source_id": source_id, "start": start, "end": end,
            "quote_sha256": quote_hash, "text_sha256": source["text_sha256"]}


def _links(values: Sequence[Mapping[str, Any]], index: Mapping[str, dict], scope: tuple) -> list:
    links = []
    seen = set()
    for value in values:
        if (not isinstance(value, Mapping) or not isinstance(value.get("kind"), str)
                or value["kind"] not in _LINK_KINDS):
            _fail("LINK_KIND_INVALID")
        if any(key in value for key in ("supports", "entailed", "semantic_support_verified")):
            _fail("MODEL_SUPPORT_FLAG_FORBIDDEN")
        origin, target = value.get("from_source_id"), value.get("to_source_id")
        if (not isinstance(origin, str) or not isinstance(target, str)
                or origin not in index or target not in index):
            _fail("LINK_SOURCE_MISSING")
        if _scope(index[origin]) != scope or _scope(index[target]) != scope:
            _fail("LINK_DOCUMENT_SCOPE_MISMATCH")
        provenance = value.get("provenance")
        if (not isinstance(provenance, Mapping)
                or provenance.get("method") not in {"deterministic_rule", "source_markup", "test_annotation"}
                or not isinstance(provenance.get("rule_id"), str) or not provenance["rule_id"]
                or not isinstance(provenance.get("rule_version"), str) or not provenance["rule_version"]):
            _fail("LINK_PROVENANCE_INVALID")
        raw_anchors = value.get("anchors", [])
        if not isinstance(raw_anchors, Sequence) or isinstance(raw_anchors, (str, bytes)):
            _fail("ANCHORS_INVALID")
        anchors = [_anchor(item, index) for item in raw_anchors]
        if {item["source_id"] for item in anchors} != {origin, target}:
            _fail("LINK_ENDPOINT_ANCHOR_MISSING")
        page_range = value.get("page_range")
        if (not isinstance(page_range, list) or len(page_range) != 2
                or any(type(page) is not int or page < 1 for page in page_range)
                or page_range[0] > page_range[1] or page_range[1] - page_range[0] > 10000
                or not all(page_range[0] <= index[item]["page_no"] <= page_range[1]
                           for item in {origin, target})):
            _fail("LINK_PAGE_RANGE_INVALID")
        link = {"kind": value["kind"], "from_source_id": origin, "to_source_id": target,
                "anchors": sorted(anchors, key=lambda item: (item["source_id"], item["start"], item["end"])),
                "page_range": page_range, "provenance": copy.deepcopy(dict(provenance)),
                "semantic_support_verified": False}
        link_id = stable_hash(link)
        if link_id in seen:
            continue
        seen.add(link_id)
        link["id"] = f"context-link:{link_id}"
        links.append(link)
    return sorted(links, key=lambda item: item["id"])


def build_evidence_context(
    sources: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    center_source_ids: Sequence[str],
    *,
    documents: Sequence[Mapping[str, Any]] | None = None,
    explicit_links: Sequence[Mapping[str, Any]] | None = None,
    adjacent_pages: int = 1,
    max_sources: int = 12,
    max_characters: int = 36000,
    required_context: Sequence[str] = ("unit_scope", "period_scope"),
) -> dict:
    """Freeze one document/version context, retaining every included source in full.

    ``required_context`` defaults to numeric evidence prerequisites. Callers
    handling narrative text may explicitly pass ``()``. A resolved link means
    its endpoints and raw anchors were fetched, never that its meaning or its
    declared scope was independently verified.
    """
    if type(adjacent_pages) is not int or not 0 <= adjacent_pages <= 8:
        _fail("ADJACENT_PAGE_LIMIT_INVALID")
    if type(max_sources) is not int or not 1 <= max_sources <= 64:
        _fail("SOURCE_LIMIT_INVALID")
    if type(max_characters) is not int or not 1 <= max_characters <= 256000:
        _fail("CHARACTER_LIMIT_INVALID")
    if (not isinstance(center_source_ids, Sequence)
            or isinstance(center_source_ids, (str, bytes)) or not center_source_ids):
        _fail("CENTER_IDS_INVALID")
    if any(not isinstance(item, str) for item in center_source_ids):
        _fail("CENTER_IDS_INVALID")
    centers = sorted(set(center_source_ids))
    if (not isinstance(required_context, Sequence) or isinstance(required_context, (str, bytes))
            or any(item not in _CONTEXT_KINDS for item in required_context)):
        _fail("REQUIRED_CONTEXT_INVALID")
    required_context = sorted(set(required_context))
    index = _index_sources(sources)
    if any(item not in index for item in centers):
        _fail("CENTER_SOURCE_MISSING")
    scope = _scope(index[centers[0]])
    if any(_scope(index[item]) != scope for item in centers):
        _fail("CENTERS_MIXED_DOCUMENTS")
    local = {key: value for key, value in index.items() if _scope(value) == scope}
    sort_key = lambda key: (local[key]["page_no"], key)
    local_ids = sorted(local, key=sort_key)
    document_scope = {"filing_version_id": scope[0], "source_sha256": scope[1],
                      "document_sha256": scope[1]}
    snapshots = []
    if documents is not None and (not isinstance(documents, Sequence)
                                  or isinstance(documents, (str, bytes))):
        _fail("DOCUMENTS_INVALID")
    for document in documents or []:
        if not isinstance(document, Mapping):
            _fail("DOCUMENT_INVALID")
        if _scope(document) == scope:
            snapshots.append(copy.deepcopy(dict(document)))
    if len(snapshots) > 1:
        _fail("DOCUMENT_SNAPSHOT_DUPLICATED")
    if explicit_links is not None and (not isinstance(explicit_links, Sequence)
                                      or isinstance(explicit_links, (str, bytes))):
        _fail("LINKS_INVALID")
    if explicit_links is not None and len(explicit_links) > 256:
        _fail("LINK_LIMIT_EXCEEDED")
    links = _links(explicit_links or [], index, scope)
    # Only links connected to a requested center are eligible. Chains are not
    # recursively expanded into the whole document.
    relevant_links = [item for item in links if item["from_source_id"] in centers
                      or item["to_source_id"] in centers]
    linked_ids = {item[key] for item in relevant_links for key in ("from_source_id", "to_source_id")}
    center_pages = {local[item]["page_no"] for item in centers}
    requested_pages = {page + delta for page in center_pages
                       for delta in range(-adjacent_pages, adjacent_pages + 1) if page + delta > 0}
    adjacent_ids = {key for key, value in local.items() if value["page_no"] in requested_pages}
    ordered = (sorted(centers, key=sort_key)
               + sorted(linked_ids - set(centers), key=sort_key)
               + sorted(adjacent_ids - linked_ids - set(centers), key=sort_key))
    included, deferred, characters = [], [], 0
    for source_id in ordered:
        size = len(local[source_id]["text"])
        if len(included) >= max_sources or characters + size > max_characters:
            deferred.append(source_id)
        else:
            included.append(source_id)
            characters += size
    included_set = set(included)
    gaps = []
    def gap(code: str, **detail: Any) -> None:
        gaps.append({"code": code, **detail})
    if not snapshots:
        gap("document_metadata_not_supplied", next_action="read_local_document_metadata")
    available_pages = {value["page_no"] for value in local.values()}
    for page in sorted(requested_pages - available_pages):
        gap("adjacent_page_not_loaded", page_no=page, next_action="read_local_document_page")
    for source_id in sorted(deferred, key=sort_key):
        gap("source_deferred_by_budget", source_id=source_id, page_no=local[source_id]["page_no"],
            next_action="read_local_source_in_next_package")
    missing_centers = sorted(set(centers) - included_set)
    if missing_centers:
        gap("center_not_in_package", source_ids=missing_centers,
            next_action="read_local_source_in_next_package")
    associations = []
    for center in centers:
        for candidate in included:
            if candidate == center or candidate not in adjacent_ids:
                continue
            same_page = local[center]["page_no"] == local[candidate]["page_no"]
            associations.append({"kind": "same_page_unordered" if same_page else "adjacent_page",
                                 "from_source_id": center, "to_source_id": candidate,
                                 "status": "context_only", "semantic_support_verified": False,
                                 "provenance": {"method": "document_page_number",
                                                "page_delta": local[candidate]["page_no"] - local[center]["page_no"]}})
    scope_deferred = set()
    for link in relevant_links:
        fetched = {link["from_source_id"], link["to_source_id"]} <= included_set
        first, last = link["page_range"]
        missing_pages = sorted(set(range(first, last + 1)) - available_pages)
        not_in_package = [key for key in local_ids if first <= local[key]["page_no"] <= last
                          and key not in included_set]
        scope_deferred.update(not_in_package)
        association = copy.deepcopy(link)
        association.update(status="anchors_fetched" if fetched else "needs_context",
                           scope_status="not_semantically_verified",
                           missing_local_pages=missing_pages)
        associations.append(association)
        if missing_pages:
            gap("declared_scope_pages_not_loaded", link_id=link["id"], page_numbers=missing_pages,
                next_action="read_local_document_pages")
        if not fetched:
            gap("linked_context_not_in_package", link_id=link["id"],
                next_action="read_local_source_in_next_package")
        if not_in_package:
            gap("declared_scope_sources_not_in_package", link_id=link["id"],
                source_ids=not_in_package, next_action="read_local_source_in_next_package")
    states = []
    for center in centers:
        for kind in _CONTEXT_KINDS:
            matched = [item for item in associations if item["kind"] == kind
                       and center in {item["from_source_id"], item["to_source_id"]}]
            collected = any(item["status"] == "anchors_fetched" for item in matched)
            states.append({"source_id": center, "kind": kind,
                           "status": "anchors_fetched" if collected else "not_established",
                           "semantic_support_verified": False,
                           "link_ids": [item["id"] for item in matched]})
            if kind in required_context and not collected:
                gap("required_context_not_established", source_id=center, kind=kind,
                    next_action="inspect_local_document_context")
    # Source IDs and a corpus digest let replay detect changes even to a source
    # omitted by this package's size budget.
    corpus = [{"source_id": key, "snapshot_hash": stable_hash(local[key])} for key in local_ids]
    package = {
        "version": CONTEXT_VERSION, "document_scope": document_scope,
        "document_snapshot": snapshots[0] if snapshots else None,
        "center_source_ids": centers, "sources": [local[key] for key in included],
        "source_units": [{"source_id": key,
                          "span": {"start": 0, "end": len(local[key]["text"]),
                                   "coordinate_space": "frozen_source_unicode_text",
                                   "text_sha256": local[key]["text_sha256"]},
                          "source_boundary_status": "independence_not_verified"}
                         for key in included],
        "generation_source_ids": [] if missing_centers else included,
        "generation_allowed": not missing_centers, "local_available_ids": local_ids,
        "next_local_read_ids": sorted(set(deferred) | scope_deferred, key=sort_key),
        "local_corpus_hash": stable_hash(corpus), "local_corpus_manifest": corpus,
        "associations": associations, "context_states": states,
        "page_coverage": [{"page_no": page,
                           "status": "text_sources_present_page_completeness_not_verified",
                           "local_source_ids": [key for key in local_ids if local[key]["page_no"] == page],
                           "included_source_ids": [key for key in included if local[key]["page_no"] == page]}
                          for page in sorted(available_pages & requested_pages
                                             | {local[key]["page_no"] for key in included})],
        "closure": {"status": "needs_context" if gaps else "bounded_context_collected",
                    "scope": "registered_program_context_only", "gaps": gaps,
                    "document_complete": False, "semantic_support_verified": False,
                    "independent_disclosure_verified": False},
        "policy": {"adjacent_pages": adjacent_pages, "max_sources": max_sources,
                   "max_characters": max_characters, "required_context": required_context,
                   "source_truncation_allowed": False, "whole_document_expansion_allowed": False,
                   "model_support_flags_allowed": False},
        "provenance": {"builder": CONTEXT_VERSION, "model_called": False,
                       "source_texts_rewritten": False, "included_character_count": characters},
        # Replay uses only these declared links, after validating them anew.
        "link_specifications": [{key: copy.deepcopy(value) for key, value in item.items()
                                 if key not in {"id", "semantic_support_verified"}}
                                for item in relevant_links],
    }
    package["package_hash"] = stable_hash(package)
    return package


def rebuild_evidence_context(
    package: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]] | None = None,
) -> dict:
    """Recompute provenance, gaps and closure from the frozen inputs."""
    if not isinstance(package, Mapping) or package.get("version") != CONTEXT_VERSION:
        _fail("VERSION_INVALID")
    policy = package.get("policy")
    if not isinstance(policy, Mapping):
        _fail("POLICY_INVALID")
    if documents is None and package.get("document_snapshot") is not None:
        documents = [package["document_snapshot"]]
    return build_evidence_context(
        sources, package.get("center_source_ids", []), documents=documents,
        explicit_links=package.get("link_specifications", []),
        adjacent_pages=policy.get("adjacent_pages"), max_sources=policy.get("max_sources"),
        max_characters=policy.get("max_characters"), required_context=policy.get("required_context", []),
    )


def validate_evidence_context(
    package: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]] | None = None,
) -> dict:
    """Reject stale/tampered packages; a valid package is not a semantic pass."""
    rebuilt = rebuild_evidence_context(package, sources, documents)
    if dict(package) != rebuilt:
        _fail("REBUILD_MISMATCH")
    return {"status": "valid_frozen_context", "package_hash": rebuilt["package_hash"],
            "closure_status": rebuilt["closure"]["status"], "semantic_support_verified": False,
            "model_called": False}
