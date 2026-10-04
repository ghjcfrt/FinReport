from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import fitz
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.parsing.table_parser import extract_table_candidates
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.models import (
    EvidenceFragment,
    ExtractionJob,
    FilingVersion,
    FinancialFact,
    OcrCall,
    TableCandidateRecord,
    ValidationResult,
)
from finance_adapters.providers.model_clients import ModelClient, ModelConfig
from finance_adapters.storage.object_storage import file_sha256

PARSER_VERSION = "pymupdf-text-v1"

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
    "负债合计": ("total_liabilities", "资产负债表", "stock"),
    "所有者权益合计": ("total_equity", "资产负债表", "stock"),
    "归属于母公司所有者权益合计": ("equity_attributable", "资产负债表", "stock"),
    "经营活动产生的现金流量净额": ("cashflow_operations", "现金流量表", "flow"),
    "购建固定资产、无形资产和其他长期资产支付的现金": ("capex_cash", "现金流量表", "flow"),
    "期末现金及现金等价物余额": ("cash_end", "现金流量表", "stock"),
    "期初现金及现金等价物余额": ("cash_begin", "现金流量表", "stock"),
}


def _parse_number(value: str) -> Decimal | None:
    cleaned = value.replace(",", "").replace("，", "").replace(" ", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        result = Decimal(cleaned)
        return -result if negative else result
    except InvalidOperation:
        return None


def _statement_for_page(text: str) -> str:
    if "资产负债表" in text:
        return "资产负债表"
    if "现金流量表" in text:
        return "现金流量表"
    if "利润表" in text:
        return "利润表"
    return "unclassified"


def run_extraction(session: Session, job_id: int) -> None:
    job = session.get(ExtractionJob, job_id)
    if job is None:
        return
    filing = session.get(FilingVersion, job.filing_version_id)
    if filing is None:
        return
    if filing.status != "scan_clean":
        job.status, job.stage, job.error_code = "failed", "failed", "FILE_SCAN_REQUIRED"
        job.error_detail, job.retryable = "File has not passed malware scanning", False
        session.commit()
        return
    job.status, job.stage, job.progress, job.started_at = (
        "running",
        "extracting",
        5,
        datetime.now(UTC),
    )
    filing.status = "extracting"
    record_audit_event(
        session,
        filing.id,
        category="tool",
        action="pdf_extraction_started",
        status="running",
        source_ref=filing.object_key,
        tool_name="pymupdf",
        tool_version=PARSER_VERSION,
        input_hash=filing.file_sha256,
        detail={"extraction_job_id": job.id, "parser_version": PARSER_VERSION},
    )
    session.commit()
    try:
        path = Path(filing.object_key)
        document = fitz.open(path)
        job.stage = "normalizing"
        session.commit()
        total = max(len(document), 1)
        seen: set[str] = set()
        ocr_client = None
        if settings.ocr_base_url and settings.ocr_api_key and settings.ocr_model:
            ocr_client = ModelClient(ModelConfig(settings.ocr_provider, settings.ocr_base_url, settings.ocr_api_key, settings.ocr_model, settings.ocr_timeout_seconds))
        for index, page in enumerate(document):
            session.refresh(job)
            if job.status == "cancelled":
                filing.status = "failed"
                job.error_code, job.error_detail = "CANCELLED", "Cancelled by user"
                session.commit()
                return
            text = page.get_text("text")
            for candidate in extract_table_candidates(path, index + 1) if text.strip() else []:
                session.add(TableCandidateRecord(extraction_job_id=job.id, page_no=candidate.page_no, rows=candidate.rows, extractor=candidate.extractor, confidence=Decimal(str(candidate.confidence)), review_required=candidate.review_required))
            if not text.strip() and ocr_client:
                page_png = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
                started = time.perf_counter()
                page_hash = hashlib.sha256(page_png).hexdigest()
                call = OcrCall(extraction_job_id=job.id, provider=settings.ocr_provider, model=settings.ocr_model, page_no=index + 1, page_sha256=page_hash, status="running")
                session.add(call)
                session.flush()
                if len(page_png) > settings.ocr_max_page_bytes:
                    call.status, call.error_detail = "failed", "OCR_PAGE_TOO_LARGE"
                    session.commit()
                    raise ValueError("OCR_PAGE_TOO_LARGE")
                last_error = None
                for attempt in range(settings.ocr_max_retries + 1):
                    try:
                        text, trace = ocr_client.vision(system="Return the page text faithfully. Do not infer or calculate values.", prompt="OCR this financial-report page and return plain text with table labels and values.", image=page_png)
                        if not text.strip():
                            raise ValueError("OCR_EMPTY_RESPONSE")
                        call.status = "completed"
                        call.response_id = trace.get("id")
                        call.response_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        break
                    except Exception as exc:  # noqa: BLE001 - provider errors are persisted and retried
                        last_error = exc
                        call.status, call.error_detail = "retrying" if attempt < settings.ocr_max_retries else "failed", str(exc)
                        job.retry_count += 1
                        session.commit()
                if call.status == "failed":
                    job.retryable = True
                    raise last_error or ValueError("OCR_FAILED")
                call.duration_ms = int((time.perf_counter() - started) * 1000)
                record_audit_event(
                    session,
                    filing.id,
                    category="tool",
                    action="ocr_page_processed",
                    status="completed",
                    source_ref=f"page:{index + 1}",
                    tool_name="ocr",
                    tool_version=settings.ocr_model,
                    input_hash=call.page_sha256,
                    output_hash=call.response_hash,
                    detail={
                        "ocr_call_id": call.id,
                        "provider": call.provider,
                        "model": call.model,
                        "page_no": call.page_no,
                        "duration_ms": call.duration_ms,
                    },
                )
                session.commit()
            page_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            statement = _statement_for_page(text)
            for label, (code, expected_statement, flow_or_stock) in CONCEPTS.items():
                if label not in text or (
                    statement != "unclassified" and statement != expected_statement
                ):
                    continue
                match = re.search(
                    re.escape(label) + r"[^\n\d\-（(]{0,80}([（(]?[-\d,，]+(?:\.\d+)?[）)]?)", text
                )
                if not match:
                    continue
                value = _parse_number(match.group(1))
                if value is None:
                    continue
                key = f"{code}:{index + 1}"
                if key in seen:
                    continue
                seen.add(key)
                fact = FinancialFact(
                    filing_version_id=filing.id,
                    concept_code=code,
                    concept_name=label,
                    statement_type=expected_statement,
                    period_start=filing.period_start,
                    period_end=filing.period_end,
                    value=value,
                    currency="CNY",
                    unit_scale=1,
                    flow_or_stock=flow_or_stock,
                    raw_value=match.group(1),
                )
                session.add(fact)
                session.flush()
                session.add(
                    EvidenceFragment(
                        fact_id=fact.id,
                        page_no=index + 1,
                        table_name=statement,
                        row_locator=label,
                        column_locator="unconfirmed-current-period",
                        raw_label=label,
                        raw_value=match.group(1),
                        bounding_box={"source": "text-layer", "page": index + 1},
                    page_sha256=page_hash,
                    confidence=Decimal("0.6000"),
                    extractor=PARSER_VERSION,
                )
                )
            job.progress = max(5, int((index + 1) / total * 80))
            session.commit()
        document.close()
        job.stage = "validating"
        filing.status = "needs_review"
        job.status, job.stage, job.progress, job.finished_at = (
            "completed",
            "needs_review",
            100,
            datetime.now(UTC),
        )
        session.add(
            ValidationResult(
                filing_version_id=filing.id,
                rule_code="source_sha256",
                severity="info",
                status="passed" if file_sha256(path) == filing.file_sha256 else "failed",
                actual_value=file_sha256(path),
                expected_value=filing.file_sha256,
            )
        )
        record_audit_event(
            session,
            filing.id,
            category="tool",
            action="pdf_extraction_completed",
            status="completed",
            source_ref=filing.object_key,
            tool_name="pymupdf",
            tool_version=PARSER_VERSION,
            input_hash=filing.file_sha256,
            output_hash=stable_hash({"fact_count": len(seen), "job_id": job.id}),
            detail={"extraction_job_id": job.id, "fact_count": len(seen), "validation": "source_sha256"},
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - every parser failure must be persisted.
        job.status, job.stage, job.error_code = "failed", "failed", "OCR_FAILURE" if "OCR" in str(exc) else "PARSER_FAILURE"
        job.error_detail, job.retryable, job.finished_at = str(exc), True, datetime.now(UTC)
        filing.status = "failed"
        record_audit_event(
            session,
            filing.id,
            category="tool",
            action="pdf_extraction_failed",
            status="failed",
            source_ref=filing.object_key,
            tool_name="pymupdf",
            tool_version=PARSER_VERSION,
            input_hash=filing.file_sha256,
            detail={"extraction_job_id": job.id, "error_code": job.error_code, "error": str(exc)},
        )
        session.commit()
