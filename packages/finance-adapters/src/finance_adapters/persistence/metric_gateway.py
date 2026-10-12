from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from finance_adapters.persistence.models import (
    Company,
    EvidenceFragment,
    FactDimension,
    FilingVersion,
    FinancialFact,
    MetricResult,
    ValidationResult,
)

FORMULA_VERSION = "general-non-financial-v1"


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
    for fact in session.scalars(select(FinancialFact).where(
        FinancialFact.filing_version_id == filing.id,
        FinancialFact.review_status != "superseded",
        FinancialFact.quality_status == "facts_verified",
        FinancialFact.review_status == "verified",
        FinancialFact.comparative_period.is_(False),
        FinancialFact.is_restatement.is_(False),
        FinancialFact.period_start == filing.period_start,
        FinancialFact.period_end == filing.period_end,
    ).order_by(FinancialFact.id)):
        scopes = list(session.scalars(select(FactDimension.dimension_value).where(
            FactDimension.fact_id == fact.id, FactDimension.dimension_name == "scope",
        )))
        if not scopes or any(scope != filing.scope for scope in scopes):
            raise ValueError("FACT_SCOPE_MISMATCH")
        if fact.concept_code in facts:
            raise ValueError("AMBIGUOUS_CURRENT_FACT")
        if fact.assurance_level != "audited_annual":
            raise ValueError("STRICT_AUDIT_ASSURANCE_REQUIRED")
        if not Decimal(fact.value).is_finite() or fact.unit_scale <= 0 or fact.currency != "CNY":
            raise ValueError("FACT_UNIT_OR_VALUE_INVALID")
        facts[fact.concept_code] = fact
    return facts


def _snapshot(facts: dict[str, FinancialFact]) -> str:
    value = []
    for fact in sorted(facts.values(), key=lambda item: (item.concept_code, item.id)):
        session = object_session(fact)
        evidence = list(session.scalars(select(EvidenceFragment).where(
            EvidenceFragment.fact_id == fact.id,
        ).order_by(EvidenceFragment.id))) if session else []
        dimensions = list(session.scalars(select(FactDimension).where(
            FactDimension.fact_id == fact.id,
        ).order_by(FactDimension.dimension_name, FactDimension.dimension_value))) if session else []
        value.append({
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
        })
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


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
    company = session.get(Company, filing.company_id)
    financial_industries = {"bank", "insurance", "broker", "finance", "J66", "J67", "J68"}
    is_financial = bool(company and (company.industry_code in financial_industries or company.industry_code.lower() in financial_industries))
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
        values.append(
            _result(
                session,
                filing_id,
                "free_cash_flow",
                "currency",
                cfo.value - capex.value,
                "calculated",
                [cfo.id, capex.id],
                snapshot,
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
