from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from decimal import Decimal

import fitz
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.parsing.statement_extractor import (
    StatementExtractor,
    declared_currency,
    ocr_table_candidates,
    parse_number,
)
from finance_adapters.persistence.audit import record_audit_event, stable_hash
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

PARSER_VERSION = "pymupdf-statements-v2"

# Extraction is deliberately conservative. Unknown labels remain candidates and cannot enter strict calculations.
CONCEPTS = {
    "营业收入": ("revenue", "利润表", "flow"),
    "营业成本": ("operating_cost", "利润表", "flow"),
    "营业利润": ("operating_profit", "利润表", "flow"),
    "净利润": ("net_income", "利润表", "flow"),
    "归属于母公司所有者的净利润": ("net_income_attributable", "利润表", "flow"),
    "归属于上市公司股东的净利润": ("net_income_attributable", "利润表", "flow"),
    "扣除非经常性损益后的净利润": ("net_income_attributable_excl_nonrecurring", "利润表", "flow"),
    "资产总计": ("total_assets", "资产负债表", "stock"),
    "流动资产合计": ("total_current_assets", "资产负债表", "stock"),
    "非流动资产合计": ("total_noncurrent_assets", "资产负债表", "stock"),
    "负债合计": ("total_liabilities", "资产负债表", "stock"),
    "流动负债合计": ("total_current_liabilities", "资产负债表", "stock"),
    "非流动负债合计": ("total_noncurrent_liabilities", "资产负债表", "stock"),
    "所有者权益合计": ("total_equity", "资产负债表", "stock"),
    "股东权益合计": ("total_equity", "资产负债表", "stock"),
    "归属于母公司所有者权益合计": ("equity_attributable", "资产负债表", "stock"),
    "归属于母公司股东权益合计": ("equity_attributable", "资产负债表", "stock"),
    "经营活动产生的现金流量净额": ("cashflow_operations", "现金流量表", "flow"),
    "购建固定资产、无形资产和其他长期资产支付的现金": ("capex_cash", "现金流量表", "flow"),
    "期末现金及现金等价物余额": ("cash_end", "现金流量表", "stock"),
    "年末现金及现金等价物余额": ("cash_end", "现金流量表", "stock"),
    "期初现金及现金等价物余额": ("cash_begin", "现金流量表", "stock"),
    "年初现金及现金等价物余额": ("cash_begin", "现金流量表", "stock"),
    "投资活动产生的现金流量净额": ("cashflow_investing", "现金流量表", "flow"),
    "投资活动使用的现金流量净额": ("cashflow_investing", "现金流量表", "flow"),
    "筹资活动产生的现金流量净额": ("cashflow_financing", "现金流量表", "flow"),
    "筹资活动产生/（使用）的现金流量净额": ("cashflow_financing", "现金流量表", "flow"),
    "筹资活动使用的现金流量净额": ("cashflow_financing", "现金流量表", "flow"),
    "汇率变动对现金及现金等价物的影响": ("cashflow_exchange_effect", "现金流量表", "flow"),
    "现金及现金等价物净增加额": ("cash_net_change", "现金流量表", "flow"),
    "现金及现金等价物净减少额": ("cash_net_change", "现金流量表", "flow"),
}


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
) -> str:
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
    for attempt in range(settings.ocr_max_retries + 1):
        try:
            text, trace = client.vision(
                system="Return the page faithfully; preserve table cells, headings and units.",
                prompt="Recognize this financial-report page. Return text and HTML or Markdown tables without inferring values.",
                image=page_png,
            )
            if not text.strip():
                raise ValueError("OCR_EMPTY_RESPONSE")
            call.status, call.response_id = "completed", trace.get("id")
            call.response_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            break
        except Exception as exc:  # noqa: BLE001 - persist bounded provider retries
            last_error = exc
            call.status = "retrying" if attempt < settings.ocr_max_retries else "failed"
            call.error_detail = str(exc)
            job.retry_count += 1
            session.commit()
    if call.status == "failed":
        raise last_error or ValueError("OCR_FAILED")
    call.duration_ms = int((time.perf_counter() - started) * 1000)
    for rows in ocr_table_candidates(text):
        session.add(
            TableCandidateRecord(
                extraction_job_id=job.id,
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
        tool_name="ocr",
        tool_version=settings.ocr_model,
        input_hash=page_hash,
        output_hash=call.response_hash,
        detail={
            "ocr_call_id": call.id,
            "page_no": page_no,
            "duration_ms": call.duration_ms,
            "facts_admitted": False,
            "reason": "OCR_TABLE_REQUIRES_POSITION_AND_SCOPE_REVIEW",
        },
    )
    session.commit()
    return text


def run_extraction(session: Session, job_id: int) -> None:
    job = session.get(ExtractionJob, job_id)
    if job is None:
        return
    filing_id = job.filing_version_id
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        return
    document = None
    try:
        if filing.period_basis != "annual" or filing.report_type != "annual":
            raise ValueError("UNSUPPORTED_STRICT_PERIOD")
        if filing.scope != "consolidated":
            raise ValueError("UNSUPPORTED_STRICT_SCOPE")
        if (
            filing.period_start.isoformat() != f"{filing.period_end.year}-01-01"
            or filing.period_end.isoformat() != f"{filing.period_end.year}-12-31"
        ):
            raise ValueError("ANNUAL_PERIOD_MISMATCH")
        path = scanned_object_path(session, filing)
        _clear_job_candidates(session, filing_id, job_id)
        job.status, job.stage, job.progress = "running", "extracting", 5
        job.started_at, job.finished_at = datetime.now(UTC), None
        job.parser_version, job.error_code, job.error_detail = PARSER_VERSION, None, None
        filing.status = "extracting"
        record_audit_event(
            session,
            filing_id,
            category="tool",
            action="pdf_extraction_started",
            status="running",
            source_ref=filing.object_key,
            tool_name="pymupdf",
            tool_version=PARSER_VERSION,
            input_hash=filing.file_sha256,
            detail={"extraction_job_id": job_id, "parser_version": PARSER_VERSION},
        )
        session.commit()
        document = fitz.open(path)
        if document.is_encrypted:
            raise ValueError("ENCRYPTED_PDF_UNSUPPORTED")
        page_texts = [page.get_text("text") for page in document]
        extractor = StatementExtractor(
            CONCEPTS,
            filing.period_end.year,
            declared_currency("\n".join(page_texts)),
            expected_period_end=filing.period_end,
        )
        ocr_client = None
        if settings.ocr_base_url and settings.ocr_api_key and settings.ocr_model:
            ocr_client = ModelClient(
                ModelConfig(
                    settings.ocr_provider,
                    settings.ocr_base_url,
                    settings.ocr_api_key,
                    settings.ocr_model,
                    settings.ocr_timeout_seconds,
                )
            )
        ocr_seen = False
        job.stage = "normalizing"
        session.commit()
        for index, page in enumerate(document):
            session.refresh(job)
            if job.status == "cancelled":
                filing.status = "failed"
                job.error_code, job.error_detail = "CANCELLED", "Cancelled by user"
                session.commit()
                return
            text = page_texts[index]
            if text.strip():
                page_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                extractor.add_page(page, index + 1, page_hash)
            elif ocr_client:
                ocr_text = _run_ocr(session, job, filing, page, index + 1, ocr_client)
                if ocr_table_candidates(ocr_text):
                    extractor.issues.append(f"page:{index + 1}:OCR_POSITION_AND_SCOPE_UNCONFIRMED")
                ocr_seen = True
            job.progress = max(5, int((index + 1) / max(len(document), 1) * 80))
            # Long documents retain visible progress without committing each fact.
            if index % 10 == 0:
                session.commit()
        if not extractor.facts:
            raise ValueError(
                "OCR_MANUAL_VERIFICATION_REQUIRED"
                if ocr_seen
                else "OCR_REQUIRED_FOR_SCANNED_DOCUMENT"
                if not any(text.strip() for text in page_texts)
                else "PRIMARY_STATEMENTS_NOT_EXTRACTED"
            )
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
            fact = FinancialFact(
                filing_version_id=filing_id,
                concept_code=candidate.concept_code,
                concept_name=candidate.concept_name,
                statement_type=candidate.statement_type,
                period_start=filing.period_start,
                period_end=filing.period_end,
                value=candidate.value * candidate.unit_scale,
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
        for page_no in extractor.statement_pages:
            session.add(
                TableCandidateRecord(
                    extraction_job_id=job_id,
                    page_no=page_no,
                    rows=[
                        [item.raw_label, item.raw_value, item.column_label]
                        for item in extractor.facts.values()
                        if item.page_no == page_no
                    ],
                    extractor=PARSER_VERSION,
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
        job.status, job.stage, job.progress, job.finished_at = (
            "completed",
            "needs_review",
            100,
            datetime.now(UTC),
        )
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
            output_hash=stable_hash({"fact_count": len(extractor.facts), "job_id": job_id}),
            detail={
                "extraction_job_id": job_id,
                "fact_count": len(extractor.facts),
                "statement_pages": extractor.statement_pages,
                "issues": extractor.issues,
                "value_basis": "normalized_currency",
                "audit_opinion": opinion,
                "audit_opinion_page": opinion_page,
                "validation": "source_sha256",
            },
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - rollback failed inserts before persisting failure
        detail = str(exc)
        session.rollback()
        job = session.get(ExtractionJob, job_id)
        filing = session.get(FilingVersion, filing_id)
        if job is None or filing is None:
            return
        job.status, job.stage = "failed", "failed"
        job.error_code = "OCR_FAILURE" if "OCR" in detail else "PARSER_FAILURE"
        job.error_detail, job.retryable, job.finished_at = detail, True, datetime.now(UTC)
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
            tool_name="pymupdf",
            tool_version=PARSER_VERSION,
            input_hash=filing.file_sha256,
            detail={"extraction_job_id": job_id, "error_code": job.error_code, "error": detail},
        )
        session.commit()
    finally:
        if document is not None:
            document.close()
