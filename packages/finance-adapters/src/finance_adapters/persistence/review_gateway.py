from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.persistence.metric_gateway import (
    _located,
    _related_records,
    has_current_approvals,
    require_source_assurance,
)
from finance_adapters.persistence.models import (
    EvidenceFragment,
    FactDimension,
    FactReview,
    FilingVersion,
    FinancialFact,
    ValidationResult,
)

QUALITY_RULES = {
    'balance_sheet_balance', 'cashflow_reconciliation', 'cashflow_net_change',
    'fact_period_scope_units',
}


def _comparative_metadata_matches(fact: FinancialFact, filing: FilingVersion, evidence) -> bool:
    if not evidence:
        return False
    for item in evidence:
        metadata = item.bounding_box or {}
        header = re.sub(r"\s+", "", item.column_locator)
        dates = re.findall(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", header)
        if (metadata.get("period_role") != "comparative"
                or metadata.get("column_role") != f"year:{fact.period_end.year}"
                or metadata.get("comparability_basis") != "same_report_consolidated_annual_columns"
                or metadata.get("scope") != filing.scope
                or metadata.get("unit_scale") != fact.unit_scale
                or metadata.get("value_basis") != "normalized_currency"
                or metadata.get("accounting_standard") != filing.accounting_standard
                or metadata.get("is_restatement") is not fact.is_restatement
                or not item.table_name.startswith("合并")
                or str(fact.period_end.year) not in header
                or ("重述" in header or "重列" in header) != fact.is_restatement):
            return False
        if dates and dates != [(str(fact.period_end.year), "12", "31")]:
            return False
        if fact.flow_or_stock == "stock" and not dates:
            return False
    return True


def recompute_filing_quality(session: Session, filing_id: int) -> str:
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise ValueError('FILING_NOT_FOUND')
    # Recompute our own findings after corrections; retain source/parser failures.
    session.execute(ValidationResult.__table__.delete().where(
        ValidationResult.filing_version_id == filing_id,
        ValidationResult.rule_code.in_(QUALITY_RULES),
    ))
    facts = list(session.scalars(select(FinancialFact).where(
        FinancialFact.filing_version_id == filing_id,
        FinancialFact.review_status != 'superseded',
        FinancialFact.quality_status != 'superseded',
    )))
    ids = [fact.id for fact in facts]
    dimensions = _related_records(session, FactDimension, ids,
                                  FactDimension.dimension_name, FactDimension.dimension_value)
    evidence_by_fact = _related_records(session, EvidenceFragment, ids, EvidenceFragment.id)
    reviews_by_fact = _related_records(session, FactReview, ids)
    current_facts = [fact for fact in facts if not fact.comparative_period]
    all_verified = bool(current_facts)
    by_code = {}
    comparative_codes = set()
    metadata_valid = bool(current_facts)
    source_valid = True
    try:
        require_source_assurance(session, filing)
    except ValueError:
        metadata_valid = False
        source_valid = False
    for fact in facts:
        reviews, evidence = reviews_by_fact[fact.id], evidence_by_fact[fact.id]
        scopes = [d.dimension_value for d in dimensions[fact.id] if d.dimension_name == 'scope']
        codes = comparative_codes if fact.comparative_period else by_code
        period_valid = (
            fact.period_start == date(filing.period_end.year - 1, 1, 1)
            and fact.period_end == date(filing.period_end.year - 1, 12, 31)
            and _comparative_metadata_matches(fact, filing, evidence)
        ) if fact.comparative_period else (
            fact.period_start == filing.period_start and fact.period_end == filing.period_end
            and not fact.is_restatement
        )
        valid = (source_valid and period_valid
                 and fact.currency == 'CNY' and fact.unit_scale in {1, 1000, 10000, 1000000, 100000000}
                 and Decimal(fact.value).is_finite() and bool(scopes)
                 and fact.assurance_level == 'audited_annual'
                 and all(scope == filing.scope for scope in scopes)
                 and fact.concept_code not in codes)
        if not fact.comparative_period:
            metadata_valid = metadata_valid and valid
        if (valid and any(_located(item) for item in evidence)
                and has_current_approvals(fact, reviews, evidence, dimensions[fact.id])):
            fact.review_status, fact.quality_status = 'verified', 'facts_verified'
        else:
            fact.review_status, fact.quality_status = 'needs_review', 'candidate'
            if not fact.comparative_period:
                all_verified = False
        if fact.comparative_period:
            comparative_codes.add(fact.concept_code)
        else:
            by_code[fact.concept_code] = fact

    def record(code, passed, actual, expected, tolerance='0'):
        session.add(ValidationResult(filing_version_id=filing_id, rule_code=code,
                    rule_version='v2', severity='fatal', status='passed' if passed else 'failed',
                    actual_value=str(actual), expected_value=str(expected), tolerance=tolerance))
        return passed

    checks = [record('fact_period_scope_units', metadata_valid,
                     'consistent' if metadata_valid else 'missing_or_mismatched_metadata',
                     'current period; declared scope; CNY; normalized yuan')]
    def equation(code, left_code, right_codes):
        required = {left_code, *right_codes}
        if not required.issubset(by_code):
            return record(code, False, 'missing_required_fact', '+'.join(right_codes))
        left = by_code[left_code].value
        right = sum((by_code[c].value for c in right_codes), Decimal(0))
        # Each displayed amount can round by half one original display unit.
        tolerance = sum((Decimal(by_code[c].unit_scale) / 2 for c in required), Decimal(0))
        return record(code, abs(left - right) <= tolerance, left, right, str(tolerance))
    checks.append(equation('balance_sheet_balance', 'total_assets',
                           ['total_liabilities', 'total_equity']))
    checks.append(equation('cashflow_reconciliation', 'cash_end',
                           ['cash_begin', 'cashflow_operations', 'cashflow_investing',
                            'cashflow_financing', 'cashflow_exchange_effect']))
    if 'cash_net_change' in by_code:
        checks.append(equation('cashflow_net_change', 'cash_net_change',
                               ['cashflow_operations', 'cashflow_investing',
                                'cashflow_financing', 'cashflow_exchange_effect']))
    session.flush()
    fatal_failed = session.scalar(select(ValidationResult.id).where(
        ValidationResult.filing_version_id == filing_id,
        ValidationResult.severity == 'fatal', ValidationResult.status != 'passed',
    )) is not None
    filing.status = 'facts_verified' if all_verified and all(checks) and not fatal_failed else 'needs_review'
    session.commit()
    return filing.status
