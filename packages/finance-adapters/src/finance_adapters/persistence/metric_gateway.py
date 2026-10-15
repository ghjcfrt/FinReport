from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from decimal import Decimal

from finance_domain.metrics.definitions import is_financial_industry
from finance_domain.metrics.formulas import free_cash_flow
from sqlalchemy import select, update
from sqlalchemy.orm import Session, object_session

from finance_adapters.persistence.models import (
    Company,
    EvidenceFragment,
    FactDimension,
    FactReview,
    FilingVersion,
    FinancialFact,
    MetricResult,
    ValidationResult,
)

FORMULA_VERSION = "general-non-financial-v2"


def _related_records(session: Session, model, fact_ids: list[int], *ordering) -> dict:
    """Batch within conservative bind limits; no cross-request ORM cache."""
    grouped = defaultdict(list)
    for start in range(0, len(fact_ids), 500):
        query = select(model).where(model.fact_id.in_(fact_ids[start:start + 500]))
        for row in session.scalars(query.order_by(*ordering)):
            grouped[row.fact_id].append(row)
    return grouped


def _snapshot_record(fact: FinancialFact, evidence, dimensions) -> dict:
    return {
        "id": fact.id,
        "code": fact.concept_code,
        "value": str(Decimal(fact.value).quantize(Decimal("0.0001"))),
        "revision": fact.revision_no,
        "currency": fact.currency, "unit_scale": fact.unit_scale,
        "period_start": str(fact.period_start), "period_end": str(fact.period_end),
        "statement_type": fact.statement_type,
        "dimensions": [[d.dimension_name, d.dimension_value] for d in dimensions],
        "evidence": [{"id": e.id, "page": e.page_no, "sha256": e.page_sha256,
                      "row": e.row_locator, "column": e.column_locator,
                      "bbox": e.bounding_box, "raw_value": e.raw_value} for e in evidence],
    }


def _review_digest(fact: FinancialFact, evidence, dimensions) -> str:
    payload = {
        "version": "fact-review-evidence-v1",
        "snapshot": _snapshot_record(fact, evidence, dimensions),
        "filing_version_id": fact.filing_version_id,
        "concept_name": fact.concept_name,
        "assurance_level": fact.assurance_level,
        "flow_or_stock": fact.flow_or_stock,
        "raw_value": fact.raw_value,
        "comparative_period": fact.comparative_period,
        "is_restatement": fact.is_restatement,
        "evidence_metadata": [{"id": item.id, "table": item.table_name,
                               "raw_label": item.raw_label, "extractor": item.extractor,
                               "confidence": str(Decimal(item.confidence).quantize(Decimal("0.0001")))}
                              for item in evidence],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def review_evidence_digest(session: Session, fact: FinancialFact) -> str:
    """Bind an approval to the exact reviewed value, units, period and evidence."""
    evidence = _related_records(session, EvidenceFragment, [fact.id], EvidenceFragment.id)
    dimensions = _related_records(session, FactDimension, [fact.id],
                                  FactDimension.dimension_name, FactDimension.dimension_value)
    return _review_digest(fact, evidence[fact.id], dimensions[fact.id])


def has_current_approvals(fact: FinancialFact, reviews, evidence, dimensions) -> bool:
    digest = _review_digest(fact, evidence, dimensions)
    confirmed = {review.reviewer_id.strip() for review in reviews
                 if review.decision in {"confirmed", "corrected"}
                 and review.reviewer_id.strip() and review.evidence_digest == digest}
    return (len(confirmed) >= 2 and bool(evidence) and all(_located(item) for item in evidence)
            and not any(review.decision == "rejected" for review in reviews))


def _located(evidence: EvidenceFragment | None) -> bool:
    if evidence is None or not evidence.row_locator or not evidence.column_locator:
        return False
    if evidence.page_no < 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", evidence.page_sha256):
        return False
    box = evidence.bounding_box or {}
    coordinates = box.get("value") or box.get("row") or box.get("bbox")
    return (isinstance(coordinates, list) and len(coordinates) == 4
            and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) for value in coordinates)
            and coordinates[0] < coordinates[2] and coordinates[1] < coordinates[3])


def require_supported_period(filing: FilingVersion) -> None:
    if (filing.scope != "consolidated" or filing.period_basis != "annual"
            or filing.report_type != "annual"
            or filing.period_start.year != filing.period_end.year
            or (filing.period_start.month, filing.period_start.day) != (1, 1)
            or (filing.period_end.month, filing.period_end.day) != (12, 31)):
        raise ValueError("UNSUPPORTED_STRICT_FILING_SCOPE_OR_PERIOD")


def require_source_assurance(session: Session, filing: FilingVersion) -> None:
    require_supported_period(filing)
    validations = list(session.scalars(select(ValidationResult).where(
        ValidationResult.filing_version_id == filing.id,
    )))
    if any(v.severity == "fatal" and v.status != "passed" for v in validations):
        raise ValueError("STRICT_VALIDATIONS_FAILED")
    passed = {v.rule_code for v in validations if v.status == "passed"}
    if not {"source_sha256", "audit_opinion"}.issubset(passed):
        raise ValueError("STRICT_SOURCE_AND_AUDIT_REQUIRED")


def current_verified_facts(session: Session, filing: FilingVersion) -> dict[str, FinancialFact]:
    require_supported_period(filing)
    facts: dict[str, FinancialFact] = {}
    candidates = list(session.scalars(select(FinancialFact).where(
        FinancialFact.filing_version_id == filing.id,
        FinancialFact.review_status != "superseded",
        FinancialFact.quality_status == "facts_verified",
        FinancialFact.review_status == "verified",
        FinancialFact.comparative_period.is_(False),
        FinancialFact.is_restatement.is_(False),
        FinancialFact.period_start == filing.period_start,
        FinancialFact.period_end == filing.period_end,
    ).order_by(FinancialFact.id)))
    ids = [fact.id for fact in candidates]
    dimensions = _related_records(session, FactDimension, ids,
                                  FactDimension.dimension_name, FactDimension.dimension_value)
    evidence = _related_records(session, EvidenceFragment, ids, EvidenceFragment.id)
    reviews = _related_records(session, FactReview, ids)
    for fact in candidates:
        scopes = [d.dimension_value for d in dimensions[fact.id] if d.dimension_name == "scope"]
        if not scopes or any(scope != filing.scope for scope in scopes):
            raise ValueError("FACT_SCOPE_MISMATCH")
        if fact.concept_code in facts:
            raise ValueError("AMBIGUOUS_CURRENT_FACT")
        if fact.assurance_level != "audited_annual":
            raise ValueError("STRICT_AUDIT_ASSURANCE_REQUIRED")
        if (not Decimal(fact.value).is_finite()
                or fact.unit_scale not in {1, 1000, 10000, 1000000, 100000000}
                or fact.currency != "CNY"):
            raise ValueError("FACT_UNIT_OR_VALUE_INVALID")
        if not has_current_approvals(fact, reviews[fact.id], evidence[fact.id], dimensions[fact.id]):
            raise ValueError("FACT_REVIEW_SNAPSHOT_STALE")
        facts[fact.concept_code] = fact
    return facts


def _snapshot(facts: dict[str, FinancialFact]) -> str:
    value = []
    sessions = defaultdict(list)
    for fact in facts.values():
        if (session := object_session(fact)) is not None:
            sessions[session].append(fact.id)
    related = {}
    for session, ids in sessions.items():
        related[session] = (
            _related_records(session, EvidenceFragment, ids, EvidenceFragment.id),
            _related_records(session, FactDimension, ids,
                             FactDimension.dimension_name, FactDimension.dimension_value),
        )
    for fact in sorted(facts.values(), key=lambda item: (item.concept_code, item.id)):
        session = object_session(fact)
        evidence, dimensions = related[session] if session else ({}, {})
        value.append(_snapshot_record(fact, evidence.get(fact.id, []), dimensions.get(fact.id, [])))
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def current_metric_snapshot(session: Session, filing: FilingVersion) -> str:
    """Validate live audited inputs before displaying a persisted metric."""
    require_source_assurance(session, filing)
    facts = current_verified_facts(session, filing)
    if not facts:
        raise ValueError("STRICT_FACTS_REQUIRED")
    return _snapshot(facts)


def _result(
    session: Session,
    filing_id: int,
    code: str,
    unit: str,
    value: Decimal | None,
    status: str,
    fact_ids: list[int],
    snapshot: str | None,
    explanation: str | None = None,
) -> MetricResult:
    item = session.scalar(
        select(MetricResult).where(
            MetricResult.filing_version_id == filing_id,
            MetricResult.metric_code == code,
            MetricResult.formula_version == FORMULA_VERSION,
        )
    )
    if item is None:
        item = MetricResult(
            filing_version_id=filing_id,
            metric_code=code,
            unit=unit,
            formula_version=FORMULA_VERSION,
            applicability_status=status,
            computation_mode="strict_audited",
            input_fact_ids=fact_ids,
            input_snapshot_hash=snapshot,
            explanation_code=explanation,
        )
        session.add(item)
    else:
        (
            item.value,
            item.applicability_status,
            item.input_fact_ids,
            item.input_snapshot_hash,
            item.explanation_code,
        ) = value, status, fact_ids, snapshot, explanation
    item.value = value
    item.quality_status = "strict_audited"
    filing = session.get(FilingVersion, filing_id)
    item.scope = filing.scope if filing else "consolidated"
    item.period_basis = filing.period_basis if filing else "annual"
    return item


def calculate_metrics(session: Session, filing_id: int) -> list[MetricResult]:
    filing = session.get(FilingVersion, filing_id)
    if filing is None or filing.status not in {"facts_verified", "metrics_ready", "analysis_ready"}:
        raise ValueError("STRICT_FACTS_REQUIRED")
    require_source_assurance(session, filing)
    facts = current_verified_facts(session, filing)
    if not facts:
        raise ValueError("STRICT_FACTS_REQUIRED")
    # Preserve historical values/IDs for audit bundles while preventing an old
    # admission policy from being presented as the current calculation.
    session.execute(update(MetricResult).where(
        MetricResult.filing_version_id == filing_id,
        MetricResult.formula_version != FORMULA_VERSION,
    ).values(applicability_status="superseded", quality_status="superseded"))
    company = session.get(Company, filing.company_id)
    is_financial = bool(company and is_financial_industry(company.industry_code))
    snapshot = _snapshot(facts)
    values: list[MetricResult] = []
    direct = {
        "revenue": ("营业收入", "currency"),
        "operating_profit": ("营业利润", "currency"),
        "net_income_attributable": ("归母净利润", "currency"),
        "cashflow_operations": ("经营活动现金流净额", "currency"),
    }
    for code, (_, unit) in direct.items():
        fact = facts.get(code)
        values.append(
            _result(
                session,
                filing_id,
                code,
                unit,
                fact.value if fact else None,
                "calculated" if fact else "not_available",
                [fact.id] if fact else [],
                snapshot if fact else None,
            )
        )

    def ratio(code: str, numerator: str, denominator: str, unit: str = "ratio") -> None:
        top, bottom = facts.get(numerator), facts.get(denominator)
        if not top or not bottom:
            values.append(
                _result(
                    session,
                    filing_id,
                    code,
                    unit,
                    None,
                    "insufficient_data",
                    [],
                    None,
                    "MISSING_INPUT",
                )
            )
        elif bottom.value <= 0:
            values.append(
                _result(
                    session,
                    filing_id,
                    code,
                    unit,
                    None,
                    "insufficient_data",
                    [top.id, bottom.id],
                    snapshot,
                    "NON_POSITIVE_DENOMINATOR",
                )
            )
        else:
            values.append(
                _result(
                    session,
                    filing_id,
                    code,
                    unit,
                    top.value / bottom.value,
                    "calculated",
                    [top.id, bottom.id],
                    snapshot,
                )
            )

    revenue, cost = facts.get("revenue"), facts.get("operating_cost")
    if is_financial:
        values.append(_result(session, filing_id, "gross_margin", "ratio", None, "not_applicable", [], None, "FINANCIAL_INDUSTRY_NOT_APPLICABLE"))
    elif revenue and cost and revenue.value > 0:
        values.append(
            _result(
                session,
                filing_id,
                "gross_margin",
                "ratio",
                (revenue.value - cost.value) / revenue.value,
                "calculated",
                [revenue.id, cost.id],
                snapshot,
            )
        )
    else:
        values.append(
            _result(
                session,
                filing_id,
                "gross_margin",
                "ratio",
                None,
                "insufficient_data",
                [],
                None,
                "MISSING_OR_NON_POSITIVE_REVENUE",
            )
        )
    if is_financial:
        values.append(_result(session, filing_id, "net_margin", "ratio", None, "not_applicable", [], None, "FINANCIAL_INDUSTRY_SPECIAL_REVENUE"))
        values.append(_result(session, filing_id, "operating_cashflow_to_net_income", "ratio", None, "not_applicable", [], None, "FINANCIAL_INDUSTRY_SPECIAL_CASHFLOW"))
    else:
        ratio("net_margin", "net_income_attributable", "revenue")
        ratio("operating_cashflow_to_net_income", "cashflow_operations", "net_income")
    cfo, capex = facts.get("cashflow_operations"), facts.get("capex_cash")
    if is_financial:
        values.append(_result(session, filing_id, "free_cash_flow", "currency", None, "not_applicable", [], None, "FINANCIAL_INDUSTRY_NOT_APPLICABLE"))
        session.commit()
        return values
    if cfo and capex:
        fcf, issue = free_cash_flow(cfo.value, capex.value)
        values.append(
            _result(
                session,
                filing_id,
                "free_cash_flow",
                "currency",
                fcf,
                "insufficient_data" if issue else "calculated",
                [cfo.id, capex.id],
                snapshot,
                issue or None,
            )
        )
    else:
        values.append(
            _result(
                session,
                filing_id,
                "free_cash_flow",
                "currency",
                None,
                "not_available",
                [],
                None,
                "MISSING_INPUT",
            )
        )
    session.commit()
    return values
