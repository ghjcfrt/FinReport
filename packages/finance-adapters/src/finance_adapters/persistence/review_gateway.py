from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.persistence.metric_gateway import require_source_assurance
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


def _located(evidence: EvidenceFragment | None) -> bool:
    if evidence is None or not evidence.row_locator or not evidence.column_locator:
        return False
    if evidence.page_no < 1 or not re.fullmatch(r'[0-9a-fA-F]{64}', evidence.page_sha256):
        return False
    box = evidence.bounding_box or {}
    coordinates = box.get('value') or box.get('row') or box.get('bbox')
    return (isinstance(coordinates, list) and len(coordinates) == 4
            and all(isinstance(v, (int, float)) for v in coordinates)
            and coordinates[0] < coordinates[2] and coordinates[1] < coordinates[3])


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
    all_verified = bool(facts)
    by_code = {}
    metadata_valid = bool(facts)
    try:
        require_source_assurance(session, filing)
    except ValueError:
        metadata_valid = False
    for fact in facts:
        reviews = list(session.scalars(select(FactReview).where(FactReview.fact_id == fact.id)))
        evidence = session.scalar(select(EvidenceFragment).where(EvidenceFragment.fact_id == fact.id))
        confirmed = {r.reviewer_id.strip() for r in reviews
                     if r.decision in {'confirmed', 'corrected'} and r.reviewer_id.strip()}
        rejected = any(r.decision == 'rejected' for r in reviews)
        scopes = list(session.scalars(select(FactDimension.dimension_value).where(
            FactDimension.fact_id == fact.id, FactDimension.dimension_name == 'scope',
        )))
        valid = (fact.period_start == filing.period_start and fact.period_end == filing.period_end
                 and not fact.comparative_period and not fact.is_restatement
                 and fact.currency == 'CNY' and fact.unit_scale in {1, 1000, 10000, 1000000, 100000000}
                 and Decimal(fact.value).is_finite() and bool(scopes)
                 and fact.assurance_level == 'audited_annual'
                 and all(scope == filing.scope for scope in scopes)
                 and fact.concept_code not in by_code)
        metadata_valid = metadata_valid and valid
        if len(confirmed) >= 2 and not rejected and _located(evidence) and valid:
            fact.review_status, fact.quality_status = 'verified', 'facts_verified'
        else:
            fact.review_status, fact.quality_status = 'needs_review', 'candidate'
            all_verified = False
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
