"""Read bounded experimental PDF context without changing extraction history.

The original retrieval snapshot remains immutable. Supplemental page sources
are separately hashed and are candidates only; local readability never proves
semantic support. An OCR-selected filing never falls back to native PDF text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from copy import deepcopy

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.persistence.audit import stable_hash
from finance_adapters.persistence.models import ExtractionJob, FilingVersion
from finance_adapters.security.file_gate import scanned_object_path

SUPPLEMENTAL_CONTEXT_VERSION = "supplemental-local-pdf-context-v1"
_MAX_PAGES = 64
_MAX_TOTAL_TEXT = 256000


def _invalid(code: str) -> None:
    raise ValueError(f"GROUNDED_LOCAL_CONTEXT_{code}")


def _frozen_document(snapshot: dict, filing: FilingVersion) -> dict:
    if not isinstance(snapshot, dict):
        _invalid("SNAPSHOT_INVALID")
    content = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    if snapshot.get("snapshot_hash") != stable_hash(content):
        _invalid("SNAPSHOT_HASH_MISMATCH")
    documents = snapshot.get("documents")
    if (not isinstance(documents, list) or len(documents) != 1
            or not isinstance(documents[0], dict)
            or documents[0].get("filing_version_id") != filing.id
            or documents[0].get("source_sha256") != filing.file_sha256):
        _invalid("DOCUMENT_MISMATCH")
    document = documents[0]
    if not isinstance(document.get("chunks"), list):
        _invalid("CHUNKS_INVALID")
    seen = set()
    for chunk in document["chunks"]:
        if not isinstance(chunk, Mapping):
            _invalid("CHUNK_INVALID")
        page = chunk.get("page_no")
        source_id = chunk.get("source_id", chunk.get("id"))
        text = chunk.get("text")
        if (type(page) is not int or page < 1 or not isinstance(source_id, str)
                or not source_id or source_id in seen
                or chunk.get("filing_version_id") != filing.id
                or chunk.get("source_sha256") != filing.file_sha256
                or not isinstance(text, str) or not text
                or chunk.get("text_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()):
            _invalid("CHUNK_INTEGRITY_INVALID")
        seen.add(source_id)
    return document


def supplemental_local_context(
    session: Session,
    filing: FilingVersion,
    snapshot: dict,
    requested_pages: Sequence[int],
    adjacent_pages: int = 1,
) -> dict:
    """Freeze only missing selected/adjacent pages from a scanned native PDF.

    ``sources`` can be combined with the caller's original snapshot sources for
    this experiment. The caller must retain both snapshots for later replay.
    No extraction job, text chunk, audit record or database state is modified.
    """
    if (not isinstance(requested_pages, Sequence) or isinstance(requested_pages, (str, bytes))
            or not 1 <= len(requested_pages) <= 12
            or any(type(page) is not int or page < 1 for page in requested_pages)
            or len(set(requested_pages)) != len(requested_pages)):
        _invalid("REQUESTED_PAGES_INVALID")
    if type(adjacent_pages) is not int or not 0 <= adjacent_pages <= 8:
        _invalid("ADJACENT_PAGES_INVALID")
    requested = sorted(requested_pages)
    planned = sorted({page + delta for page in requested
                      for delta in range(-adjacent_pages, adjacent_pages + 1) if page + delta > 0})
    if len(planned) > _MAX_PAGES:
        _invalid("PAGE_LIMIT_EXCEEDED")
    document = _frozen_document(snapshot, filing)
    covered_pages = {source["page_no"] for source in document["chunks"]}
    missing = sorted(set(planned) - covered_pages)
    with session.no_autoflush:
        job = session.scalar(select(ExtractionJob).where(
            ExtractionJob.filing_version_id == filing.id,
        ).order_by(ExtractionJob.id.desc()).limit(1))
    job_snapshot = None if job is None else {
        "id": job.id, "extraction_method": job.extraction_method,
        "parser_version": job.parser_version, "page_numbers": deepcopy(job.page_numbers),
        "status": job.status,
    }
    result = {
        "version": SUPPLEMENTAL_CONTEXT_VERSION, "filing_version_id": filing.id,
        "source_sha256": filing.file_sha256, "document_sha256": filing.file_sha256,
        "base_snapshot_hash": snapshot["snapshot_hash"], "requested_pages": requested,
        "adjacent_pages": adjacent_pages, "planned_page_numbers": planned,
        "already_available_page_numbers": sorted(covered_pages & set(planned)),
        "extraction_job": job_snapshot, "sources": [], "gaps": [],
        "model_called": False, "database_written": False, "semantic_support_verified": False,
        "original_snapshot_modified": False, "document_complete": False,
        "policy": {"maximum_pages": _MAX_PAGES, "maximum_total_text_characters": _MAX_TOTAL_TEXT,
                   "ocr_to_native_fallback_allowed": False, "text_truncation_allowed": False},
    }

    def gap(code: str, page: int, **detail) -> None:
        result["gaps"].append({"code": code, "page_no": page, **detail,
                               "next_action": "inspect_local_document_or_extraction"})

    if not missing:
        result["status"] = "no_supplement_needed"
    elif job is None:
        for page in missing:
            gap("extraction_job_not_found", page)
    elif job.extraction_method == "ocr":
        for page in missing:
            gap("ocr_page_not_available_in_frozen_chunks", page)
    else:
        with session.no_autoflush:
            path = scanned_object_path(session, filing)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != filing.file_sha256:
            _invalid("PDF_HASH_MISMATCH")
        total = 0
        try:
            with pymupdf.open(stream=data, filetype="pdf") as pdf:
                result["original_document_page_count"] = len(pdf)
                if pdf.needs_pass:
                    for number in missing:
                        gap("pdf_password_required", number)
                else:
                    # Selected pages take precedence over neighboring context
                    # when the bounded text budget cannot retain every page.
                    read_order = sorted(missing, key=lambda page: (page not in requested, page))
                    for number in read_order:
                        if number > len(pdf):
                            # A requested page is an input gap; a neighbor beyond
                            # the final page is simply outside the document.
                            if number in requested:
                                gap("requested_page_out_of_range", number)
                            continue
                        page = pdf[number - 1]
                        text = page.get_text("text") or ""
                        if not text.strip():
                            gap("native_page_text_unavailable", number)
                            continue
                        if total + len(text) > _MAX_TOTAL_TEXT:
                            gap("page_deferred_by_text_budget", number)
                            continue
                        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        source_id = f"local-pdf-page:{filing.id}:{number}"
                        result["sources"].append({
                            "id": source_id, "source_id": source_id,
                            "filing_version_id": filing.id, "page_no": number,
                            "page_sha256": digest, "page_sha256_kind": "native_page_text_sha256",
                            "section": next((line for line in text.splitlines() if line.strip()), ""),
                            "text": text, "text_sha256": digest,
                            "source_sha256": filing.file_sha256, "document_sha256": filing.file_sha256,
                            "extraction_method": "pdf_text",
                            "extractor_version": f"pymupdf-text-{pymupdf.VersionBind}",
                            "bounding_box": {"page": [float(value) for value in page.rect]},
                            "review_status": "unreviewed", "backend": "experimental_local_pdf",
                            "line_ids": [source_id], "context_line_ids": [source_id],
                            "semantic_support_verified": False,
                            "provenance": {"method": "bounded_supplemental_native_pdf_read",
                                           "extraction_job_id": job.id, "source_pdf_hash_verified": True,
                                           "source_text_rewritten": False},
                        })
                        total += len(text)
        except (pymupdf.FileDataError, RuntimeError):
            # A parser failure cannot publish a partly successful supplemental
            # snapshot as complete. Keep the original corpus unchanged.
            result["sources"] = []
            result["gaps"] = []
            for page in missing:
                gap("local_pdf_unreadable", page)
        if hashlib.sha256(path.read_bytes()).hexdigest() != filing.file_sha256:
            _invalid("PDF_CHANGED_DURING_READ")
    result["sources"].sort(key=lambda source: source["page_no"])
    result["gaps"].sort(key=lambda item: (item["page_no"], item["code"]))
    available = covered_pages | {source["page_no"] for source in result["sources"]}
    result["available_page_numbers"] = sorted(available & set(planned))
    result["requested_pages_available"] = set(requested) <= available
    result["status"] = result.get("status", "partial_context" if result["gaps"] else "supplemented")
    result["snapshot_hash"] = stable_hash(result)
    return result
