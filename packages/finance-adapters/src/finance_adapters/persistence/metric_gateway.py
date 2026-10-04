from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.persistence.models import Company, FilingVersion, FinancialFact, MetricResult

FORMULA_VERSION = "general-non-financial-v1"


def _snapshot(facts: dict[str, FinancialFact]) -> str:
    value = [
        {
            "id": fact.id,
            "code": fact.concept_code,
            "value": str(fact.value),
            "revision": fact.revision_no,
        }
        for fact in facts.values()
    ]
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
    if filing is None or filing.status != "facts_verified":
        raise ValueError("STRICT_FACTS_REQUIRED")
    if filing.scope not in {"consolidated", "parent"} or filing.period_basis not in {"annual", "semi_annual"}:
        raise ValueError("UNSUPPORTED_FILING_SCOPE_OR_PERIOD")
    facts = {
        fact.concept_code: fact
        for fact in session.scalars(
            select(FinancialFact).where(
                FinancialFact.filing_version_id == filing_id,
                FinancialFact.quality_status == "facts_verified",
            )
        )
    }
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
