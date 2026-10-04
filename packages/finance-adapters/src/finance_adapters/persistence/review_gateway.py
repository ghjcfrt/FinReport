from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.persistence.models import (
    EvidenceFragment,
    FactReview,
    FilingVersion,
    FinancialFact,
    ValidationResult,
)


def recompute_filing_quality(session: Session, filing_id: int) -> str:
    filing = session.get(FilingVersion, filing_id)
    facts = list(
        session.scalars(select(FinancialFact).where(FinancialFact.filing_version_id == filing_id))
    )
    if not facts:
        filing.status = "needs_review"
        session.commit()
        return filing.status
    all_verified = True
    for fact in facts:
        reviews = list(session.scalars(select(FactReview).where(FactReview.fact_id == fact.id)))
        evidence = session.scalar(
            select(EvidenceFragment).where(EvidenceFragment.fact_id == fact.id)
        )
        confirmed = {
            review.reviewer_id
            for review in reviews
            if review.decision in {"confirmed", "corrected"}
        }
        rejected = any(review.decision == "rejected" for review in reviews)
        has_position = evidence is not None and bool(
            evidence.row_locator and evidence.column_locator and evidence.page_sha256
        )
        if len(confirmed) >= 2 and not rejected and has_position:
            fact.review_status, fact.quality_status = "verified", "facts_verified"
        else:
            fact.review_status, fact.quality_status = "needs_review", "candidate"
            all_verified = False

    session.execute(
        ValidationResult.__table__.delete().where(ValidationResult.filing_version_id == filing_id)
    )
    by_code = {fact.concept_code: fact for fact in facts if fact.quality_status == "facts_verified"}
    balance_codes = {"total_assets", "total_liabilities", "total_equity"}
    balance_pass = balance_codes.issubset(by_code)
    if balance_pass:
        actual = by_code["total_assets"].value
        expected = by_code["total_liabilities"].value + by_code["total_equity"].value
        # In a full parser, tolerance comes from declared table display unit. Candidate parser has none -> exact only.
        balance_pass = actual == expected
        session.add(
            ValidationResult(
                filing_version_id=filing_id,
                rule_code="balance_sheet_balance",
                severity="fatal",
                status="passed" if balance_pass else "failed",
                actual_value=str(actual),
                expected_value=str(expected),
                tolerance="0; declared display unit unavailable",
            )
        )
    else:
        session.add(
            ValidationResult(
                filing_version_id=filing_id,
                rule_code="balance_sheet_balance",
                severity="fatal",
                status="failed",
                actual_value="missing_required_fact",
                expected_value="assets=liabilities+equity",
                tolerance="unknown",
            )
        )
    filing.status = "facts_verified" if all_verified and balance_pass else "needs_review"
    session.commit()
    return filing.status
