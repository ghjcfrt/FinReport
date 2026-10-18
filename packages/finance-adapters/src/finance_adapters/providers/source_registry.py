"""Frozen, document-scoped registry for previously delivered evidence sources.

The registry lets a later workflow round resolve an earlier source reference
after that source has left the current prompt window. A source is returned only
when its filing is in the frozen selection, its document/page/text hashes are
valid, and the delivery record authorizes the same text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

REGISTRY_VERSION = "frozen_source_registry_v1"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_TEXT_CHUNK_ID = re.compile(r"^text-chunk:(\d+)$")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest(raw)


def _records(values: object) -> list[dict[str, Any]]:
    if isinstance(values, Mapping):
        result = []
        for key, value in values.items():
            if not isinstance(value, Mapping):
                continue
            item = dict(value)
            item.setdefault("id", key)
            result.append(item)
        return result
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        return [dict(value) for value in values if isinstance(value, Mapping)]
    return []


def _document_snapshot(
    document_snapshot: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], list[dict]]:
    if not isinstance(document_snapshot, Sequence) or isinstance(document_snapshot, (str, bytes)):
        raise TypeError("SOURCE_REGISTRY_DOCUMENT_SNAPSHOT_INVALID")
    documents: dict[str, str] = {}
    snapshot = []
    for document in document_snapshot:
        if not isinstance(document, Mapping):
            raise TypeError("SOURCE_REGISTRY_DOCUMENT_SNAPSHOT_INVALID")
        filing_id = document.get("filing_version_id")
        document_hash = document.get("source_sha256") or document.get("file_sha256")
        if isinstance(filing_id, bool) or not isinstance(filing_id, int) or filing_id <= 0:
            raise ValueError("SOURCE_REGISTRY_DOCUMENT_ID_INVALID")
        if not isinstance(document_hash, str) or not _SHA256.fullmatch(document_hash):
            raise ValueError("SOURCE_REGISTRY_DOCUMENT_HASH_REQUIRED")
        key = str(filing_id)
        if key in documents and documents[key] != document_hash:
            raise ValueError("SOURCE_REGISTRY_DOCUMENT_SNAPSHOT_CONFLICT")
        documents[key] = document_hash
        snapshot.append({"filing_version_id": filing_id, "source_sha256": document_hash})
    if not documents:
        raise ValueError("SOURCE_REGISTRY_DOCUMENT_SNAPSHOT_EMPTY")
    return documents, sorted(snapshot, key=lambda item: item["filing_version_id"])


def _sent_authorizations(sent_source_hashes: object) -> dict[str, list[dict | None]]:
    """Normalize a source-id hash map or the workflow's delivery ledger."""
    result: dict[str, list[dict | None]] = {}
    if sent_source_hashes is None:
        return result

    def add(source_id: object, value: object, fingerprint: object = None) -> None:
        if not isinstance(source_id, str):
            return
        spec: dict | None
        if isinstance(value, str):
            spec = {"text_sha256": value}
        elif isinstance(value, Mapping):
            spec = dict(value)
        else:
            spec = None
        if isinstance(fingerprint, Mapping):
            merged = dict(spec or {})
            merged.update(fingerprint)
            spec = merged
        result.setdefault(source_id, []).append(spec)

    if isinstance(sent_source_hashes, Mapping):
        if "delivery_ledger" in sent_source_hashes:
            return _sent_authorizations(sent_source_hashes["delivery_ledger"])
        if "source_ids" in sent_source_hashes and "source_hashes" in sent_source_hashes:
            return _sent_authorizations([sent_source_hashes])
        for source_id, value in sent_source_hashes.items():
            add(source_id, value)
        return result

    if not isinstance(sent_source_hashes, Sequence) or isinstance(sent_source_hashes, (str, bytes)):
        raise TypeError("SOURCE_REGISTRY_DELIVERY_LEDGER_INVALID")
    for entry in sent_source_hashes:
        if not isinstance(entry, Mapping):
            raise TypeError("SOURCE_REGISTRY_DELIVERY_LEDGER_INVALID")
        source_ids = entry.get("source_ids")
        source_hashes = entry.get("source_hashes")
        fingerprints = entry.get("source_fingerprints", {})
        if (
            not isinstance(source_ids, list)
            or len(source_ids) != len(set(source_ids))
            or not isinstance(source_hashes, Mapping)
            or set(source_ids) != set(source_hashes)
            or not isinstance(fingerprints, Mapping)
            or not set(fingerprints).issubset(set(source_ids))
        ):
            raise ValueError("SOURCE_REGISTRY_DELIVERY_LEDGER_INVALID")
        for source_id in source_ids:
            add(source_id, source_hashes[source_id], fingerprints.get(source_id))
    return result


def _source_validation(
    source: Mapping[str, Any], documents: Mapping[str, str]
) -> tuple[str, dict | None]:
    source_id = source.get("id")
    chunk_id = source.get("chunk_id")
    filing_id = source.get("filing_version_id")
    match = _TEXT_CHUNK_ID.fullmatch(source_id) if isinstance(source_id, str) else None
    if (
        match is None
        or isinstance(chunk_id, bool)
        or not isinstance(chunk_id, int)
        or int(match.group(1)) != chunk_id
        or isinstance(filing_id, bool)
        or not isinstance(filing_id, int)
        or filing_id <= 0
    ):
        return "not_registered", None
    if str(filing_id) not in documents:
        return "not_selected", {"filing_version_id": filing_id}

    text = source.get("text")
    text_hash = source.get("text_sha256")
    page_hash = source.get("page_sha256")
    document_hash = source.get("source_sha256")
    page_no = source.get("page_no")
    if (
        not isinstance(text, str)
        or not isinstance(text_hash, str)
        or not _SHA256.fullmatch(text_hash)
        or _digest(text) != text_hash
        or not isinstance(page_hash, str)
        or not _SHA256.fullmatch(page_hash)
        or not isinstance(document_hash, str)
        or not _SHA256.fullmatch(document_hash)
        or isinstance(page_no, bool)
        or not isinstance(page_no, int)
        or page_no <= 0
    ):
        return "hash_mismatch", None
    if document_hash != documents[str(filing_id)]:
        return "hash_mismatch", None
    fingerprint = {
        "id": source_id,
        "chunk_id": chunk_id,
        "filing_version_id": filing_id,
        "page_no": page_no,
        "page_sha256": page_hash,
        "text_sha256": text_hash,
        "source_sha256": document_hash,
    }
    return "registered", {"fingerprint": fingerprint, "source": deepcopy(dict(source))}


def _authorization_matches(spec: dict | None, fingerprint: Mapping[str, Any]) -> bool:
    if not isinstance(spec, Mapping):
        return False
    for key in ("text_sha256", "page_sha256", "source_sha256"):
        value = spec.get(key)
        if value is not None and value != fingerprint[key]:
            return False
    for key in ("filing_version_id", "page_no", "chunk_id", "id"):
        value = spec.get(key)
        if value is not None and value != fingerprint[key]:
            return False
    return spec.get("text_sha256") == fingerprint["text_sha256"]


def build_source_registry(
    sources: object,
    document_snapshot: Sequence[Mapping[str, Any]],
    sent_source_hashes: object = None,
) -> dict[str, Any]:
    """Build a registry bound to a frozen selected-document snapshot.

    ``sent_source_hashes`` may be a mapping of source IDs to text hashes (the
    legacy ledger shape), a mapping to full fingerprints, or a delivery-ledger
    sequence. New ledger entries can include ``source_fingerprints`` with text,
    page and document hashes plus page/document identity fields.
    """
    documents, snapshot = _document_snapshot(document_snapshot)
    sent = _sent_authorizations(sent_source_hashes)
    inventory: dict[str, dict] = {}
    duplicates: set[str] = set()
    for source in _records(sources):
        source_id = source.get("id")
        if not isinstance(source_id, str):
            continue
        if source_id in inventory:
            duplicates.add(source_id)
            continue
        status, value = _source_validation(source, documents)
        inventory[source_id] = {"status": status, **(value or {})}

    for source_id in duplicates:
        inventory[source_id] = {"status": "hash_mismatch"}

    entries: dict[str, dict] = {}
    for source_id, item in inventory.items():
        if item["status"] != "registered":
            entries[source_id] = item
            continue
        fingerprint = item["fingerprint"]
        if source_id not in sent:
            entries[source_id] = {"status": "not_sent", "fingerprint": fingerprint}
            continue
        authorizations = sent[source_id]
        if not authorizations or not all(
            _authorization_matches(spec, fingerprint) for spec in authorizations
        ):
            entries[source_id] = {"status": "hash_mismatch", "fingerprint": fingerprint}
            continue
        entries[source_id] = {
            "status": "authorized",
            "fingerprint": fingerprint,
            "source": item["source"],
        }

    registry = {
        "version": REGISTRY_VERSION,
        "document_snapshot": snapshot,
        "document_snapshot_hash": _json_hash(snapshot),
        "entries": entries,
        "sent_ids": sorted(sent),
    }
    registry["registry_hash"] = _json_hash(registry)
    return registry


def _current_source_map(current_sources: object) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for source in _records(current_sources):
        source_id = source.get("id")
        if isinstance(source_id, str):
            result[source_id] = source
    return result


def _registry_is_intact(registry: Mapping[str, Any]) -> bool:
    if registry.get("version") != REGISTRY_VERSION or not isinstance(
        registry.get("entries"), Mapping
    ):
        return False
    expected = registry.get("registry_hash")
    payload = {key: value for key, value in registry.items() if key != "registry_hash"}
    return (
        isinstance(expected, str)
        and _SHA256.fullmatch(expected) is not None
        and _json_hash(payload) == expected
    )


def resolve_registered_sources(
    registry: Mapping[str, Any],
    ids: Sequence[str] | str,
    current_sources: object = None,
) -> dict[str, Any]:
    """Resolve only sources registered and authorized by an earlier delivery."""
    if not isinstance(registry, Mapping) or not _registry_is_intact(registry):
        raise ValueError("SOURCE_REGISTRY_INTEGRITY_INVALID")
    requested = [ids] if isinstance(ids, str) else list(ids)
    if any(not isinstance(source_id, str) for source_id in requested):
        raise ValueError("SOURCE_REGISTRY_SOURCE_IDS_INVALID")
    requested = list(dict.fromkeys(requested))
    current = _current_source_map(current_sources)
    entries = registry["entries"]
    sent_ids = set(registry.get("sent_ids", []))
    resolved: list[dict] = []
    results: list[dict] = []
    errors: list[dict] = []
    for source_id in requested:
        item = entries.get(source_id)
        status = "unknown_id"
        source = None
        if item is None:
            status = "unavailable" if source_id in sent_ids else "unknown_id"
        else:
            stored_status = item.get("status")
            if stored_status != "authorized":
                status = stored_status
            else:
                source = item.get("source")
                fingerprint = item.get("fingerprint", {})
                if not isinstance(source, Mapping) or not _authorization_matches(
                    fingerprint, fingerprint
                ):
                    status = "hash_mismatch"
                    source = None
                elif source_id in current:
                    current_source = current[source_id]
                    validation, current_item = _source_validation(
                        current_source,
                        {
                            str(doc["filing_version_id"]): doc["source_sha256"]
                            for doc in registry["document_snapshot"]
                        },
                    )
                    current_fingerprint = (current_item or {}).get("fingerprint")
                    if validation != "registered" or current_fingerprint != fingerprint:
                        status = "changed"
                        source = None
                    else:
                        status = "resolved"
                else:
                    status = "resolved"
        result = {"source_id": source_id, "status": status}
        if status == "resolved" and source is not None:
            result["source"] = deepcopy(dict(source))
            resolved.append(deepcopy(dict(source)))
        else:
            errors.append(result)
        results.append(result)
    return {"resolved": resolved, "results": results, "errors": errors}
