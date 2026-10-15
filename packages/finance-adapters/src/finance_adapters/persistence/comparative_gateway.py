"""Auditable same-report comparison, separate from current-period metric IDs."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext

from finance_domain.metrics.definitions import METRIC_DEFINITIONS, is_financial_industry
from finance_domain.metrics.period_changes import period_change
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.persistence.audit import stable_hash
from finance_adapters.persistence.metric_gateway import (
    _snapshot,
    current_verified_facts,
    has_current_approvals,
    require_source_assurance,
)
from finance_adapters.persistence.models import (
    Company,
    EvidenceFragment,
    FactDimension,
    FactReview,
    FilingVersion,
    FinancialFact,
)
from finance_adapters.persistence.review_gateway import _comparative_metadata_matches

COMPARISON_VERSION = "annual-period-comparison-v1"


def calculate_period_comparison(inputs: dict) -> dict:
    """Pure replay of an admitted input bundle. Hashes do not prove provenance."""
    if inputs.get("version") != COMPARISON_VERSION:
        raise ValueError("UNSUPPORTED_PERIOD_COMPARISON_VERSION")
    current_period, prior_period = inputs["current_period"], inputs["prior_period"]
    year = date.fromisoformat(current_period["period_end"]).year
    if (current_period != {"period_start": f"{year}-01-01", "period_end": f"{year}-12-31"}
            or prior_period != {"period_start": f"{year-1}-01-01", "period_end": f"{year-1}-12-31"}
            or inputs.get("scope") != "consolidated" or inputs.get("currency") != "CNY"
            or inputs.get("period_basis") != "annual"):
        raise ValueError("PERIOD_COMPARABILITY_MISMATCH")
    current = {item["concept_code"]: Decimal(item["value"]) for item in inputs["current_facts"]}
    prior = {item["concept_code"]: Decimal(item["value"]) for item in inputs["prior_facts"]}
    if (len(current) != len(inputs["current_facts"]) or len(prior) != len(inputs["prior_facts"])
            or any(not value.is_finite() for value in [*current.values(), *prior.values()])):
        raise ValueError("AMBIGUOUS_OR_INVALID_PERIOD_FACTS")
    for facts, period in ((inputs["current_facts"], current_period), (inputs["prior_facts"], prior_period)):
        for fact in facts:
            if (fact.get("period_start") != period["period_start"]
                    or fact.get("period_end") != period["period_end"]
                    or fact.get("currency") != inputs["currency"]
                    or fact.get("scope") != inputs["scope"]
                    or fact.get("value_basis") != "normalized_currency"
                    or fact.get("source_unit_scale") not in {1, 1000, 10000, 1000000, 100000000}
                    or not isinstance(fact.get("is_restatement"), bool)):
                raise ValueError("PERIOD_FACT_METADATA_MISMATCH")
    if any(fact["is_restatement"] for fact in inputs["current_facts"]):
        raise ValueError("CURRENT_RESTATEMENT_UNSUPPORTED")
    items = []
    for code, definition in METRIC_DEFINITIONS.items():
        result = {"metric_code": code, "name": definition.name,
                  "formula": definition.formula, "unit": definition.unit,
                  "current_fact_ids": [f["id"] for f in inputs["current_facts"]
                                       if f["concept_code"] in definition.required_concepts],
                  "prior_fact_ids": [f["id"] for f in inputs["prior_facts"]
                                     if f["concept_code"] in definition.required_concepts]}
        if (inputs.get("financial_industry") and code in {
            "gross_margin", "net_margin", "operating_cashflow_to_net_income", "free_cash_flow"
        }):
            result.update(status="not_applicable", explanation_code="FINANCIAL_INDUSTRY_NOT_APPLICABLE")
        elif not set(definition.required_concepts).issubset(current.keys() & prior.keys()):
            result.update(status="insufficient_data", explanation_code="MISSING_COMPARABLE_INPUT")
        else:
            try:
                with localcontext() as context:
                    context.prec = 48
                    result.update(period_change(definition.evaluate(current), definition.evaluate(prior), definition.unit))
                    if code in {"gross_margin", "net_margin"}:
                        result["percentage_point_change"] = str(Decimal(result["absolute_change"]) * 100)
                        result["change_unit"] = "percentage_points"
            except ValueError as exc:
                result.update(status="not_comparable", explanation_code=str(exc))
        items.append(result)
    return {"version": COMPARISON_VERSION, "input_hash": stable_hash(inputs), "metrics": items,
            "basis": "same_report_consolidated_annual_columns",
            "uses_restated_base": any(item["is_restatement"] for item in inputs["prior_facts"]),
            "limitations": ["金额同比仅在正基期下计算；负或零基期不输出增长率。",
                            "利润率变动使用百分点；现金流利润比使用倍数差，不作因果解释。",
                            "比较基期以本报告列报口径为准，不与上次报告未重述数混用。"]}


def _fact_input(session: Session, fact: FinancialFact) -> dict:
    evidence = list(session.scalars(select(EvidenceFragment).where(
        EvidenceFragment.fact_id == fact.id).order_by(EvidenceFragment.id)))
    scopes = set(session.scalars(select(FactDimension.dimension_value).where(
        FactDimension.fact_id == fact.id, FactDimension.dimension_name == "scope")))
    if len(scopes) != 1:
        raise ValueError("FACT_SCOPE_MISMATCH")
    return {"id": fact.id, "concept_code": fact.concept_code, "value": str(fact.value),
            "scope": scopes.pop(), "value_basis": "normalized_currency",
            "currency": fact.currency, "source_unit_scale": fact.unit_scale,
            "period_start": fact.period_start.isoformat(), "period_end": fact.period_end.isoformat(),
            "is_restatement": fact.is_restatement, "revision_no": fact.revision_no,
            "evidence": [{"id": e.id, "page_no": e.page_no, "page_sha256": e.page_sha256,
                          "row": e.row_locator, "column": e.column_locator,
                          "raw_value": e.raw_value, "bbox": e.bounding_box} for e in evidence]}


def build_period_comparison(session: Session, filing_id: int) -> dict:
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise ValueError("FILING_NOT_FOUND")
    if filing.status not in {"facts_verified", "metrics_ready", "analysis_ready"}:
        raise ValueError("STRICT_FACTS_REQUIRED")
    require_source_assurance(session, filing)
    current = current_verified_facts(session, filing)
    if not current:
        raise ValueError("STRICT_FACTS_REQUIRED")
    if session.scalar(select(FilingVersion.id).where(FilingVersion.supersedes_filing_version_id == filing.id)):
        raise ValueError("SUPERSEDED_FILING")
    prior_start = date(filing.period_start.year - 1, 1, 1)
    prior_end = date(filing.period_end.year - 1, 12, 31)
    prior = {}
    seen = set()
    excluded = []
    for fact in session.scalars(select(FinancialFact).where(
        FinancialFact.filing_version_id == filing.id,
        FinancialFact.comparative_period.is_(True),
        FinancialFact.review_status != "superseded",
        FinancialFact.quality_status != "superseded",
    ).order_by(FinancialFact.id)):
        if fact.concept_code in seen:
            raise ValueError("AMBIGUOUS_COMPARATIVE_FACT")
        seen.add(fact.concept_code)
        dimensions = list(session.scalars(select(FactDimension).where(FactDimension.fact_id == fact.id)))
        scopes = [d.dimension_value for d in dimensions if d.dimension_name == "scope"]
        evidence = list(session.scalars(select(EvidenceFragment).where(EvidenceFragment.fact_id == fact.id)))
        reviews = list(session.scalars(select(FactReview).where(FactReview.fact_id == fact.id)))
        valid = (
            fact.quality_status == "facts_verified" and fact.review_status == "verified"
            and fact.period_start == prior_start and fact.period_end == prior_end
            and fact.currency == "CNY" and fact.unit_scale in {1, 1000, 10000, 1000000, 100000000}
            and Decimal(fact.value).is_finite() and fact.assurance_level == "audited_annual"
            and bool(scopes) and all(scope == filing.scope for scope in scopes)
            and has_current_approvals(fact, reviews, evidence, dimensions)
            and _comparative_metadata_matches(fact, filing, evidence)
        )
        if valid:
            prior[fact.concept_code] = fact
        else:
            excluded.append(fact.id)
    company = session.get(Company, filing.company_id)
    inputs = {"version": COMPARISON_VERSION, "filing_version_id": filing.id,
              "source_sha256": filing.file_sha256, "accounting_standard": filing.accounting_standard,
              "scope": filing.scope, "currency": "CNY", "period_basis": "annual",
              "financial_industry": bool(company and is_financial_industry(company.industry_code)),
              "current_period": {"period_start": filing.period_start.isoformat(),
                                 "period_end": filing.period_end.isoformat()},
              "prior_period": {"period_start": prior_start.isoformat(), "period_end": prior_end.isoformat()},
              "current_facts": [_fact_input(session, fact) for fact in current.values()],
              "prior_facts": [_fact_input(session, fact) for fact in prior.values()]}
    result = calculate_period_comparison(inputs)
    return {**result, "inputs": inputs, "current_snapshot_hash": _snapshot(current),
            "prior_snapshot_hash": _snapshot(prior), "excluded_prior_fact_ids": excluded,
            "status": "available" if prior else "comparative_review_required"}
