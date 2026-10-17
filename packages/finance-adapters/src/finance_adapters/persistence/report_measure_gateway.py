"""Located report candidates, human review, and reproducible period arithmetic.

Report-disclosed amounts never acquire the strict audited annual assurance by
being extracted or calculated. All copies, signs, periods and review states
are retained; conflicts suppress the affected calculations.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.parsing.report_measures import CORE, extract_report_measures
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.models import (
    AnalysisRun,
    AuditEvent,
    ExtractionJob,
    FilingVersion,
)

VERSION = "report-measures-bundle-v2"
SOURCE_ACTION = "report_measures_extracted"
REVIEW_ACTION = "report_measure_reviewed"
REVIEW_VERSION = "report-measure-review-v1"
REVIEW_FIELDS = {"review_status", "reviewers", "review_count", "source_conflict"}
BOUNDARY = "披露金额的候选计算；不升级为经审计事实，未独立剔除合并范围及会计政策变化。"


def extract_and_persist_measures(session: Session, filing: FilingVersion,
                                 job: ExtractionJob, document) -> dict:
    """Join extraction completion transaction; never commit partial results."""
    if job.filing_version_id != filing.id or job.extraction_method != "pdf_text":
        raise ValueError("REPORT_MEASURES_NATIVE_JOB_REQUIRED")
    payload = extract_report_measures(document, filing, job.page_numbers)
    attempt = job.started_at.replace(tzinfo=None).isoformat() if job.started_at else None
    items = []
    for raw in payload["items"]:
        original_id = raw["id"]
        data = {k: v for k, v in raw.items() if k != "id"}
        if original_id != stable_hash(data):
            raise ValueError("REPORT_MEASURE_EXTRACTION_HASH_MISMATCH")
        data.update(source_item_hash=original_id, extraction_job_id=job.id,
                    extraction_started_at=attempt)
        items.append({**data, "id": stable_hash(data)})
    detail = {**payload, "items": items, "extraction_job_id": job.id,
              "extraction_started_at": attempt, "assurance_granted": False}
    record_audit_event(session, filing.id, category="report_measure", action=SOURCE_ACTION,
                       status="completed", tool_name="pymupdf", tool_version=VERSION,
                       source_ref=filing.object_key, input_hash=filing.file_sha256,
                       output_hash=stable_hash(detail), detail=detail)
    return detail


def report_measure_context(session: Session, filing: FilingVersion) -> dict:
    from finance_adapters.security.file_gate import scanned_object_path
    scanned_object_path(session, filing)
    job = session.scalar(select(ExtractionJob).where(ExtractionJob.filing_version_id == filing.id)
                         .order_by(ExtractionJob.id.desc()).limit(1))
    if job is None or job.status != "completed":
        raise ValueError("CURRENT_COMPLETED_EXTRACTION_REQUIRED")
    attempt = job.started_at.replace(tzinfo=None).isoformat() if job.started_at else None
    detail = None
    for event in session.scalars(select(AuditEvent).where(
            AuditEvent.filing_version_id == filing.id, AuditEvent.action == SOURCE_ACTION,
    ).order_by(AuditEvent.id.desc())):
        payload = event.detail or {}
        if payload.get("extraction_job_id") != job.id or payload.get("extraction_started_at") != attempt:
            continue
        if event.input_hash != filing.file_sha256 or event.output_hash != stable_hash(payload):
            raise ValueError("REPORT_MEASURE_SOURCE_HASH_MISMATCH")
        if payload.get("selected_page_numbers") != job.page_numbers and job.page_numbers is not None:
            raise ValueError("REPORT_MEASURE_PAGE_SCOPE_CHANGED")
        detail = payload
        break
    reviews = defaultdict(dict)
    for event in session.scalars(select(AuditEvent).where(
            AuditEvent.filing_version_id == filing.id, AuditEvent.action == REVIEW_ACTION,
            AuditEvent.tool_version == REVIEW_VERSION,
    ).order_by(AuditEvent.id)):
        record = event.detail or {}
        if event.input_hash != record.get("candidate_id") or event.output_hash != stable_hash(record):
            raise ValueError("REPORT_MEASURE_REVIEW_HASH_MISMATCH")
        reviews[record["candidate_id"]][record["reviewer_id"]] = record["decision"]
    items = []
    for raw in (detail or {}).get("items", []):
        _validate_item(raw)
        if raw["source_sha256"] != filing.file_sha256 or raw["extraction_job_id"] != job.id:
            raise ValueError("REPORT_MEASURE_SOURCE_FILE_MISMATCH")
        decisions = reviews[raw["id"]]
        confirmed = sorted(k for k, v in decisions.items() if v == "confirmed")
        rejected = "rejected" in decisions.values()
        items.append({**raw, "reviewers": confirmed, "review_count": len(confirmed),
                      "review_status": "rejected" if rejected else "reviewed" if len(confirmed) >= 2 else "needs_review"})
    document = {"filing_version_id": filing.id, "source_sha256": filing.file_sha256,
                "company_id": filing.company_id, "scope": filing.scope,
                "accounting_standard": filing.accounting_standard,
                "period_start": filing.period_start.isoformat(), "period_end": filing.period_end.isoformat(),
                "report_type": filing.report_type, "period_basis": filing.period_basis,
                "extraction_job_id": job.id, "extraction_method": job.extraction_method,
                "selected_page_numbers": job.page_numbers,
                "processing_scope": "selected_pages" if job.page_numbers is not None else "whole_document"}
    bundle = rebuild_measure_bundle({"version": VERSION, "documents": [document], "items": items,
                                     "selection_mode": "independent", "anchor_document_id": filing.id})
    return {**document, "version": VERSION, "items": items, "calculations": bundle["calculations"],
            "issues": sorted(set((detail or {}).get("issues", []) + bundle["issues"])),
            "review_policy": {"confirmed_reviewers_required": 2, "identity_authenticated": False},
            "limitations": [BOUNDARY, "OCR金额须通过已有逐单元格映射/复核；本路径不会读取OCR任务的PDF文字层。"]}


def review_report_measure(session: Session, filing: FilingVersion, candidate_id: str,
                          reviewer_id: str, decision: str, reason: str) -> dict:
    if decision not in {"confirmed", "rejected"} or not reviewer_id.strip() or not reason.strip():
        raise ValueError("REPORT_MEASURE_REVIEW_INVALID")
    data = report_measure_context(session, filing)
    candidate = next((x for x in data["items"] if x["id"] == candidate_id), None)
    if candidate is None:
        raise ValueError("REPORT_MEASURE_CURRENT_CANDIDATE_REQUIRED")
    detail = {"candidate_id": candidate_id, "reviewer_id": reviewer_id.strip(),
              "decision": decision, "reason": reason.strip()}
    previous = next((x for x in session.scalars(select(AuditEvent).where(
        AuditEvent.filing_version_id == filing.id, AuditEvent.action == REVIEW_ACTION,
        AuditEvent.input_hash == candidate_id,
    ).order_by(AuditEvent.id.desc())) if (x.detail or {}).get("reviewer_id") == reviewer_id.strip()), None)
    if previous is None or previous.output_hash != stable_hash(detail):
        record_audit_event(session, filing.id, category="report_measure", action=REVIEW_ACTION,
                           status="completed", tool_name="human_review", tool_version=REVIEW_VERSION,
                           input_hash=candidate_id, output_hash=stable_hash(detail), detail=detail)
        # Invalidate anchor and explicit joint analyses that depend on this file.
        for run in session.scalars(select(AnalysisRun).where(AnalysisRun.invalidated_at.is_(None))):
            package = (run.tool_trace or {}).get("input_snapshot", {})
            selected = package.get("selected_document_ids", [])
            if run.filing_version_id == filing.id or filing.id in selected:
                run.invalidated_at = datetime.now(UTC)
                run.status = "invalidated"
                run.failure_reason = "REPORT_MEASURE_REVIEW_CHANGED"
    session.commit()
    return report_measure_context(session, filing)


def _validate_item(item: dict) -> None:
    raw = {k: v for k, v in item.items() if k not in REVIEW_FIELDS | {"id"}}
    if item.get("id") != stable_hash(raw):
        raise ValueError("REPORT_MEASURE_CANDIDATE_HASH_MISMATCH")
    if (item.get("unit") != "CNY" or item.get("currency") != "CNY" or
            item.get("flow_or_stock") != "flow" or not Decimal(item["value"]).is_finite()):
        raise ValueError("REPORT_MEASURE_CONTRACT_MISMATCH")
    if date.fromisoformat(item["period_start"]) > date.fromisoformat(item["period_end"]):
        raise ValueError("REPORT_MEASURE_PERIOD_INVALID")


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _entry(code: str, label: str, formula: str, refs: list[dict], value: Decimal | None,
           *, unit: str = "CNY", details: dict | None = None, start: str | None = None,
           end: str | None = None) -> dict:
    refs = sorted({x["id"]: x for x in refs}.values(), key=lambda x: x["id"])
    payload = {"kind": "report_measure", "code": code, "label": label, "formula": formula,
               "input_ids": [x["id"] for x in refs], "input_snapshot": refs,
               "value": _s(value), "unit": unit,
               "period_start": start or refs[0]["period_start"], "period_end": end or refs[0]["period_end"],
               "filing_version_ids": sorted({x["filing_version_id"] for x in refs}),
               "status": "reviewed" if all(x.get("review_status") == "reviewed" for x in refs) else "candidate_arithmetic",
               "details": details or {}, "boundary": BOUNDARY}
    return {"id": "report:" + stable_hash(payload), **payload}


def _groups(items: list[dict], issues: list[str]) -> dict:
    groups = defaultdict(list)
    for item in items:
        if item.get("review_status") == "rejected":
            continue
        groups[(item["filing_version_id"], item["code"], item["period_start"], item["period_end"])].append(item)
    admitted = {}
    for key, rows in groups.items():
        if len({(Decimal(x["value"]), x.get("arithmetic_sign", 1)) for x in rows}) != 1:
            issues.append("SOURCE_VALUE_CONFLICT:" + ":".join(map(str, key)))
        else:
            admitted[key] = rows
    return admitted


def _single_document(document: dict, groups: dict, out: list[dict], issues: list[str],
                     *, include_prior_nonrec: bool = True) -> None:
    fid, end = document["filing_version_id"], document["period_end"]
    year = int(end[:4])
    start, prior_start, prior_end = f"{year}-01-01", f"{year - 1}-01-01", f"{year - 1}" + end[4:]

    def rows(code, s=start, e=end):
        return groups.get((fid, code, s, e), [])

    def val(code, s=start, e=end):
        group = rows(code, s, e)
        return Decimal(group[0]["value"]) if group else None

    for code, label in CORE.items():
        current, prior = rows(code), rows(code, prior_start, prior_end)
        if current and prior:
            cv, pv = Decimal(current[0]["value"]), Decimal(prior[0]["value"])
            out.append(_entry("yoy:" + code, label + "同比变动率", "(current - prior) / prior", current + prior,
                              (cv - pv) / pv if pv > 0 else None, unit="ratio", start=start, end=end,
                              details={"current": _s(cv), "prior": _s(pv), "difference": _s(cv - pv),
                                       "growth_rate": _s((cv - pv) / pv) if pv > 0 else None,
                                       "unavailable_reason": None if pv > 0 else "NON_POSITIVE_PRIOR_DENOMINATOR"}))
    profit, cash = rows("net_income"), rows("cashflow_operations")
    if profit and cash:
        pv, cv = val("net_income"), val("cashflow_operations")
        out.append(_entry("cashflow_profit_ratio", "经营现金流/合并净利润", "CFO / consolidated_net_income",
                          profit + cash, cv / pv if pv > 0 else None, unit="ratio", start=start, end=end,
                          details={"net_income": _s(pv), "cashflow": _s(cv), "opposite_signs": pv * cv < 0}))
        pp, cp = rows("net_income", prior_start, prior_end), rows("cashflow_operations", prior_start, prior_end)
        if pp and cp:
            pd, cd = pv - Decimal(pp[0]["value"]), cv - Decimal(cp[0]["value"])
            out.append(_entry("profit_cashflow_changes", "合并利润与经营现金流同比方向", "compare(current - prior)",
                              profit + cash + pp + cp, None, start=start, end=end,
                              details={"profit_change": _s(pd), "cashflow_change": _s(cd),
                                       "profit_up_cashflow_down": pd > 0 and cd < 0,
                                       "opposite_change_directions": pd * cd < 0}))
    nonrec_periods = [(start, end)]
    if include_prior_nonrec:
        nonrec_periods.append((prior_start, prior_end))
    for s, e in nonrec_periods:
        parent, ex = rows("net_income_parent", s, e), rows("net_income_ex_nonrecurring", s, e)
        if parent and ex:
            nonrec = val("net_income_parent", s, e) - val("net_income_ex_nonrecurring", s, e)
            total = rows("nonrec_total", s, e)
            out.append(_entry("nonrec_parent_net", "非经常性损益归母税后净额", "parent_net_income - parent_ex_nonrecurring",
                              parent + ex + total, nonrec, start=s, end=e,
                              details={"contribution_rate": _s(nonrec / val("net_income_parent", s, e)) if val("net_income_parent", s, e) > 0 else None,
                                       "disclosed_total": _s(val("nonrec_total", s, e)),
                                       "residual": _s(nonrec - val("nonrec_total", s, e)) if total else None}))
    # Rows retain the sign printed in the source table. A residual is always
    # shown, never hidden.
    for s, e in sorted({(key[2], key[3]) for key in groups if key[0] == fid}):
        nonrec_codes = [key[1] for key in groups if key[0] == fid and key[2:] == (s, e) and key[1].startswith("nonrec_") and key[1] != "nonrec_total"]
        total = rows("nonrec_total", s, e)
        if nonrec_codes and total:
            refs = [x for code in sorted(nonrec_codes) for x in rows(code, s, e)]
            signed = sum((Decimal(rows(c, s, e)[0]["value"]) * rows(c, s, e)[0].get("arithmetic_sign", 1) for c in nonrec_codes), Decimal(0))
            residual = signed - Decimal(total[0]["value"])
            required = {"nonrec_tax_effect", "nonrec_minority_effect"}
            complete = required.issubset(nonrec_codes) and len(nonrec_codes) >= 3
            out.append(_entry("nonrec_signed_bridge", "非经常性损益逐行税后归母桥接", "sum(signed_rows) - disclosed_total",
                              refs + total, residual, start=s, end=e,
                              details={"row_count": len(nonrec_codes), "signed_sum": _s(signed),
                                       "disclosed_total": total[0]["value"], "residual": _s(residual),
                                       "matched": residual == 0 and complete, "tax_and_minority_present": complete}))
        bridge_codes = [key[1] for key in groups if key[0] == fid and key[2:] == (s, e) and key[1].startswith("bridge_row:")]
        total = rows("bridge_total", s, e)
        if bridge_codes and total:
            refs = [x for c in sorted(bridge_codes) for x in rows(c, s, e)]
            amount = sum((Decimal(rows(c, s, e)[0]["value"]) for c in bridge_codes), Decimal(0))
            residual = amount - Decimal(total[0]["value"])
            out.append(_entry("cashflow_signed_bridge", "合并净利润至经营现金流逐行桥接", "sum(signed_rows) - CFO",
                              refs + total, residual, start=s, end=e,
                              details={"row_count": len(bridge_codes), "signed_sum": _s(amount), "residual": _s(residual),
                                       "disclosed_total": total[0]["value"], "matched": residual == 0}))
    current_bridge = {key[1]: value for key, value in groups.items() if key[0] == fid and key[2:] == (start, end) and key[1].startswith("bridge_row:")}
    prior_bridge = {key[1]: value for key, value in groups.items() if key[0] == fid and key[2:] == (prior_start, prior_end) and key[1].startswith("bridge_row:")}
    ct, pt = rows("bridge_total"), rows("bridge_total", prior_start, prior_end)
    if current_bridge and current_bridge.keys() == prior_bridge.keys() and ct and pt:
        contributions = [{"label": current_bridge[c][0]["label"], "code": c,
                          "difference": _s(Decimal(current_bridge[c][0]["value"]) - Decimal(prior_bridge[c][0]["value"]))}
                         for c in sorted(current_bridge)]
        difference = Decimal(ct[0]["value"]) - Decimal(pt[0]["value"])
        contribution_sum = sum((Decimal(x["difference"]) for x in contributions), Decimal(0))
        refs = [x for g in [*current_bridge.values(), *prior_bridge.values(), ct, pt] for x in g]
        out.append(_entry("cashflow_bridge_changes", "经营现金流同比差额的表内行贡献", "sum(current_row - prior_row)",
                          refs, difference, start=start, end=end,
                          details={"rows": contributions, "residual": _s(difference - contribution_sum),
                                   "matched": difference == contribution_sum,
                                   "not_independently_verified_business_cause": True}))


def _joint(documents: list[dict], groups: dict, out: list[dict], issues: list[str]) -> None:
    # A cross-report series is conditional on declared company/scope/accounting
    # tags. We never assert that consolidation-population effects are removed.
    series = defaultdict(list)
    for key, refs in groups.items():
        fid, code, s, e = key
        if code in CORE and s[5:] == "01-01" and e[5:] in {"03-31", "06-30", "09-30", "12-31"}:
            series[(code, int(s[:4]), int(e[5:7]) // 3)].append((fid, refs))
    quarters = {}
    for (code, year, q), copies in sorted(series.items()):
        if len({Decimal(refs[0]["value"]) for _, refs in copies}) != 1:
            issues.append(f"CROSS_REPORT_VALUE_CONFLICT:{code}:{year}:Q{q}")
            continue
        later = [x for _, refs in copies for x in refs]
        if q == 1:
            refs, value = later, Decimal(later[0]["value"])
        else:
            previous = series.get((code, year, q - 1), [])
            if not previous or len({Decimal(r[0]["value"]) for _, r in previous}) != 1:
                issues.append(f"MISSING_PRIOR_YTD_FOR_QUARTER:{code}:{year}:Q{q}")
                continue
            refs = later + [x for _, r in previous for x in r]
            value = Decimal(later[0]["value"]) - Decimal(previous[0][1][0]["value"])
        s = date(year, (q - 1) * 3 + 1, 1).isoformat()
        e = date(year + 1, 1, 1) - timedelta(days=1) if q == 4 else date(year, q * 3 + 1, 1) - timedelta(days=1)
        # Compare directly disclosed QTD where available; do not reverse rounded percentages.
        direct = [r for key, r in groups.items() if key[1:] == (code, s, e.isoformat())]
        details = {"quarter": q, "year": year, "conditional_cross_report_comparability": True}
        if q > 1 and direct:
            if any(Decimal(row["value"]) != value for group in direct for row in group):
                issues.append(f"DIRECT_QUARTER_VALUE_CONFLICT:{code}:{year}:Q{q}")
                continue
            # Direct disclosure participates in the calculation binding and
            # review state, not merely a list of unbound comparison IDs.
            refs = refs + [x for group in direct for x in group]
            details.update(direct_qtd_residual="0", direct_qtd_ids=[x["id"] for r in direct for x in r])
        calc = _entry(f"quarter:{code}", f"{CORE[code]}单季金额", "Q1=YTD3; Qn=YTDn-YTD(n-1)", refs, value,
                      start=s, end=e.isoformat(), details=details)
        out.append(calc)
        quarters[(code, year, q)] = (calc, refs, value)
    for (code, year, q), (current, refs, value) in sorted(quarters.items()):
        for comparison, prior_key in (("qoq", (code, year, q - 1) if q > 1 else (code, year - 1, 4)),
                                      ("quarter_yoy", (code, year - 1, q))):
            prior = quarters.get(prior_key)
            if prior is None:
                if comparison == "qoq":
                    issues.append(f"MISSING_PREVIOUS_QUARTER:{code}:{year}:Q{q}:need:{prior_key[1]}:Q{prior_key[2]}")
                continue
            pv = prior[2]
            out.append(_entry(comparison + ":" + code, CORE[code] + ("单季环比" if comparison == "qoq" else "单季同比"),
                              "(quarter_current - quarter_prior) / quarter_prior", refs + prior[1],
                              (value - pv) / pv if pv > 0 else None, unit="ratio",
                              start=current["period_start"], end=current["period_end"],
                              details={"current": _s(value), "prior": _s(pv), "difference": _s(value - pv),
                                       "growth_rate": _s((value - pv) / pv) if pv > 0 else None,
                                       "unavailable_reason": None if pv > 0 else "NON_POSITIVE_PRIOR_DENOMINATOR"}))


def _independent_quarters(document: dict, groups: dict, out: list[dict]) -> None:
    """Use complete, directly disclosed calendar quarters in one report."""
    quarters = {}
    fid = document["filing_version_id"]
    for (source_id, code, start, end), refs in sorted(groups.items()):
        if source_id != fid or code not in CORE:
            continue
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        quarter = (first.month - 1) // 3 + 1
        expected_end = (date(first.year + 1, 1, 1) - timedelta(days=1) if quarter == 4
                        else date(first.year, quarter * 3 + 1, 1) - timedelta(days=1))
        if (first.day != 1 or first.month != (quarter - 1) * 3 + 1 or last != expected_end
                or not any(item.get("group") == "single_quarter" for item in refs)):
            continue
        value = Decimal(refs[0]["value"])
        out.append(_entry("quarter:" + code, CORE[code] + "单季金额", "direct_disclosed_quarter",
                          refs, value, start=start, end=end,
                          details={"quarter": quarter, "year": first.year,
                                   "same_report_direct_disclosure": True}))
        quarters[(code, first.year, quarter)] = (refs, value, start, end)
    for (code, year, quarter), (refs, value, start, end) in sorted(quarters.items()):
        prior = quarters.get((code, year, quarter - 1) if quarter > 1 else (code, year - 1, 4))
        if prior is None:
            continue
        prior_refs, prior_value, _, _ = prior
        growth = (value - prior_value) / prior_value if prior_value > 0 else None
        out.append(_entry("qoq:" + code, CORE[code] + "单季环比",
                          "(quarter_current - quarter_prior) / quarter_prior", refs + prior_refs,
                          growth, unit="ratio", start=start, end=end,
                          details={"current": _s(value), "prior": _s(prior_value),
                                   "difference": _s(value - prior_value), "growth_rate": _s(growth),
                                   "same_report_direct_disclosure": True,
                                   "unavailable_reason": None if growth is not None else "NON_POSITIVE_PRIOR_DENOMINATOR"}))


def rebuild_measure_bundle(bundle: dict) -> dict:
    version = bundle.get("version")
    if version not in ("report-measures-bundle-v1", VERSION):
        raise ValueError("REPORT_MEASURE_BUNDLE_VERSION_UNSUPPORTED")
    documents, items = bundle.get("documents", []), bundle.get("items", [])
    ids = [x["filing_version_id"] for x in documents]
    if not documents or len(set(ids)) != len(ids):
        raise ValueError("REPORT_MEASURE_DOCUMENT_SELECTION_INVALID")
    if bundle.get("selection_mode") == "independent" and len(ids) != 1:
        raise ValueError("REPORT_MEASURE_INDEPENDENT_SCOPE_INVALID")
    if bundle.get("selection_mode") not in {"independent", "explicit_joint"}:
        raise ValueError("REPORT_MEASURE_SELECTION_REQUIRED")
    if len({(x["company_id"], x["scope"], x["accounting_standard"]) for x in documents}) != 1:
        raise ValueError("REPORT_MEASURE_CROSS_SCOPE_INVALID")
    by_id = {x["filing_version_id"]: x for x in documents}
    for item in items:
        _validate_item(item)
        document = by_id.get(item["filing_version_id"])
        if (document is None or item["source_sha256"] != document["source_sha256"] or
                any(item[k] != document[k] for k in ("company_id", "scope", "accounting_standard")) or
                document.get("selected_page_numbers") is not None and item["page_no"] not in document["selected_page_numbers"]):
            raise ValueError("REPORT_MEASURE_ITEM_OUTSIDE_DOCUMENT_SELECTION")
    issues, out = [], []
    groups = _groups(items, issues)
    with localcontext() as context:
        context.prec = 48
        for document in documents:
            _single_document(document, groups, out, issues, include_prior_nonrec=version == VERSION)
        if bundle["selection_mode"] == "explicit_joint":
            _joint(documents, groups, out, issues)
        elif version == VERSION:
            _independent_quarters(documents[0], groups, out)
    return {"version": version, "documents": documents, "items": items,
            "selection_mode": bundle["selection_mode"], "anchor_document_id": bundle["anchor_document_id"],
            "selected_document_ids": ids, "calculations": out, "issues": sorted(set(issues)), "limitations": [BOUNDARY]}


def validate_measure_bundle(bundle: dict) -> None:
    if bundle != rebuild_measure_bundle(bundle):
        raise ValueError("REPORT_MEASURE_BUNDLE_RECOMPUTATION_MISMATCH")
