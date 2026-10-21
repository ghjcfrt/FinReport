"""Create separately reviewable prior-period facts from already measured columns."""
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date
from decimal import Decimal

from finance_domain.filings.amounts import normalize_monetary_value
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.parsing.statement_extractor import (
    StatementExtractor,
    compact,
    normalized_label,
    parse_number,
)
from finance_adapters.persistence.models import (
    EvidenceFragment,
    FactDimension,
    FactReview,
    FilingVersion,
    FinancialFact,
)

COMPARATIVE_VERSION = "native-comparative-v2"


def comparative_candidates(extractor: StatementExtractor, target_year: int) -> list[dict]:
    """Use the same row/unit as a current fact; never guess a column or a scope."""
    results = []
    for current in extractor.facts.values():
        table = extractor.tables.get(current.page_no)
        if table is None or not table.table_name.startswith("合并"):
            continue
        current_columns = [i for i, role in enumerate(table.column_roles)
                           if role == f"year:{target_year}"]
        prior_columns = [i for i, role in enumerate(table.column_roles)
                         if role == f"year:{target_year - 1}"]
        if len(current_columns) != 1 or len(prior_columns) != 1:
            continue
        current_column, prior_column = current_columns[0], prior_columns[0]
        header = table.headers[prior_column]
        dates = re.findall(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", compact(header))
        if current.statement_type == "资产负债表" and dates != [(str(target_year - 1), "12", "31")]:
            continue
        if dates and dates != [(str(target_year - 1), "12", "31")]:
            continue
        matches = [row for row in table.rows
                   if normalized_label(row.raw_label) == normalized_label(current.raw_label)
                   and len(row.cells) > max(current_column, prior_column)
                   and len(row.column_bboxes) > prior_column
                   and len(row.value_bboxes) > prior_column
                   and row.value_bboxes[prior_column] is not None
                   and parse_number(row.cells[current_column]) == current.value]
        if len(matches) != 1:
            continue
        row = matches[0]
        value = parse_number(row.cells[prior_column])
        if value is None or not value.is_finite():
            continue
        results.append({
            "current": current, "value": normalize_monetary_value(value, current.unit_scale),
            "raw_value": row.cells[prior_column], "column_label": header,
            "period_start": date(target_year - 1, 1, 1),
            "period_end": date(target_year - 1, 12, 31),
            "is_restatement": "重述" in header or "重列" in header,
            "column_role": table.column_roles[prior_column],
            "row_bbox": list(row.column_bboxes[prior_column]),
            "value_bbox": list(row.value_bboxes[prior_column]),
        })
    return results


def persist_comparative_candidates(
    session: Session, filing: FilingVersion, job_id: int,
    extractor: StatementExtractor, *, audited: bool,
) -> int:
    """No commit: the extraction transaction owns completion and cancellation."""
    count = 0
    for candidate in comparative_candidates(extractor, filing.period_end.year):
        current = candidate["current"]
        previous = list(session.scalars(select(FinancialFact).where(
            FinancialFact.filing_version_id == filing.id,
            FinancialFact.concept_code == current.concept_code,
            FinancialFact.period_start == candidate["period_start"],
            FinancialFact.period_end == candidate["period_end"],
        )))
        if any(item.quality_status == "facts_verified" or
               (item.review_status != "superseded" and session.scalar(
                   select(FactReview.id).where(FactReview.fact_id == item.id)) is not None)
               for item in previous):
            continue
        for previous_fact in previous:
            if (previous_fact.comparative_period
                    and previous_fact.quality_status == "candidate"
                    and previous_fact.review_status == "needs_review"
                    and session.scalar(select(FactReview.id).where(
                        FactReview.fact_id == previous_fact.id,
                    )) is None):
                previous_fact.quality_status = previous_fact.review_status = "superseded"
        fact = FinancialFact(
            filing_version_id=filing.id, concept_code=current.concept_code,
            concept_name=current.concept_name, statement_type=current.statement_type,
            period_start=candidate["period_start"], period_end=candidate["period_end"],
            value=candidate["value"], currency=current.currency,
            unit_scale=current.unit_scale, flow_or_stock=current.flow_or_stock,
            raw_value=candidate["raw_value"], comparative_period=True,
            is_restatement=candidate["is_restatement"],
            quality_status="candidate", review_status="needs_review",
            assurance_level="audited_annual" if audited else "unknown",
            revision_no=max((item.revision_no for item in previous), default=0) + 1,
        )
        session.add(fact)
        session.flush()
        session.add(EvidenceFragment(
            fact_id=fact.id, page_no=current.page_no, table_name=current.table_name,
            row_locator=current.raw_label, raw_label=current.raw_label,
            column_locator=candidate["column_label"], raw_value=candidate["raw_value"],
            bounding_box={
                "source": "text-layer", "row": candidate["row_bbox"],
                "value": candidate["value_bbox"],
                "extraction_job_id": job_id, "scope": filing.scope,
                "unit_scale": current.unit_scale, "unit_page_no": current.unit_page_no,
                "unit_text": current.unit_text, "value_basis": "normalized_currency",
                "period_role": "comparative", "column_role": candidate["column_role"],
                "accounting_standard": filing.accounting_standard,
                "is_restatement": candidate["is_restatement"],
                "comparability_basis": "same_report_consolidated_annual_columns",
                "label_layout": asdict(current.label_layout) if current.label_layout else None,
            },
            page_sha256=current.page_sha256, confidence=Decimal("0.9000"),
            extractor=COMPARATIVE_VERSION,
        ))
        session.add_all([
            FactDimension(fact_id=fact.id, dimension_name="scope", dimension_value=filing.scope),
            FactDimension(fact_id=fact.id, dimension_name="amount_basis", dimension_value="normalized_currency"),
            FactDimension(fact_id=fact.id, dimension_name="comparability_basis",
                          dimension_value="same_report_consolidated_annual_columns"),
        ])
        count += 1
    session.flush()
    return count
