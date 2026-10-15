"""Append-only disclosure evidence/reviews; never grant audited-fact assurance."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, localcontext

from finance_domain.filings.disclosures import (
    DISCLOSURE_DEFINITIONS,
    DisclosureReviewRequest,
    OcrDisclosureMappingRequest,
    check_disclosure_currency,
    normalize_disclosure_value,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.parsing.annual_disclosures import DISCLOSURE_VERSION, disclosure_label
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.models import (
    AuditEvent,
    ExtractionJob,
    FilingVersion,
    TableCandidateRecord,
)

SOURCE_ACTION = "annual_disclosures_extracted"
MAPPING_ACTION = "annual_disclosure_mapped"
REVIEW_ACTION = "annual_disclosure_reviewed"
POLICY_VERSION = "reviewed-annual-disclosures-v1"
LIMITATIONS = [
    "这些为年度报告披露值，不因财报审计意见而自动成为经审计三表事实。",
    "两名不同审核标识仅确认披露原文、单位和期间；目前未认证个人身份。",
    "披露加权ROE和EPS不以简单期初期末平均数或期末股数重新冒算。",
    "跨期数据保留同一报告列报口径及相关注释；尚未验证所有调整、加权明细。",
    "这些披露及派生比较单独展示，尚未纳入模型数值结论通路。",
]


def _job(session: Session, filing: FilingVersion) -> ExtractionJob | None:
    from finance_adapters.persistence.metric_gateway import require_supported_period
    require_supported_period(filing)
    job = session.scalar(select(ExtractionJob).where(ExtractionJob.filing_version_id == filing.id)
                         .order_by(ExtractionJob.id.desc()).limit(1))
    if job is not None and job.status != "completed":
        raise ValueError("CURRENT_EXTRACTION_NOT_COMPLETED")
    return job


def _candidate(item: dict, job_id: int, file_hash: str, attempt: str | None) -> dict:
    payload = {**item, "extraction_job_id": job_id, "file_sha256": file_hash, "extraction_started_at": attempt}
    return {**payload, "id": stable_hash(payload)}


def persist_annual_disclosures(session: Session, filing: FilingVersion, job_id: int, items: list[dict]) -> None:
    """Participate in the extraction completion transaction; no hidden commit."""
    job = session.get(ExtractionJob, job_id)
    attempt = job.started_at.replace(tzinfo=None).isoformat() if job and job.started_at else None
    candidates = [_candidate(item, job_id, filing.file_sha256, attempt) for item in items]
    record_audit_event(session, filing.id, category="disclosure", action=SOURCE_ACTION, status="completed",
                       tool_name="pymupdf", tool_version=DISCLOSURE_VERSION,
                       source_ref=filing.object_key, input_hash=filing.file_sha256,
                       output_hash=stable_hash(candidates),
                       detail={"extraction_job_id": job_id, "extraction_started_at": attempt,
                               "items": candidates, "assurance_granted": False})


def _live_candidates(session: Session, filing: FilingVersion, job: ExtractionJob | None,
                      *, validate_sources: bool = True) -> list[dict]:
    if job is None:
        return []
    events = list(session.scalars(select(AuditEvent).where(
        AuditEvent.filing_version_id == filing.id,
        AuditEvent.action.in_([SOURCE_ACTION, MAPPING_ACTION]),
    ).order_by(AuditEvent.id)))
    native = []
    mapped = []
    for event in events:
        detail = event.detail or {}
        attempt = job.started_at.replace(tzinfo=None).isoformat() if job.started_at else None
        if (detail.get("extraction_job_id") != job.id or event.input_hash != filing.file_sha256
                or detail.get("extraction_started_at") != attempt):
            continue
        if event.output_hash != stable_hash(detail.get("items", [])):
            raise ValueError("DISCLOSURE_SOURCE_EVENT_HASH_MISMATCH")
        if event.action == SOURCE_ACTION and job.extraction_method == "pdf_text":
            native = detail.get("items", [])
        elif event.action == MAPPING_ACTION and job.extraction_method == "ocr":
            mapped.extend(detail.get("items", []))
    candidates = native if job.extraction_method == "pdf_text" else mapped
    latest_ids = {(item["code"], item["period_end"]): item["id"] for item in candidates}
    by_key = {}
    for item in candidates:
        unhashed = {key: value for key, value in item.items() if key != "id"}
        if item.get("id") != stable_hash(unhashed):
            raise ValueError("DISCLOSURE_CANDIDATE_HASH_MISMATCH")
        definition = DISCLOSURE_DEFINITIONS.get(item.get("code"))
        if (definition is None or item.get("unit") != definition["unit"]
                or item.get("scope") != definition["scope"] or item.get("basis") != "as_disclosed"
                or item.get("assurance") != "annual_report_disclosed_not_audit_opinion"
                or item.get("file_sha256") != filing.file_sha256):
            raise ValueError("DISCLOSURE_CONTRACT_MISMATCH")
        if (validate_sources and item.get("source_kind") == "ocr"
                and item["id"] == latest_ids[(item["code"], item["period_end"])]):
            from finance_adapters.persistence.mapping_gateway import _source_call
            _source_call(session, job, item["ocr_call_id"], item["page_sha256"], item["response_sha256"])
            mapping = OcrDisclosureMappingRequest.model_validate(item["mapping"])
            table = session.get(TableCandidateRecord, mapping.table_id)
            if (table is None or table.extraction_job_id != job.id or table.ocr_call_id != item["ocr_call_id"]
                    or mapping.row_index + 1 >= len(table.rows) or mapping.column_index >= len(table.rows[0])
                    or mapping.column_index >= len(table.rows[mapping.row_index + 1])
                    or table.rows[mapping.row_index + 1][0].strip() != mapping.raw_label
                    or table.rows[mapping.row_index + 1][mapping.column_index].strip() != mapping.raw_value
                    or table.rows[0][mapping.column_index].strip() != mapping.column_label):
                raise ValueError("DISCLOSURE_OCR_TABLE_SOURCE_CHANGED")
            unit = item.get("unit_source")
            if unit:
                _source_call(session, job, unit["ocr_call_id"], unit["page_sha256"], unit["response_sha256"])
        by_key[(item["code"], item["period_end"])] = item
    # Conflicting native copies need review of a new mapped source instead of
    # silently selecting the first copy. Native extractor records all evidence.
    if job.extraction_method == "pdf_text":
        for key, chosen in by_key.items():
            same = [item for item in candidates if (item["code"], item["period_end"]) == key]
            if len({(item["value"], item["unit"], item["currency"]) for item in same}) > 1:
                chosen = {**chosen, "source_conflict": True}
                by_key[key] = chosen
    return list(by_key.values())


def _reviewed_items(session: Session, filing: FilingVersion, candidates: list[dict]) -> list[dict]:
    reviews = list(session.scalars(select(AuditEvent).where(
        AuditEvent.filing_version_id == filing.id, AuditEvent.action == REVIEW_ACTION,
        AuditEvent.tool_version == POLICY_VERSION,
    ).order_by(AuditEvent.id)))
    items = []
    for candidate in candidates:
        by_reviewer = {}
        for event in reviews:
            detail = event.detail or {}
            if (detail.get("candidate_id") == candidate["id"] and event.input_hash == candidate["id"]
                    and event.output_hash == stable_hash(detail)):
                by_reviewer[detail.get("reviewer_id")] = detail
        confirmed = [key for key, value in by_reviewer.items() if key and value.get("decision") == "confirmed"]
        rejected = any(value.get("decision") == "rejected" for value in by_reviewer.values())
        reviewed = len(confirmed) >= 2 and not rejected and not candidate.get("source_conflict")
        items.append({**candidate, "review_count": len(confirmed),
                      "review_status": "reviewed" if reviewed else "rejected" if rejected else "needs_review",
                      "reviewers": sorted(confirmed)})
    return sorted(items, key=lambda item: (item["code"], item["period_end"]))


def _display(value: Decimal) -> str:
    return format(value, ".12f").rstrip("0").rstrip(".") or "0"


def disclosure_calculations(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Only matched sources/periods; negative prior profit has no normal CAGR."""
    comparisons, reconciliations = [], []
    by_code = {}
    for item in items:
        by_code.setdefault(item["code"], {})[item["period_role"]] = item
    with localcontext() as context:
        context.prec = 48
        for code, periods in sorted(by_code.items()):
            current, prior = periods.get("current"), periods.get("comparative")
            if not current or not prior:
                continue
            status = "reviewed" if all(x["review_status"] == "reviewed" for x in (current, prior)) else "candidate_arithmetic"
            if (current["unit"], current["currency"], current["scope"], current["basis"]) != (
                    prior["unit"], prior["currency"], prior["scope"], prior["basis"]):
                continue
            current_value, prior_value = Decimal(current["value"]), Decimal(prior["value"])
            change = current_value - prior_value
            entry = {"code": code, "current_id": current["id"], "prior_id": prior["id"],
                     "current_value": current["value"], "prior_value": prior["value"], "status": status,
                     "difference": _display(change), "difference_unit": current["unit"],
                     "growth_rate": _display(change / prior_value) if prior_value > 0 else None,
                     "growth_unavailable_reason": None if prior_value > 0 else "NON_POSITIVE_PRIOR_DENOMINATOR"}
            if current["unit"] == "ratio":
                entry.update(difference=_display(change * 100), percentage_point_change=_display(change * 100),
                             difference_unit="percentage_points")
            comparisons.append(entry)
        for role in ("current", "comparative"):
            required = [by_code.get(code, {}).get(role) for code in (
                "rd_investment_disclosed", "rd_capitalized_disclosed", "rd_capitalization_ratio_disclosed")]
            if not all(required):
                continue
            investment, capitalized, disclosed = required
            denominator, numerator = Decimal(investment["value"]), Decimal(capitalized["value"])
            raw = (disclosed.get("corrected_raw_value") or disclosed["raw_value"]).replace("％", "%").strip().rstrip("%")
            decimals = len(raw.rsplit(".", 1)[1]) if "." in raw else 0
            tolerance = Decimal(1).scaleb(-decimals) / 200
            issues = []
            if denominator <= 0:
                issues.append("NON_POSITIVE_RD_INVESTMENT")
            if numerator < 0:
                issues.append("NEGATIVE_CAPITALIZED_RD")
            if numerator > denominator:
                issues.append("CAPITALIZED_RD_EXCEEDS_TOTAL_RD")
            if not 0 <= Decimal(disclosed["value"]) <= 1:
                issues.append("DISCLOSED_RD_CAPITALIZATION_RATIO_OUT_OF_RANGE")
            value = numerator / denominator if not issues else None
            reconciliations.append({"code": "rd_capitalization_ratio", "period_role": role,
                "input_ids": [x["id"] for x in required], "calculated_value": _display(value) if value is not None else None,
                "disclosed_value": disclosed["value"], "tolerance": _display(tolerance),
                "matched": abs(value - Decimal(disclosed["value"])) <= tolerance if value is not None else False,
                "calculation_status": "calculated" if value is not None else "insufficient_data", "issues": issues,
                "status": "reviewed" if all(x["review_status"] == "reviewed" for x in required) else "candidate_arithmetic",
                "formula": "rd_capitalized_disclosed / rd_investment_disclosed"})
    return comparisons, reconciliations


def annual_disclosure_context(session: Session, filing: FilingVersion) -> dict:
    job = _job(session, filing)
    candidates = _live_candidates(session, filing, job)
    if candidates:
        from finance_adapters.security.file_gate import scanned_object_path
        scanned_object_path(session, filing)
    items = _reviewed_items(session, filing, candidates)
    comparisons, reconciliations = disclosure_calculations(items)
    return {"extraction_job_id": job.id if job else None, "source_kind": job.extraction_method if job else None,
            "items": items, "derived_comparisons": comparisons, "reconciliations": reconciliations,
            "policy_version": POLICY_VERSION, "limitations": LIMITATIONS,
            "definitions": [{"code": code, **definition} for code, definition in DISCLOSURE_DEFINITIONS.items()]}


def review_annual_disclosure(session: Session, filing: FilingVersion, candidate_id: str,
                             payload: DisclosureReviewRequest) -> dict:
    from finance_adapters.security.file_gate import scanned_object_path
    scanned_object_path(session, filing)
    context = annual_disclosure_context(session, filing)
    candidate = next((item for item in context["items"] if item["id"] == candidate_id), None)
    if candidate is None or candidate.get("source_conflict"):
        raise ValueError("DISCLOSURE_CURRENT_UNAMBIGUOUS_CANDIDATE_REQUIRED")
    detail = {"candidate_id": candidate_id, **payload.model_dump()}
    previous = next((event for event in session.scalars(select(AuditEvent).where(
        AuditEvent.filing_version_id == filing.id, AuditEvent.action == REVIEW_ACTION,
        AuditEvent.input_hash == candidate_id,
    ).order_by(AuditEvent.id.desc())) if (event.detail or {}).get("reviewer_id") == payload.reviewer_id), None)
    if previous is None or previous.output_hash != stable_hash(detail):
        record_audit_event(session, filing.id, category="disclosure", action=REVIEW_ACTION, status="completed",
                           tool_name="human_review", tool_version=POLICY_VERSION, input_hash=candidate_id,
                           output_hash=stable_hash(detail), detail=detail)
        session.commit()
    return annual_disclosure_context(session, filing)


def extract_annual_disclosures(session: Session, filing: FilingVersion) -> dict:
    """Explicit backfill for an existing native job; never invoke an OCR model."""
    import fitz

    from finance_adapters.parsing.annual_disclosures import document_annual_disclosures
    from finance_adapters.security.file_gate import scanned_object_path

    job = _job(session, filing)
    if job is None or job.extraction_method != "pdf_text":
        raise ValueError("CURRENT_COMPLETED_NATIVE_EXTRACTION_REQUIRED")
    path = scanned_object_path(session, filing)
    with fitz.open(path) as document:
        texts = [page.get_text("text") for page in document]
        items = document_annual_disclosures(document, texts, filing.period_end.year)
    session.expire(job)
    if _job(session, filing).id != job.id:
        raise ValueError("DISCLOSURE_EXTRACTION_JOB_CHANGED")
    persist_annual_disclosures(session, filing, job.id, items)
    session.commit()
    return annual_disclosure_context(session, filing)


def map_ocr_disclosure(session: Session, filing: FilingVersion, payload: OcrDisclosureMappingRequest) -> dict:
    from finance_adapters.persistence.mapping_gateway import (
        _check_box,
        _check_column_header,
        _check_currency,
        _source_call,
        latest_ocr_job,
        render_ocr_page,
    )
    job = latest_ocr_job(session, filing)
    _job(session, filing)
    call = _source_call(session, job, payload.ocr_call_id, payload.page_sha256, payload.response_sha256)
    table = session.get(TableCandidateRecord, payload.table_id)
    if (table is None or table.extraction_job_id != job.id or table.ocr_call_id != call.id
            or table.extractor != "ocr-table-review-v1" or table.page_no != call.page_no):
        raise ValueError("OCR_TABLE_STALE_OR_MISMATCHED")
    rows = table.rows
    if (payload.row_index + 1 >= len(rows) or not rows
            or payload.column_index >= len(rows[0]) or payload.column_index >= len(rows[payload.row_index + 1])):
        raise ValueError("OCR_TABLE_CELL_OUT_OF_RANGE")
    row = rows[payload.row_index + 1]
    if (payload.raw_label != row[0].strip() or payload.raw_value != row[payload.column_index].strip()
            or payload.column_label != rows[0][payload.column_index].strip()):
        raise ValueError("OCR_TABLE_RAW_CELL_MISMATCH")
    definition = DISCLOSURE_DEFINITIONS.get(payload.code)
    if definition is None or disclosure_label(payload.raw_label) not in definition["labels"]:
        raise ValueError("DISCLOSURE_EXPLICIT_LABEL_REQUIRED")
    source = re.sub(r"\s+", "", call.response_text)
    if definition["section"] not in source:
        raise ValueError("DISCLOSURE_SECTION_SOURCE_REQUIRED")
    year_match = re.search(r"20\d{2}", payload.column_label)
    if year_match is None or int(year_match[0]) not in {filing.period_end.year, filing.period_end.year - 1}:
        raise ValueError("DISCLOSURE_ANNUAL_PERIOD_REQUIRED")
    year = int(year_match[0])
    _check_column_header(payload.column_label, year, "利润表")
    # OCR providers often place the year and subannual qualifier on separate
    # header rows. Inspect this table before the selected row, not the whole
    # page: annual and quarterly tables legitimately coexist on a page.
    for preceding in rows[1:payload.row_index + 1]:
        joined = re.sub(r"\s+", "", "".join(preceding))
        if re.search(r"季度|半年|期中|累计|YTD|quarter|sixmonths|[1-9]月|1[01]月", joined, re.IGNORECASE):
            raise ValueError("DISCLOSURE_SUBANNUAL_OR_MULTILEVEL_COLUMN")
    unit_call = call
    refs = (payload.unit_ocr_call_id, payload.unit_page_sha256, payload.unit_response_sha256)
    if any(ref is not None for ref in refs):
        if not all(ref is not None for ref in refs):
            raise ValueError("OCR_UNIT_SOURCE_REFERENCE_INCOMPLETE")
        unit_call = _source_call(session, job, *refs)
    _check_currency(call.response_text)
    _check_currency(unit_call.response_text)
    unit_source_text = re.sub(r"\s+", "", unit_call.response_text)
    if not payload.unit_text.strip() or re.sub(r"\s+", "", payload.unit_text) not in unit_source_text:
        raise ValueError("DISCLOSURE_UNIT_SOURCE_REQUIRED")
    unit = definition["unit"]
    unit_source = re.sub(r"\s+", "", payload.unit_text)
    check_disclosure_currency(payload.unit_text, unit_context=True)
    inline = re.search(r"\((?:人民币)?(元/股|百万元|亿元|万元|千元|元)\)$",
                       re.sub(r"\s+", "", payload.raw_label).replace("（", "(").replace("）", ")"))
    if inline:
        inline_unit = inline[1]
        inline_scale = {"元": 1, "千元": 1000, "万元": 10000, "百万元": 1000000, "亿元": 100000000}.get(inline_unit)
        if (unit == "currency" and inline_scale != payload.unit_scale
                or unit == "currency_per_share" and inline_unit != "元/股"
                or unit == "ratio"):
            raise ValueError("DISCLOSURE_INLINE_UNIT_CONFLICT")
    if (unit == "currency_per_share" and "元/股" not in unit_source
            or unit == "currency" and not any(word in unit_source for word in ("元", "千元", "万元", "亿元"))
            or unit == "ratio" and not any(word in unit_source for word in ("%", "％"))):
        raise ValueError("DISCLOSURE_UNIT_SOURCE_REQUIRED")
    if unit == "currency":
        words = {1: "元", 1000: "千元", 10000: "万元", 1000000: "百万元", 100000000: "亿元"}
        match = re.search(r"(百万元|亿元|万元|千元|元)", unit_source)
        if match is None or match[1] != words.get(payload.unit_scale):
            raise ValueError("DISCLOSURE_UNIT_SCALE_INVALID")
    _, width, height = render_ocr_page(session, filing, call)
    for box in (payload.value_bbox, payload.row_bbox):
        _check_box(box, width, height)
    _, unit_width, unit_height = render_ocr_page(session, filing, unit_call)
    _check_box(payload.unit_bbox, unit_width, unit_height)
    row_box, value_box = payload.row_bbox, payload.value_bbox
    if not (row_box[0] <= value_box[0] < value_box[2] <= row_box[2]
            and row_box[1] <= value_box[1] < value_box[3] <= row_box[3]):
        raise ValueError("DISCLOSURE_VALUE_OUTSIDE_ROW")
    value = normalize_disclosure_value(payload.corrected_raw_value or payload.raw_value, unit,
                                       unit_scale=payload.unit_scale)
    item = {"code": payload.code, "name": definition["name"], "value": str(value), "unit": unit,
            "currency": "CNY" if unit != "ratio" else None, "unit_scale": payload.unit_scale,
            "scope": definition["scope"], "raw_value": payload.raw_value, "raw_label": payload.raw_label,
            "corrected_raw_value": payload.corrected_raw_value, "column_label": payload.column_label,
            "period_start": date(year, 1, 1).isoformat(), "period_end": date(year, 12, 31).isoformat(),
            "period_role": "current" if year == filing.period_end.year else "comparative",
            "page_no": call.page_no, "page_sha256": call.page_sha256, "response_sha256": call.response_hash,
            "ocr_call_id": call.id, "source_kind": "ocr", "section": definition["section"],
            "unit_source": {"ocr_call_id": unit_call.id, "page_sha256": unit_call.page_sha256,
                            "response_sha256": unit_call.response_hash},
            "basis": "as_disclosed", "assurance": "annual_report_disclosed_not_audit_opinion",
            "source_excerpt": call.response_text[:12000], "mapping": payload.model_dump(),
            "evidence": {"source": "ocr-image", "row": payload.row_bbox, "value": payload.value_bbox,
                         "unit": payload.unit_bbox, "unit_text": payload.unit_text}}
    attempt = job.started_at.replace(tzinfo=None).isoformat() if job.started_at else None
    candidate = _candidate(item, job.id, filing.file_sha256, attempt)
    live = _live_candidates(session, filing, job, validate_sources=False)
    predecessor = next((existing for existing in live
                        if (existing["code"], existing["period_end"]) == (candidate["code"], candidate["period_end"])), None)
    if predecessor and predecessor.get("mapping") == payload.model_dump():
        return annual_disclosure_context(session, filing)
    if predecessor:
        candidate = _candidate({**item, "supersedes_id": predecessor["id"]}, job.id, filing.file_sha256, attempt)
    if not any(existing["id"] == candidate["id"] for existing in live):
        record_audit_event(session, filing.id, category="disclosure", action=MAPPING_ACTION, status="completed",
                           source_ref=f"ocr:{call.id}", tool_name="human_mapping", tool_version=POLICY_VERSION,
                           input_hash=filing.file_sha256, output_hash=stable_hash([candidate]),
                           detail={"extraction_job_id": job.id, "extraction_started_at": attempt,
                                   "items": [candidate], "assurance_granted": False})
        session.commit()
    return annual_disclosure_context(session, filing)
