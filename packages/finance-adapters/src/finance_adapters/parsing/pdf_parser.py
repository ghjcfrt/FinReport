from __future__ import annotations

import hashlib
import time
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

import fitz
from finance_domain.filings.amounts import normalize_monetary_value
from finance_domain.filings.concepts import CONCEPT_LABELS
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.parsing.annual_disclosures import document_annual_disclosures
from finance_adapters.parsing.report_measures import selected_pages, validate_report_period
from finance_adapters.parsing.statement_extractor import (
    StatementExtractor,
    declared_currency,
    ocr_table_candidates,
    parse_number,
)
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.extraction_lifecycle import (
    ExtractionHeartbeat,
    claim_extraction,
    transition_extraction,
)
from finance_adapters.persistence.models import (
    EvidenceFragment,
    ExtractionJob,
    FactDimension,
    FactReview,
    FilingVersion,
    FinancialFact,
    OcrCall,
    TableCandidateRecord,
    ValidationResult,
)
from finance_adapters.providers.model_clients import ModelClient, ModelConfig
from finance_adapters.security.file_gate import scanned_object_path
from finance_adapters.storage.object_storage import file_sha256

PARSER_VERSION = "pymupdf-statements-v5"
TABLE_LAYOUT_VERSION = "pymupdf-statement-layout-v1"
OCR_PARSER_VERSION = "ocr-table-review-v3-unlimited-ocr"

# Extraction is deliberately conservative. Unknown labels remain candidates and cannot enter strict calculations.
CONCEPTS = CONCEPT_LABELS


def _parse_number(value: str) -> Decimal | None:
    return parse_number(value)


def _statement_for_page(text: str) -> str:
    if "资产负债表" in text:
        return "资产负债表"
    if "现金流量表" in text:
        return "现金流量表"
    if "利润表" in text:
        return "利润表"
    return "unclassified"


def _clear_job_candidates(session: Session, filing_id: int, job_id: int) -> None:
    filing_job_ids = list(
        session.scalars(
            select(ExtractionJob.id).where(ExtractionJob.filing_version_id == filing_id)
        )
    )
    candidates = list(
        session.scalars(
            select(FinancialFact).where(
                FinancialFact.filing_version_id == filing_id,
                FinancialFact.quality_status == "candidate",
            )
        )
    )
    for fact in candidates:
        evidence = list(
            session.scalars(select(EvidenceFragment).where(EvidenceFragment.fact_id == fact.id))
        )
        reviewed = session.scalar(select(FactReview.id).where(FactReview.fact_id == fact.id))
        legacy = bool(evidence) and all(
            item.extractor == "pymupdf-text-v1"
            and (item.bounding_box or {}).get("extraction_job_id") is None
            for item in evidence
        )
        if legacy and reviewed is None and filing_job_ids != [job_id]:
            raise ValueError("LEGACY_CANDIDATES_REQUIRE_REVIEW")
        belongs_to_job = (
            legacy
            and filing_job_ids == [job_id]
            or bool(evidence)
            and all(
                (item.bounding_box or {}).get("extraction_job_id") == job_id for item in evidence
            )
        )
        if reviewed is not None or not belongs_to_job:
            continue
        session.query(FactDimension).filter(FactDimension.fact_id == fact.id).delete()
        for item in evidence:
            session.delete(item)
        session.delete(fact)
    session.query(TableCandidateRecord).filter(
        TableCandidateRecord.extraction_job_id == job_id
    ).delete()
    session.flush()


def _audit_opinion(page_texts: list[str], target_year: int) -> tuple[str, int | None]:
    import re

    for page_no, text in enumerate(page_texts, 1):
        compact_text = re.sub(r"\s+", "", text)
        if not all(
            value in compact_text
            for value in ("审计报告", "我们审计了", "合并", str(target_year), "审计意见类型")
        ):
            continue
        match = re.search(r"审计意见类型(.{1,45}?)(?:审计报告签署日期|审计机构名称)", compact_text)
        if match:
            return match[1], page_no
    return "UNCONFIRMED", None


def _run_ocr(
    session: Session,
    job: ExtractionJob,
    filing: FilingVersion,
    page,
    page_no: int,
    client: ModelClient,
) -> tuple[str, int]:
    page_png = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
    page_hash = hashlib.sha256(page_png).hexdigest()
    call = OcrCall(
        extraction_job_id=job.id,
        provider=settings.ocr_provider,
        model=settings.ocr_model,
        page_no=page_no,
        page_sha256=page_hash,
        status="running",
    )
    session.add(call)
    session.commit()
    if len(page_png) > settings.ocr_max_page_bytes:
        call.status, call.error_detail = "failed", "OCR_PAGE_TOO_LARGE"
        session.commit()
        raise ValueError("OCR_PAGE_TOO_LARGE")
    started = time.perf_counter()
    last_error = None
    text = ""
    expected_owner = job.owner_token
    for attempt in range(settings.ocr_max_retries + 1):
        session.refresh(job)
        if job.status != "running" or job.owner_token != expected_owner:
            raise ValueError("EXTRACTION_NOT_RUNNING")
        try:
            text, trace = client.vision(
                system="Return the page faithfully; preserve table cells, headings and units.",
                prompt=settings.ocr_prompt,
                image=page_png,
            )
            if not text.strip():
                raise ValueError("OCR_EMPTY_RESPONSE")
            call.status, call.response_id = "completed", trace.get("id")
            call.response_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            call.response_text = text
            call.error_detail = None
            record_audit_event(
                session, filing.id, category="tool", action="ocr_provider_attempt", status="completed",
                source_ref=f"page:{page_no}", tool_name=settings.ocr_provider,
                tool_version=OCR_PARSER_VERSION, input_hash=page_hash,
                output_hash=trace.get("response_sha256"),
                detail={"ocr_call_id": call.id, "page_no": page_no, "attempt": attempt + 1,
                        "provider_metadata": trace},
            )
            break
        except Exception as exc:  # noqa: BLE001 - persist bounded provider retries
            last_error = exc
            call.status = "retrying" if attempt < settings.ocr_max_retries else "failed"
            call.error_detail = str(exc)
            job.retry_count += 1
            trace = getattr(exc, "metadata", {})
            record_audit_event(
                session, filing.id, category="tool", action="ocr_provider_attempt", status="failed",
                source_ref=f"page:{page_no}", tool_name=settings.ocr_provider,
                tool_version=OCR_PARSER_VERSION, input_hash=page_hash,
                output_hash=trace.get("response_sha256"),
                detail={"ocr_call_id": call.id, "page_no": page_no, "attempt": attempt + 1,
                        "error_code": str(exc), "provider_metadata": trace},
            )
            call.duration_ms = int((time.perf_counter() - started) * 1000)
            session.commit()
    if call.status == "failed":
        raise last_error or ValueError("OCR_FAILED")
    call.duration_ms = int((time.perf_counter() - started) * 1000)
    tables = [
        rows for rows in ocr_table_candidates(text)
        if len(rows) >= 2 and len(rows[0]) >= 2
        and any(len(row) >= 2 and any(cell.strip() for cell in row) for row in rows[1:])
    ]
    for rows in tables:
        session.add(
            TableCandidateRecord(
                extraction_job_id=job.id,
                ocr_call_id=call.id,
                page_no=page_no,
                rows=rows,
                extractor="ocr-table-review-v1",
                confidence=Decimal("0.4000"),
                review_required=True,
            )
        )
    record_audit_event(
        session,
        filing.id,
        category="tool",
        action="ocr_page_processed",
        status="completed",
        source_ref=f"page:{page_no}",
        tool_name=settings.ocr_provider,
        tool_version=OCR_PARSER_VERSION,
        input_hash=page_hash,
        output_hash=call.response_hash,
        detail={
            "ocr_call_id": call.id,
            "page_no": page_no,
            "duration_ms": call.duration_ms,
            "facts_admitted": False,
            "table_count": len(tables),
            "reason": "OCR_TABLE_REQUIRES_POSITION_AND_SCOPE_REVIEW",
        },
    )
    session.commit()
    return text, len(tables)


def _run_ocr_document(session: Session, job: ExtractionJob, filing: FilingVersion,
                      document, path, owner: str) -> None:
    """Only render pixels; OCR candidates require explicit manual fact mapping."""
    from finance_adapters.providers.model_clients import BaiduUnlimitedOCRAdapter

    if BaiduUnlimitedOCRAdapter.is_retired_ocr(settings.ocr_provider, settings.ocr_model):
        raise ValueError("OCR_LEGACY_PROVIDER_RETIRED_CONFIGURE_UNLIMITED_OCR")
    if not (settings.ocr_base_url and settings.ocr_model):
        raise ValueError("OCR_NOT_CONFIGURED")
    client = ModelClient(ModelConfig(
        settings.ocr_provider, settings.ocr_base_url, settings.ocr_api_key,
        settings.ocr_model,
        timeout=settings.ocr_timeout_seconds,
        protocol=settings.ocr_protocol,
        secret_key=settings.ocr_secret_key,
        poll_interval=settings.ocr_poll_interval_seconds,
    ))
    table_count = 0
    pages_with_tables = []
    page_numbers = selected_pages(document, job.page_numbers)
    for index, page_no in enumerate(page_numbers):
        page = document[page_no - 1]
        session.refresh(job)
        if job.status != "running" or job.owner_token != owner:
            session.rollback()
            return
        _text, count = _run_ocr(session, job, filing, page, page_no, client)
        table_count += count
        if count:
            pages_with_tables.append(page_no)
        if not transition_extraction(session, job.id, owner, progress=max(
            5, int((index + 1) / max(len(page_numbers), 1) * 90),
        )):
            session.rollback()
            return
        session.commit()
    session.refresh(job)
    if job.status != "running" or job.owner_token != owner:
        session.rollback()
        return
    if not table_count:
        raise ValueError("OCR_TABLES_NOT_EXTRACTED")
    session.query(ValidationResult).filter(
        ValidationResult.filing_version_id == filing.id,
        ValidationResult.rule_code.in_([
            "source_sha256", "primary_statement_extraction", "audit_opinion", "extraction_failure",
        ]),
    ).delete()
    actual_sha = file_sha256(path)
    session.add_all([
        ValidationResult(
            filing_version_id=filing.id, rule_code="source_sha256", severity="fatal",
            status="passed" if actual_sha == filing.file_sha256 else "failed",
            actual_value=actual_sha, expected_value=filing.file_sha256,
        ),
        ValidationResult(
            filing_version_id=filing.id, rule_code="primary_statement_extraction", severity="fatal",
            status="failed", actual_value="OCR_FACT_MAPPING_REQUIRED",
            expected_value="manually_mapped_confirmed_columns_units_scope_and_rows",
        ),
        ValidationResult(
            filing_version_id=filing.id, rule_code="audit_opinion", severity="fatal",
            status="failed", actual_value="UNCONFIRMED",
            expected_value="standard_unqualified_current_year_consolidated",
        ),
    ])
    if not transition_extraction(
        session, job.id, owner, status="completed", stage="needs_review", progress=100,
        finished_at=datetime.now(UTC), owner_token=None, lease_expires_at=None,
    ):
        session.rollback()
        return
    filing.status = "needs_review"
    record_audit_event(
        session, filing.id, category="tool", action="pdf_extraction_completed", status="completed",
        source_ref=filing.object_key, tool_name=settings.ocr_provider, tool_version=OCR_PARSER_VERSION,
        input_hash=filing.file_sha256,
        output_hash=stable_hash({"job_id": job.id, "table_count": table_count,
                                 "pages": pages_with_tables, "extraction_method": "ocr"}),
        detail={"extraction_job_id": job.id, "extraction_method": "ocr", "fact_count": 0,
                "table_count": table_count, "statement_pages": pages_with_tables,
                "facts_admitted": False, "review_required": True,
                "reason": "OCR_FACT_MAPPING_REQUIRED"},
    )
    session.commit()
    session.refresh(job)


def _run_supplemental_document(session: Session, job: ExtractionJob, filing: FilingVersion,
                               document, owner: str) -> None:
    """Make interim reports searchable without admitting unaudited strict facts."""
    from finance_adapters.providers.evidence_retrieval import materialize_document_text_chunks

    if job.extraction_method == "ocr":
        if not (settings.ocr_base_url and settings.ocr_model):
            raise ValueError("OCR_NOT_CONFIGURED")
        client = ModelClient(ModelConfig(
            settings.ocr_provider, settings.ocr_base_url, settings.ocr_api_key, settings.ocr_model,
            timeout=settings.ocr_timeout_seconds, protocol=settings.ocr_protocol,
            poll_interval=settings.ocr_poll_interval_seconds,
            secret_key=getattr(settings, "ocr_secret_key", ""),
        ))
        page_numbers = selected_pages(document, job.page_numbers)
        for index, page_no in enumerate(page_numbers):
            page = document[page_no - 1]
            session.refresh(job)
            if job.status != "running" or job.owner_token != owner:
                session.rollback()
                return
            _run_ocr(session, job, filing, page, page_no, client)
            if not transition_extraction(session, job.id, owner,
                                         progress=int((index + 1) / max(len(page_numbers), 1) * 90)):
                session.rollback()
                return
            session.commit()
    # Docling conversion may take minutes. Do it before flushing monetary
    # candidates so SQLite's writer lock stays free for the lease heartbeat.
    # Both candidate sets still commit atomically with fenced completion below.
    result = materialize_document_text_chunks(session, filing.id)
    if not result.get("chunk_count"):
        raise ValueError("SUPPLEMENT_TEXT_NOT_EXTRACTED")
    if job.extraction_method == "pdf_text":
        from finance_adapters.persistence.report_measure_gateway import extract_and_persist_measures
        extract_and_persist_measures(session, filing, job, document)
    if not transition_extraction(
        session, job.id, owner, status="completed", stage="needs_review", progress=100,
        finished_at=datetime.now(UTC), owner_token=None, lease_expires_at=None,
    ):
        session.rollback()
        return
    filing.status = "needs_review"
    record_audit_event(
        session, filing.id, category="tool", action="supplement_text_extraction_completed",
        status="completed", source_ref=filing.object_key, tool_name="document_chunk_materializer",
        tool_version="supplement-text-v1", input_hash=filing.file_sha256,
        output_hash=stable_hash(result),
        detail={"extraction_job_id": job.id, "extraction_method": job.extraction_method,
                "report_type": filing.report_type, "chunk_count": result["chunk_count"],
                "page_numbers": job.page_numbers,
                "review_required": True, "facts_admitted": False,
                "limitation": "Report text and located monetary candidates require human review; annual audited metrics use a separate assurance gate."},
    )
    session.commit()
    session.refresh(job)


def run_extraction(session: Session, job_id: int) -> None:
    job = session.get(ExtractionJob, job_id)
    if job is None:
        return
    filing_id = job.filing_version_id
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        return
    owner = claim_extraction(session, job_id)
    if owner is None:
        session.expire_all()
        return
    session.refresh(job)
    heartbeat = ExtractionHeartbeat(session, job_id, owner)
    heartbeat.start()
    document = None
    try:
        interim = filing.period_basis in {"semi_annual", "quarterly"}
        if filing.report_type != filing.period_basis or not (interim or filing.period_basis == "annual"):
            raise ValueError("UNSUPPORTED_STRICT_PERIOD")
        if filing.scope != "consolidated":
            raise ValueError("UNSUPPORTED_STRICT_SCOPE")
        if not interim and (
            filing.period_start.isoformat() != f"{filing.period_end.year}-01-01"
            or filing.period_end.isoformat() != f"{filing.period_end.year}-12-31"
        ):
            raise ValueError("ANNUAL_PERIOD_MISMATCH")
        validate_report_period(filing)
        if job.extraction_method not in {"pdf_text", "ocr"}:
            raise ValueError("UNSUPPORTED_EXTRACTION_METHOD")
        path = scanned_object_path(session, filing)
        parser_version = OCR_PARSER_VERSION if job.extraction_method == "ocr" else PARSER_VERSION
        job.parser_version, job.error_code, job.error_detail = parser_version, None, None
        filing.status = "extracting"
        record_audit_event(
            session,
            filing_id,
            category="tool",
            action="pdf_extraction_started",
            status="running",
            source_ref=filing.object_key,
            tool_name=settings.ocr_provider if job.extraction_method == "ocr" else "pymupdf",
            tool_version=parser_version,
            input_hash=filing.file_sha256,
            detail={"extraction_job_id": job_id, "parser_version": parser_version,
                    "extraction_method": job.extraction_method, "page_numbers": job.page_numbers},
        )
        session.commit()
        document = fitz.open(path)
        if document.is_encrypted:
            raise ValueError("ENCRYPTED_PDF_UNSUPPORTED")
        selected_pages(document, job.page_numbers)
        if interim or job.page_numbers is not None:
            _run_supplemental_document(session, job, filing, document, owner)
            return
        if job.extraction_method == "ocr":
            _run_ocr_document(session, job, filing, document, path, owner)
            return
        from finance_adapters.persistence.report_measure_gateway import extract_and_persist_measures
        extract_and_persist_measures(session, filing, job, document)
        page_texts = [page.get_text("text") for page in document]
        disclosures = document_annual_disclosures(document, page_texts, filing.period_end.year)
        extractor = StatementExtractor(
            CONCEPTS,
            filing.period_end.year,
            declared_currency("\n".join(page_texts)),
            expected_period_end=filing.period_end,
        )
        if not transition_extraction(session, job_id, owner, stage="normalizing"):
            session.rollback()
            return
        session.commit()
        for index, page in enumerate(document):
            session.refresh(job)
            if job.status != "running" or job.owner_token != owner:
                return
            text = page_texts[index]
            if text.strip():
                page_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                extractor.add_page(page, index + 1, page_hash, native_text=text)
            # Long documents retain visible progress without committing each fact.
            if index % 10 == 0:
                if not transition_extraction(session, job_id, owner, progress=max(
                    5, int((index + 1) / max(len(document), 1) * 80),
                )):
                    session.rollback()
                    return
                session.commit()
        session.refresh(job)
        if job.status != "running" or job.owner_token != owner:
            session.rollback()
            return
        if not extractor.facts:
            raise ValueError(
                "OCR_REQUIRED_FOR_SCANNED_DOCUMENT"
                if not any(text.strip() for text in page_texts)
                else "PRIMARY_STATEMENTS_NOT_EXTRACTED"
            )
        # Candidate cleanup and replacement are atomic with the fenced completion.
        _clear_job_candidates(session, filing_id, job_id)
        existing_facts = list(
            session.scalars(
                select(FinancialFact).where(FinancialFact.filing_version_id == filing_id)
            )
        )
        opinion, opinion_page = _audit_opinion(page_texts, filing.period_end.year)
        audited = opinion in {"标准的无保留意见", "标准无保留意见"}
        for candidate in extractor.facts.values():
            previous = [
                fact for fact in existing_facts if fact.concept_code == candidate.concept_code
                and fact.period_start == filing.period_start and fact.period_end == filing.period_end
                and not fact.comparative_period
            ]
            protected = [
                fact
                for fact in previous
                if fact.quality_status == "facts_verified"
                or (
                    fact.review_status != "superseded"
                    and session.scalar(select(FactReview.id).where(FactReview.fact_id == fact.id))
                    is not None
                )
            ]
            if protected:
                continue
            # Switching extraction methods creates a new job. Keep the previous
            # unreviewed candidates and their evidence as history, not as a
            # second live version of the same fact.
            for previous_fact in previous:
                if (previous_fact.quality_status == "candidate"
                        and previous_fact.review_status == "needs_review"
                        and session.scalar(select(FactReview.id).where(
                            FactReview.fact_id == previous_fact.id,
                        )) is None):
                    previous_fact.quality_status = previous_fact.review_status = "superseded"
            fact = FinancialFact(
                filing_version_id=filing_id,
                concept_code=candidate.concept_code,
                concept_name=candidate.concept_name,
                statement_type=candidate.statement_type,
                period_start=filing.period_start,
                period_end=filing.period_end,
                value=normalize_monetary_value(candidate.value, candidate.unit_scale),
                currency=candidate.currency,
                unit_scale=candidate.unit_scale,
                flow_or_stock=candidate.flow_or_stock,
                raw_value=candidate.raw_value,
                quality_status="candidate",
                review_status="needs_review",
                revision_no=max((item.revision_no for item in previous), default=0) + 1,
                assurance_level="audited_annual"
                if audited and filing.period_basis == "annual"
                else "unknown",
            )
            session.add(fact)
            session.flush()
            session.add(
                EvidenceFragment(
                    fact_id=fact.id,
                    page_no=candidate.page_no,
                    table_name=candidate.table_name,
                    row_locator=candidate.raw_label,
                    column_locator=candidate.column_label,
                    raw_label=candidate.raw_label,
                    raw_value=candidate.raw_value,
                    bounding_box={
                        "source": "text-layer",
                        "page": candidate.page_no,
                        "row": list(candidate.row_bbox),
                        "value": list(candidate.value_bbox),
                        "extraction_job_id": job_id,
                        "scope": "consolidated",
                        "unit_scale": candidate.unit_scale,
                        "unit_page_no": candidate.unit_page_no,
                        "unit_text": candidate.unit_text,
                        "value_basis": "normalized_currency",
                        "label_layout": asdict(candidate.label_layout)
                        if candidate.label_layout is not None
                        else None,
                    },
                    page_sha256=candidate.page_sha256,
                    confidence=Decimal("0.9000"),
                    extractor=PARSER_VERSION,
                )
            )
            session.add(
                FactDimension(
                    fact_id=fact.id, dimension_name="scope", dimension_value="consolidated"
                )
            )
            session.add(
                FactDimension(
                    fact_id=fact.id,
                    dimension_name="amount_basis",
                    dimension_value="normalized_currency",
                )
            )
        from finance_adapters.parsing.comparative import persist_comparative_candidates

        comparative_count = persist_comparative_candidates(
            session, filing, job_id, extractor, audited=audited,
        )
        from finance_adapters.persistence.disclosure_gateway import persist_annual_disclosures
        persist_annual_disclosures(session, filing, job_id, disclosures)
        for page_no in extractor.statement_pages:
            table = extractor.tables.get(page_no)
            if table is None:
                continue
            session.add(
                TableCandidateRecord(
                    extraction_job_id=job_id,
                    page_no=page_no,
                    rows=[
                        {
                            "kind": "header",
                            "cells": list(table.headers),
                            "column_roles": list(table.column_roles),
                            "table_name": table.table_name,
                            "statement_type": table.statement_type,
                            "table_bbox": list(table.table_bbox),
                            "page_sha256": table.page_sha256,
                        },
                        *[{"kind": "body", **asdict(row)} for row in table.rows],
                    ],
                    extractor=TABLE_LAYOUT_VERSION,
                    confidence=Decimal("0.9000"),
                    review_required=True,
                )
            )
        session.query(ValidationResult).filter(
            ValidationResult.filing_version_id == filing_id,
            ValidationResult.rule_code.in_(
                [
                    "source_sha256",
                    "primary_statement_extraction",
                    "audit_opinion",
                    "extraction_failure",
                ]
            ),
        ).delete()
        actual_sha = file_sha256(path)
        session.add(
            ValidationResult(
                filing_version_id=filing_id,
                rule_code="source_sha256",
                severity="fatal",
                status="passed" if actual_sha == filing.file_sha256 else "failed",
                actual_value=actual_sha,
                expected_value=filing.file_sha256,
            )
        )
        session.add(
            ValidationResult(
                filing_version_id=filing_id,
                rule_code="audit_opinion",
                severity="fatal",
                status="passed" if opinion in {"标准的无保留意见", "标准无保留意见"} else "failed",
                actual_value=f"{opinion};page:{opinion_page or 'unknown'}",
                expected_value="standard_unqualified_current_year_consolidated",
            )
        )
        if extractor.issues:
            session.add(
                ValidationResult(
                    filing_version_id=filing_id,
                    rule_code="primary_statement_extraction",
                    severity="fatal",
                    status="failed",
                    actual_value=";".join(extractor.issues)[:256],
                    expected_value="confirmed_columns_units_and_unambiguous_primary_rows",
                )
            )
        if not transition_extraction(
            session, job_id, owner, status="completed", stage="needs_review", progress=100,
            finished_at=datetime.now(UTC), owner_token=None, lease_expires_at=None,
        ):
            session.rollback()
            return
        filing.status = "needs_review"
        record_audit_event(
            session,
            filing_id,
            category="tool",
            action="pdf_extraction_completed",
            status="completed",
            source_ref=filing.object_key,
            tool_name="pymupdf",
            tool_version=PARSER_VERSION,
            input_hash=filing.file_sha256,
            output_hash=stable_hash({"current_fact_count": len(extractor.facts),
                                     "comparative_fact_count": comparative_count, "job_id": job_id}),
            detail={
                "extraction_job_id": job_id,
                "extraction_method": job.extraction_method,
                "fact_count": len(extractor.facts),
                "comparative_fact_count": comparative_count,
                "statement_pages": extractor.statement_pages,
                "issues": extractor.issues,
                "value_basis": "normalized_currency",
                "audit_opinion": opinion,
                "audit_opinion_page": opinion_page,
                "validation": "source_sha256",
            },
        )
        session.commit()
        session.refresh(job)
    except Exception as exc:  # noqa: BLE001 - rollback failed inserts before persisting failure
        detail = str(exc)
        session.rollback()
        job = session.get(ExtractionJob, job_id)
        filing = session.get(FilingVersion, filing_id)
        if job is None or filing is None:
            return
        error_code = "OCR_FAILURE" if "OCR" in detail else "PARSER_FAILURE"
        if not transition_extraction(
            session, job_id, owner, status="failed", stage="failed", error_code=error_code,
            error_detail=detail, retryable=True, finished_at=datetime.now(UTC),
            owner_token=None, lease_expires_at=None,
        ):
            session.rollback()
            session.expire_all()
            return
        filing.status = "failed"
        session.query(ValidationResult).filter(
            ValidationResult.filing_version_id == filing_id,
            ValidationResult.rule_code == "extraction_failure",
        ).delete()
        session.add(
            ValidationResult(
                filing_version_id=filing_id,
                rule_code="extraction_failure",
                severity="fatal",
                status="failed",
                actual_value=detail[:256],
                expected_value="completed_trusted_extraction",
            )
        )
        record_audit_event(
            session,
            filing_id,
            category="tool",
            action="pdf_extraction_failed",
            status="failed",
            source_ref=filing.object_key,
            tool_name=settings.ocr_provider if job.extraction_method == "ocr" else "pymupdf",
            tool_version=job.parser_version,
            input_hash=filing.file_sha256,
            detail={"extraction_job_id": job_id, "error_code": error_code, "error": detail,
                    "extraction_method": job.extraction_method},
        )
        session.commit()
        session.refresh(job)
    finally:
        heartbeat.close()
        if document is not None:
            document.close()
