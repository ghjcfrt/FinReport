"""Located human mappings for OCR. All coordinates refer to rendered pixels."""
from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, date, datetime
from decimal import Decimal

import fitz
from finance_domain.analysis.contracts import (
    OcrAuditConfirmationRequest,
    OcrFactMappingRequest,
    OcrMappingCompletionRequest,
)
from finance_domain.filings.amounts import normalize_monetary_value
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from finance_adapters.parsing.statement_extractor import declared_currency, parse_number
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.models import (
    AnalysisRun,
    AuditEvent,
    EvidenceFragment,
    ExtractionJob,
    FactDimension,
    FilingVersion,
    FinancialFact,
    OcrCall,
    TableCandidateRecord,
    ValidationResult,
)
from finance_adapters.security.file_gate import scanned_object_path

MAPPING_VERSION = "ocr-human-image-mapping-v1"
REQUIRED_PRIMARY_CONCEPTS = {
    "total_assets", "total_liabilities", "total_equity", "revenue", "operating_cost",
    "operating_profit", "net_income", "net_income_attributable", "cash_begin", "cash_end",
    "cashflow_operations", "cashflow_investing", "cashflow_financing", "cashflow_exchange_effect",
}
UNIT_WORDS = {1: "元", 1000: "千元", 10000: "万元", 1000000: "百万元", 100000000: "亿元"}


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _concepts() -> dict[str, tuple[str, str, str]]:
    from finance_domain.filings.concepts import CONCEPT_LABELS

    return {code: (label, statement, flow) for label, (code, statement, flow) in CONCEPT_LABELS.items()}


def latest_ocr_job(session: Session, filing: FilingVersion, *, required: bool = True):
    job = session.scalar(select(ExtractionJob).where(
        ExtractionJob.filing_version_id == filing.id,
    ).order_by(ExtractionJob.id.desc()).limit(1))
    if job is None or job.extraction_method != "ocr":
        if required:
            raise ValueError("CURRENT_OCR_JOB_REQUIRED")
        return None
    if job.status != "completed" or job.started_at is None:
        raise ValueError("CURRENT_OCR_JOB_NOT_COMPLETED")
    return job


def current_ocr_calls(session: Session, job: ExtractionJob) -> dict[int, OcrCall]:
    latest = {}
    for call in session.scalars(select(OcrCall).where(
        OcrCall.extraction_job_id == job.id, OcrCall.started_at >= job.started_at,
    ).order_by(OcrCall.id)):
        latest[call.page_no] = call
    return {call.id: call for call in latest.values() if call.status == "completed"}


def _source_call(session: Session, job: ExtractionJob, call_id: int,
                 page_hash: str, response_hash: str) -> OcrCall:
    call = current_ocr_calls(session, job).get(call_id)
    if call is None:
        raise ValueError("OCR_CALL_STALE_OR_NOT_COMPLETED")
    actual = hashlib.sha256((call.response_text or "").encode("utf-8")).hexdigest()
    if not call.response_text or call.response_hash != actual or response_hash != actual:
        raise ValueError("OCR_RESPONSE_HASH_MISMATCH")
    if page_hash != call.page_sha256:
        raise ValueError("OCR_PAGE_HASH_MISMATCH")
    return call


def render_ocr_page(session: Session, filing: FilingVersion,
                    call: OcrCall) -> tuple[bytes, int, int]:
    """Re-render the scanned object with the exact OCR settings; never read text."""
    path = scanned_object_path(session, filing)
    with fitz.open(path) as document:
        if not 1 <= call.page_no <= len(document):
            raise ValueError("OCR_PAGE_OUT_OF_RANGE")
        pixmap = document[call.page_no - 1].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        png = pixmap.tobytes("png")
        width, height = pixmap.width, pixmap.height
    if hashlib.sha256(png).hexdigest() != call.page_sha256:
        raise ValueError("OCR_RENDERED_PAGE_HASH_MISMATCH")
    return png, width, height


def _check_box(box, width: int, height: int) -> None:
    if (not isinstance(box, list) or len(box) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in box)
            or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
        raise ValueError("OCR_IMAGE_BOUNDING_BOX_INVALID")


def _check_currency(source: str) -> None:
    compact = _compact(source)
    foreign = r"美元|港元|港币|欧元|日元|英镑|USD|HKD|EUR|JPY|GBP"
    if (declared_currency(source) not in {None, "CNY"}
            or re.search(r"(?:币种[:：]?|单位[:：]?|记账本位币(?:为|采用|[:：])?|"
                         r"列报货币(?:为|采用|[:：])?)(?:" + foreign + r")", compact, re.IGNORECASE)):
        raise ValueError("OCR_CURRENCY_DECLARATION_CONFLICT")


def _normalized_amount(raw_value: str, unit_scale: int) -> Decimal:
    parsed = parse_number(raw_value)
    if parsed is None or not parsed.is_finite():
        raise ValueError("OCR_NUMERIC_VALUE_REQUIRED")
    # Establish exact storage precision before multiplication, including tiny
    # fractional tails that a bounded Decimal context could otherwise round off.
    _, digits, exponent = parsed.as_tuple()
    scaled_exponent = exponent + len(str(unit_scale)) - 1
    excess = max(0, -scaled_exponent - 4)
    if excess and any(digits[-excess:]):
        raise ValueError("OCR_NUMERIC_PRECISION_NOT_SUPPORTED")
    value = normalize_monetary_value(parsed, unit_scale)
    if not value.is_finite() or value.copy_abs() >= Decimal("1e20"):
        raise ValueError("OCR_NUMERIC_VALUE_OUT_OF_RANGE")
    # Quantization would depend on the caller's Decimal precision. The exponent
    # and trailing digits establish storage precision without arithmetic.
    return value


def _check_column_header(header: str, year: int, statement: str) -> None:
    compact = _compact(header)
    if re.search(r"未经审计|未审计|unaudited", compact, re.IGNORECASE):
        raise ValueError("OCR_COLUMN_UNAUDITED")
    compact = re.sub(r"[（(](?:重述|重列|已审计|经审计|审计后)[）)]", "", compact)
    years = set(re.findall(r"20\d{2}", compact))
    if years != {str(year)}:
        raise ValueError("OCR_COLUMN_ANNUAL_PERIOD_REQUIRED")
    year_text = str(year)
    if statement == "资产负债表":
        annual = re.fullmatch(year_text + r"年12月31日", compact)
    else:
        annual = re.fullmatch(year_text + r"年度?", compact)
        annual = annual or re.fullmatch(year_text + r"年1(?:月至|月[—–－\-~～]|[—–－\-~～至])12月", compact)
        annual = annual or re.fullmatch(year_text + r"年1月1日(?:至|[—–－\-~～])(?:" +
                                       year_text + r"年)?12月31日", compact)
    if not annual:
        raise ValueError("OCR_COLUMN_ANNUAL_PERIOD_REQUIRED")


def mapping_context(session: Session, filing: FilingVersion) -> dict:
    job = latest_ocr_job(session, filing)
    pages = []
    for call in current_ocr_calls(session, job).values():
        _source_call(session, job, call.id, call.page_sha256, call.response_hash)
        _, width, height = render_ocr_page(session, filing, call)
        pages.append({"page_no": call.page_no, "ocr_call_id": call.id,
                      "page_sha256": call.page_sha256, "response_sha256": call.response_hash,
                      "source_text": call.response_text, "width": width, "height": height,
                      "image_url": f"/api/v1/filings/{filing.id}/ocr-pages/{call.page_no}.png?ocr_call_id={call.id}"})
    return {"job_id": job.id,
            "filing": {"period_start": filing.period_start, "period_end": filing.period_end,
                       "scope": filing.scope, "accounting_standard": filing.accounting_standard,
                       "file_sha256": filing.file_sha256},
            "concepts": [{"code": code, "name": name, "statement_type": statement,
                          "flow_or_stock": flow} for code, (name, statement, flow) in _concepts().items()],
            "pages": pages,
            "mapped_fact_ids": [fact.id for fact in _active_facts(session, filing)
                                if _mapped_evidence(session, fact, job)]}


def _active_facts(session: Session, filing: FilingVersion) -> list[FinancialFact]:
    return list(session.scalars(select(FinancialFact).where(
        FinancialFact.filing_version_id == filing.id,
        FinancialFact.review_status != "superseded", FinancialFact.quality_status != "superseded",
    ).order_by(FinancialFact.id)))


def _mapped_evidence(session: Session, fact: FinancialFact, job: ExtractionJob):
    evidence = list(session.scalars(select(EvidenceFragment).where(EvidenceFragment.fact_id == fact.id)))
    return [item for item in evidence if item.extractor == MAPPING_VERSION
            and (item.bounding_box or {}).get("extraction_job_id") == job.id]


def _invalidate_outputs(session: Session, filing: FilingVersion) -> None:
    filing.status = "needs_review"
    session.execute(update(AnalysisRun).where(AnalysisRun.filing_version_id == filing.id).values(
        status="invalidated", invalidated_at=datetime.now(UTC),
    ))


def map_ocr_fact(session: Session, filing: FilingVersion, payload: OcrFactMappingRequest) -> FinancialFact:
    job = latest_ocr_job(session, filing)
    call = _source_call(session, job, payload.ocr_call_id, payload.page_sha256, payload.response_sha256)
    table = session.get(TableCandidateRecord, payload.table_id)
    if (table is None or table.extraction_job_id != job.id or table.ocr_call_id != call.id
            or table.page_no != call.page_no or table.extractor != "ocr-table-review-v1"):
        raise ValueError("OCR_TABLE_STALE_OR_MISMATCHED")
    rows = table.rows
    if (len(rows) < 2 or payload.row_index + 1 >= len(rows)
            or payload.column_index >= len(rows[0])
            or payload.column_index >= len(rows[payload.row_index + 1])):
        raise ValueError("OCR_TABLE_CELL_OUT_OF_RANGE")
    cells = rows[payload.row_index + 1]
    if (payload.raw_label != cells[0].strip() or payload.raw_value != cells[payload.column_index].strip()
            or payload.column_label != rows[0][payload.column_index].strip()):
        raise ValueError("OCR_TABLE_RAW_CELL_MISMATCH")
    concept = _concepts().get(payload.concept_code)
    if concept is None:
        raise ValueError("OCR_CONCEPT_NOT_SUPPORTED")
    name, statement, flow = concept
    expected_table = "合并" + statement
    compact_source = _compact(call.response_text)
    if (payload.table_name != expected_table or _compact(expected_table) not in compact_source
            or payload.scope != filing.scope or payload.accounting_standard != filing.accounting_standard):
        raise ValueError("OCR_STATEMENT_SCOPE_OR_STANDARD_MISMATCH")
    year = filing.period_end.year - (payload.period_role == "comparative")
    header = _compact(payload.column_label)
    _check_column_header(header, year, statement)
    if (payload.period_start != date(year, 1, 1) or payload.period_end != date(year, 12, 31)
            or str(year) not in header
            or payload.is_restatement != ("重述" in header or "重列" in header)
            or (payload.period_role == "current" and payload.is_restatement)
            or (statement == "资产负债表" and f"{year}年12月31日" not in header)):
        raise ValueError("OCR_COLUMN_PERIOD_OR_RESTATEMENT_MISMATCH")
    unit_call = call
    unit_refs = (payload.unit_ocr_call_id, payload.unit_page_sha256, payload.unit_response_sha256)
    if any(value is not None for value in unit_refs):
        if not all(value is not None for value in unit_refs):
            raise ValueError("OCR_UNIT_SOURCE_REFERENCE_INCOMPLETE")
        unit_call = _source_call(session, job, *unit_refs)
    _check_currency(call.response_text)
    _check_currency(unit_call.response_text)
    unit_text = _compact(payload.unit_text)
    declared = re.search(r"(?:单位[:：]?(?:人民币)?|人民币)(百万元|千元|万元|亿元|元)(?:整)?", unit_text)
    if (unit_text not in _compact(unit_call.response_text) or declared is None
            or declared[1] != UNIT_WORDS[payload.unit_scale]):
        raise ValueError("OCR_DECLARED_UNIT_MISMATCH")
    normalized = _normalized_amount(payload.corrected_raw_value or payload.raw_value, payload.unit_scale)
    _, width, height = render_ocr_page(session, filing, call)
    for box in (payload.value_bbox, payload.row_bbox):
        _check_box(box, width, height)
    _, unit_width, unit_height = render_ocr_page(session, filing, unit_call)
    _check_box(payload.unit_bbox, unit_width, unit_height)
    value, row = payload.value_bbox, payload.row_bbox
    if not (row[0] <= value[0] and row[1] <= value[1] and row[2] >= value[2] and row[3] >= value[3]):
        raise ValueError("OCR_VALUE_OUTSIDE_ROW")
    mapping_digest = stable_hash(payload.model_dump(mode="json"))
    for active in _active_facts(session, filing):
        for item in _mapped_evidence(session, active, job):
            source = item.bounding_box
            if (source.get("table_id") == table.id and source.get("row_index") == payload.row_index
                    and source.get("column_index") == payload.column_index
                    and active.concept_code != payload.concept_code):
                raise ValueError("OCR_CELL_ALREADY_MAPPED_TO_DIFFERENT_CONCEPT")
    previous = list(session.scalars(select(FinancialFact).where(
        FinancialFact.filing_version_id == filing.id,
        FinancialFact.concept_code == payload.concept_code,
        FinancialFact.period_start == payload.period_start, FinancialFact.period_end == payload.period_end,
    )))
    for old in previous:
        evidence = _mapped_evidence(session, old, job)
        if old.review_status != "superseded" and any(
                (item.bounding_box or {}).get("mapping_digest") == mapping_digest for item in evidence):
            return old
    for old in previous:
        old.review_status = old.quality_status = "superseded"
    fact = FinancialFact(
        filing_version_id=filing.id, concept_code=payload.concept_code, concept_name=name,
        statement_type=statement, period_start=payload.period_start, period_end=payload.period_end,
        value=normalized, currency=payload.currency, unit_scale=payload.unit_scale, flow_or_stock=flow,
        raw_value=payload.corrected_raw_value or payload.raw_value, quality_status="candidate",
        review_status="needs_review", assurance_level="unknown",
        comparative_period=payload.period_role == "comparative", is_restatement=payload.is_restatement,
        revision_no=max((old.revision_no for old in previous), default=0) + 1,
    )
    session.add(fact)
    session.flush()
    metadata = {"source": "ocr-human-image", "coordinate_space": "rendered_pixels", "page": call.page_no,
                "value": payload.value_bbox, "row": payload.row_bbox, "unit_bbox": payload.unit_bbox,
                "image_width": width, "image_height": height, "extraction_job_id": job.id,
                "ocr_call_id": call.id, "table_id": table.id, "table_sha256": stable_hash(table.rows),
                "response_sha256": call.response_hash, "scope": payload.scope,
                "unit_scale": payload.unit_scale, "unit_text": payload.unit_text,
                "unit_page_no": unit_call.page_no, "unit_ocr_call_id": unit_call.id,
                "unit_page_sha256": unit_call.page_sha256,
                "unit_response_sha256": unit_call.response_hash, "value_basis": "normalized_currency",
                "period_role": payload.period_role, "column_role": f"year:{year}",
                "comparability_basis": "same_report_consolidated_annual_columns",
                "accounting_standard": payload.accounting_standard, "is_restatement": payload.is_restatement,
                "row_index": payload.row_index, "column_index": payload.column_index,
                "original_ocr_value": payload.raw_value, "mapping_digest": mapping_digest,
                "mapped_by": payload.reviewer_id, "mapping_reason": payload.reason,
                "manually_corrected": payload.corrected_raw_value is not None}
    session.add(EvidenceFragment(
        fact_id=fact.id, page_no=call.page_no, table_name=payload.table_name,
        row_locator=payload.raw_label, column_locator=payload.column_label, raw_label=payload.raw_label,
        raw_value=fact.raw_value, bounding_box=metadata, page_sha256=call.page_sha256,
        confidence=Decimal(1), extractor=MAPPING_VERSION,
    ))
    session.add_all([FactDimension(fact_id=fact.id, dimension_name="scope", dimension_value=payload.scope),
                     FactDimension(fact_id=fact.id, dimension_name="amount_basis",
                                   dimension_value="normalized_currency")])
    record_audit_event(session, filing.id, category="review", action="ocr_fact_mapped", status="candidate",
                       tool_name="human_image_mapping", tool_version=MAPPING_VERSION,
                       source_ref=f"ocr_call:{call.id};table:{table.id};fact:{fact.id}",
                       input_hash=call.page_sha256, output_hash=mapping_digest,
                       detail={"fact_id": fact.id, "payload": payload.model_dump(mode="json"),
                               "normalized_value": str(normalized), "independent_approvals": 0})
    _invalidate_outputs(session, filing)
    session.flush()
    refresh_ocr_mapping_validation(session, filing)
    return fact


def _check_audit_quote(quote: str, source: str, year: int) -> None:
    compact = _compact(quote)
    stripped = _compact(source).replace("无保留意见", "")
    # Match affirmative opinion wording, never a positive substring in its
    # negation. Conservatively leave nonstandard/uncertain opinions outside the
    # strict standard-unqualified path even when their amounts remain useful.
    unsafe = re.search(
        r"非标准|(?:并非|不是|不属于|未(?:能|予)|尚未|并未|不|非).{0,12}(?:标准|公允反映)|"
        r"(?:是否|可能|不确定).{0,12}(?:标准|无保留|公允反映)|强调事项|重大不确定性|"
        r"持续经营.{0,12}(?:不确定|疑虑)|qualified|adverse|disclaimer",
        _compact(source), re.IGNORECASE,
    )
    if (compact not in _compact(source) or str(year) not in compact or "合并" not in compact
            or not any(word in compact for word in ("我们审计了", "审计报告"))
            or not any(word in compact for word in ("标准的无保留意见", "标准无保留意见", "在所有重大方面公允反映"))
            or unsafe or any(word in stripped for word in ("保留意见", "否定意见", "无法表示意见"))):
        raise ValueError("OCR_STANDARD_UNQUALIFIED_AUDIT_QUOTE_REQUIRED")


def confirm_ocr_audit(session: Session, filing: FilingVersion,
                      payload: OcrAuditConfirmationRequest) -> None:
    job = latest_ocr_job(session, filing)
    call = _source_call(session, job, payload.ocr_call_id, payload.page_sha256, payload.response_sha256)
    _, width, height = render_ocr_page(session, filing, call)
    _check_box(payload.bbox, width, height)
    _check_audit_quote(payload.quote, call.response_text, filing.period_end.year)
    detail = {**payload.model_dump(mode="json"), "extraction_job_id": job.id,
              "page_no": call.page_no, "coordinate_space": "rendered_pixels",
              "image_width": width, "image_height": height, "file_sha256": filing.file_sha256}
    digest = stable_hash(detail)
    latest = session.scalar(select(AuditEvent).where(
        AuditEvent.filing_version_id == filing.id, AuditEvent.action == "ocr_audit_opinion_confirmed",
    ).order_by(AuditEvent.id.desc()).limit(1))
    if latest is not None and latest.output_hash == digest and latest.detail == detail:
        refresh_ocr_mapping_validation(session, filing)
        return
    record_audit_event(session, filing.id, category="review", action="ocr_audit_opinion_confirmed",
                       status="confirmed", source_ref=f"ocr_call:{call.id};page:{call.page_no}",
                       tool_name="human_image_mapping", tool_version=MAPPING_VERSION,
                       input_hash=call.page_sha256, output_hash=digest, detail=detail)
    _invalidate_outputs(session, filing)
    session.flush()
    refresh_ocr_mapping_validation(session, filing)


def _validate_audit(session: Session, filing: FilingVersion, job: ExtractionJob,
                    rendered: dict[int, tuple[int, int]]) -> AuditEvent:
    events = list(session.scalars(select(AuditEvent).where(
        AuditEvent.filing_version_id == filing.id, AuditEvent.action == "ocr_audit_opinion_confirmed",
    ).order_by(AuditEvent.id.desc())))
    event = next((item for item in events if item.detail.get("extraction_job_id") == job.id), None)
    if event is None:
        raise ValueError("OCR_AUDIT_CONFIRMATION_REQUIRED")
    data = event.detail
    call = _source_call(session, job, data.get("ocr_call_id"), data.get("page_sha256"), data.get("response_sha256"))
    if (event.tool_version != MAPPING_VERSION or event.output_hash != stable_hash(data)
            or data.get("file_sha256") != filing.file_sha256
            or data.get("opinion") != "standard_unqualified" or not data.get("reviewer_id")
            or not data.get("reason") or event.input_hash != call.page_sha256):
        raise ValueError("OCR_AUDIT_EVIDENCE_STALE")
    _check_audit_quote(data.get("quote", ""), call.response_text, filing.period_end.year)
    if call.id not in rendered:
        _, width, height = render_ocr_page(session, filing, call)
        rendered[call.id] = width, height
    _check_box(data.get("bbox"), *rendered[call.id])
    return event


def _validate_mappings(session: Session, filing: FilingVersion, job: ExtractionJob,
                        rendered: dict[int, tuple[int, int]], *, ignore_legacy: bool = False) -> set[str]:
    codes = set()
    mapping_events = {event.detail.get("fact_id"): event for event in session.scalars(
        select(AuditEvent).where(AuditEvent.filing_version_id == filing.id,
                                AuditEvent.action == "ocr_fact_mapped").order_by(AuditEvent.id))}
    for fact in _active_facts(session, filing):
        evidence = _mapped_evidence(session, fact, job)
        if not evidence and ignore_legacy:
            continue
        if len(evidence) != 1:
            raise ValueError("OCR_LEGACY_FACT_ARCHIVAL_REQUIRED")
        item = evidence[0]
        data = item.bounding_box
        event = mapping_events.get(fact.id)
        if event is None or not isinstance(event.detail.get("payload"), dict):
            raise ValueError("OCR_MAPPING_AUDIT_RECORD_REQUIRED")
        payload = OcrFactMappingRequest.model_validate(event.detail["payload"])
        concept = _concepts().get(payload.concept_code)
        normalized = _normalized_amount(payload.corrected_raw_value or payload.raw_value, payload.unit_scale)
        _check_column_header(payload.column_label, payload.period_end.year, fact.statement_type)
        if (concept is None or event.tool_version != MAPPING_VERSION
                or event.output_hash != stable_hash(event.detail["payload"])
                or data.get("mapping_digest") != event.output_hash
                or event.input_hash != payload.page_sha256
                or fact.concept_code != payload.concept_code or fact.concept_name != concept[0]
                or (fact.statement_type, fact.flow_or_stock) != concept[1:]
                or fact.period_start != payload.period_start or fact.period_end != payload.period_end
                or fact.comparative_period != (payload.period_role == "comparative")
                or fact.is_restatement != payload.is_restatement or fact.currency != payload.currency
                or fact.unit_scale != payload.unit_scale
                or normalized != Decimal(fact.value)
                or str(normalized) != event.detail.get("normalized_value")
                or item.raw_label != payload.raw_label or item.column_locator != payload.column_label
                or item.table_name != payload.table_name or item.page_sha256 != payload.page_sha256
                or data.get("value") != payload.value_bbox or data.get("row") != payload.row_bbox
                or data.get("unit_bbox") != payload.unit_bbox
                or data.get("scope") != payload.scope or data.get("unit_scale") != payload.unit_scale
                or data.get("unit_text") != payload.unit_text
                or data.get("period_role") != payload.period_role
                or data.get("is_restatement") is not payload.is_restatement
                or data.get("accounting_standard") != payload.accounting_standard):
            raise ValueError("OCR_MAPPING_FACT_OR_METADATA_MISMATCH")
        call = _source_call(session, job, data.get("ocr_call_id"), item.page_sha256, data.get("response_sha256"))
        table = session.get(TableCandidateRecord, data.get("table_id"))
        if (table is None or table.ocr_call_id != call.id or table.extraction_job_id != job.id
                or stable_hash(table.rows) != data.get("table_sha256") or item.page_no != call.page_no):
            raise ValueError("OCR_MAPPED_TABLE_EVIDENCE_STALE")
        if call.id not in rendered:
            _, width, height = render_ocr_page(session, filing, call)
            rendered[call.id] = width, height
        for key in ("value", "row"):
            _check_box(data.get(key), *rendered[call.id])
        unit_call = _source_call(session, job, data.get("unit_ocr_call_id"),
                                 data.get("unit_page_sha256"), data.get("unit_response_sha256"))
        if unit_call.id not in rendered:
            _, width, height = render_ocr_page(session, filing, unit_call)
            rendered[unit_call.id] = width, height
        _check_box(data.get("unit_bbox"), *rendered[unit_call.id])
        index, column = data.get("row_index"), data.get("column_index")
        if (not isinstance(index, int) or not isinstance(column, int) or index < 0 or column < 1
                or index + 1 >= len(table.rows) or column >= len(table.rows[index + 1])
                or table.rows[index + 1][0].strip() != item.raw_label
                or table.rows[index + 1][column].strip() != data.get("original_ocr_value")
                or table.rows[0][column].strip() != item.column_locator
                or fact.raw_value != item.raw_value
                or _normalized_amount(fact.raw_value, fact.unit_scale) != Decimal(fact.value)):
            raise ValueError("OCR_MAPPED_RAW_VALUE_MISMATCH")
        if not fact.comparative_period:
            if fact.concept_code in codes:
                raise ValueError("OCR_AMBIGUOUS_CURRENT_MAPPING")
            codes.add(fact.concept_code)
    return codes


def complete_ocr_mapping(session: Session, filing: FilingVersion,
                          payload: OcrMappingCompletionRequest) -> dict:
    """Explicitly retire facts from previous extraction sources, preserving history."""
    job = latest_ocr_job(session, filing)
    if payload.job_id != job.id or payload.file_sha256 != filing.file_sha256:
        raise ValueError("OCR_MAPPING_COMPLETION_SOURCE_STALE")
    rendered = {}
    codes = _validate_mappings(session, filing, job, rendered, ignore_legacy=True)
    if not REQUIRED_PRIMARY_CONCEPTS.issubset(codes):
        raise ValueError("OCR_PRIMARY_FACT_MAPPINGS_INCOMPLETE")
    _validate_audit(session, filing, job, rendered)
    current, legacy = [], []
    for fact in _active_facts(session, filing):
        (current if _mapped_evidence(session, fact, job) else legacy).append(fact)
    current_ids = [fact.id for fact in current]
    events = session.scalars(select(AuditEvent).where(
        AuditEvent.filing_version_id == filing.id, AuditEvent.action == "ocr_mapping_completed",
    ).order_by(AuditEvent.id.desc()))
    latest = next(iter(events), None)
    if (not legacy and latest is not None and latest.detail.get("job_id") == job.id
            and latest.detail.get("current_fact_ids") == current_ids):
        return {"archived_fact_ids": latest.detail["archived_fact_ids"],
                "current_fact_ids": current_ids, "review_required": True}
    archived_ids = [fact.id for fact in legacy]
    detail = {"job_id": job.id, "file_sha256": filing.file_sha256,
              "current_fact_ids": current_ids, "archived_fact_ids": archived_ids,
              "reviewer_id": payload.reviewer_id, "reason": payload.reason,
              "approval_policy": "two_current_approvals_after_mapping_completion"}
    event = record_audit_event(session, filing.id, category="review", action="ocr_mapping_completed",
                              status="needs_review", tool_name="human_image_mapping",
                              tool_version=MAPPING_VERSION, input_hash=filing.file_sha256,
                              output_hash=stable_hash(detail), detail=detail)
    session.flush()
    for fact in legacy:
        fact.quality_status = fact.review_status = "superseded"
    for fact in current:
        # Evidence digest changes; earlier approvals stay visible as history.
        for item in _mapped_evidence(session, fact, job):
            item.bounding_box = {**item.bounding_box, "mapping_completion_event_id": event.id}
        fact.quality_status, fact.review_status = "candidate", "needs_review"
    _invalidate_outputs(session, filing)
    session.flush()
    refresh_ocr_mapping_validation(session, filing)
    return {"archived_fact_ids": archived_ids, "current_fact_ids": current_ids, "review_required": True}


def validate_ocr_source_assurance(session: Session, filing: FilingVersion) -> None:
    job = latest_ocr_job(session, filing, required=False)
    if job is None:
        return
    rendered = {}
    codes = _validate_mappings(session, filing, job, rendered)
    if not REQUIRED_PRIMARY_CONCEPTS.issubset(codes):
        raise ValueError("OCR_PRIMARY_FACT_MAPPINGS_INCOMPLETE")
    event = _validate_audit(session, filing, job, rendered)
    for fact in _active_facts(session, filing):
        for item in _mapped_evidence(session, fact, job):
            data = item.bounding_box
            if data.get("audit_event_id") != event.id or data.get("audit_source_digest") != event.output_hash:
                raise ValueError("OCR_FACT_AUDIT_BINDING_STALE")


def refresh_ocr_mapping_validation(session: Session, filing: FilingVersion) -> None:
    """Refresh only mapping-owned gates; leave all numerical/review gates intact."""
    job = latest_ocr_job(session, filing, required=False)
    if job is None:
        return
    rendered = {}
    mapping_error = None
    try:
        codes = _validate_mappings(session, filing, job, rendered)
        missing = sorted(REQUIRED_PRIMARY_CONCEPTS - codes)
        if missing:
            mapping_error = "MISSING:" + ",".join(missing)
    except ValueError as exc:
        mapping_error = str(exc)
    audit_error = None
    try:
        event = _validate_audit(session, filing, job, rendered)
    except ValueError as exc:
        audit_error = str(exc)
        event = None
    for rule, error, success in (
        ("primary_statement_extraction", mapping_error, f"OCR_CURRENT_MAPPINGS_COMPLETE;job:{job.id}"),
        ("audit_opinion", audit_error, f"standard_unqualified;event:{event.id if event else ''};job:{job.id}"),
    ):
        session.query(ValidationResult).filter(
            ValidationResult.filing_version_id == filing.id, ValidationResult.rule_code == rule,
        ).delete(synchronize_session=False)
        session.add(ValidationResult(
            filing_version_id=filing.id, rule_code=rule, rule_version=MAPPING_VERSION, severity="fatal",
            status="failed" if error else "passed", actual_value=(error or success)[:256],
            expected_value="current_job_located_human_mapping" if rule == "primary_statement_extraction"
            else "standard_unqualified_current_year_consolidated_located_quote",
        ))
    for fact in _active_facts(session, filing):
        evidence = _mapped_evidence(session, fact, job)
        if evidence:
            fact.assurance_level = "audited_annual" if audit_error is None else "unknown"
            for item in evidence:
                current = item.bounding_box
                binding = {"audit_event_id": event.id if event else None,
                           "audit_source_digest": event.output_hash if event else None}
                if any(current.get(key) != value for key, value in binding.items()):
                    item.bounding_box = {**current, **binding}
    session.flush()
