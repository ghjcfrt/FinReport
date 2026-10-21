from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext

from finance_domain.decimal_policy import financial_context
from finance_domain.filings.concepts import validate_concept_metadata
from finance_domain.metrics.definitions import (
    METRIC_DEFINITIONS,
    NON_FINANCIAL_METRICS,
    industry_classification,
)
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

FORMULA_VERSION = "general-non-financial-v4"


def _related_records(session: Session, model, fact_ids: list[int], *ordering) -> dict:
    """Batch within conservative bind limits; no cross-request ORM cache."""
    grouped = defaultdict(list)
    for start in range(0, len(fact_ids), 500):
        query = select(model).where(model.fact_id.in_(fact_ids[start:start + 500]))
        for row in session.scalars(query.order_by(*ordering)):
            grouped[row.fact_id].append(row)
    return grouped


def _snapshot_record(fact: FinancialFact, evidence, dimensions) -> dict:
    with localcontext(financial_context()):
        normalized_value = str(Decimal(fact.value).quantize(Decimal("0.0001")))
    return {
        "id": fact.id,
        "code": fact.concept_code,
        "value": normalized_value,
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
    with localcontext(financial_context()):
        evidence_metadata = [{"id": item.id, "table": item.table_name,
                              "raw_label": item.raw_label, "extractor": item.extractor,
                              "confidence": str(Decimal(item.confidence).quantize(Decimal("0.0001")))}
                             for item in evidence]
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
        "evidence_metadata": evidence_metadata,
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
    from finance_adapters.persistence.mapping_gateway import validate_ocr_source_assurance

    validate_ocr_source_assurance(session, filing)
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
    from finance_adapters.persistence.auto_validation_gateway import (
        auto_validated_facts,
        uses_automatic_policy,
    )
    if uses_automatic_policy(filing):
        require_source_assurance(session, filing)
        return auto_validated_facts(session, filing)
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
        if not validate_concept_metadata(fact.concept_code, fact.statement_type, fact.flow_or_stock):
            raise ValueError("FACT_CONCEPT_METADATA_MISMATCH")
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
    classifications = []
    filing_policies = {}
    for session, ids in sessions.items():
        filing_ids = {fact.filing_version_id for fact in facts.values() if object_session(fact) is session}
        rows = session.execute(select(
            FilingVersion.id, FilingVersion.company_id, Company.industry_code,
            Company.industry_taxonomy_version, FilingVersion.fact_validation_policy,
        ).outerjoin(Company, FilingVersion.company_id == Company.id)
          .where(FilingVersion.id.in_(filing_ids)))
        for filing_id, company_id, industry_code, taxonomy, policy in rows:
            filing_policies[filing_id] = policy or "legacy_manual_v1"
            classifications.append({"filing_id": filing_id, "company_id": company_id,
                                    "industry_code": industry_code,
                                    "industry_taxonomy_version": taxonomy})
    payload = {"facts": value, "classification": sorted(classifications, key=lambda row: row["filing_id"])}
    # Preserve legacy snapshot bytes; automatic inputs bind their versioned
    # source certificate as well as the existing financial snapshot.
    from finance_adapters.persistence.models import FactAutoValidationRun
    from finance_adapters.providers.fact_auto_validation import POLICY_VERSION
    certificates = []
    for session, ids in sessions.items():
        filing_ids = {fact.filing_version_id for fact in facts.values() if object_session(fact) is session}
        for filing_id in filing_ids:
            if filing_policies.get(filing_id, "legacy_manual_v1") != POLICY_VERSION:
                continue
            certificate = session.scalar(select(FactAutoValidationRun).where(
                FactAutoValidationRun.filing_version_id == filing_id,
            ).order_by(FactAutoValidationRun.id.desc()).limit(1))
            if certificate is None:
                raise ValueError("AUTO_FACT_RECEIPT_REQUIRED")
            certificates.append({"filing_id": filing_id, "policy": certificate.policy_version,
                                 "input": certificate.input_snapshot_hash, "output": certificate.output_hash})
    if certificates:
        payload["automatic_admission"] = sorted(certificates, key=lambda row: row["filing_id"])
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def current_metric_snapshot(session: Session, filing: FilingVersion) -> str:
    """Validate live audited inputs before displaying a persisted metric."""
    require_source_assurance(session, filing)
    facts = metric_facts(session, filing)
    if not facts:
        raise ValueError("STRICT_FACTS_REQUIRED")
    return _snapshot(facts)


def verified_comparative_facts(
    session: Session, filing: FilingVersion,
) -> tuple[dict[str, FinancialFact], list[int]]:
    """Admit same-report prior columns with the same evidence/review gate as comparison."""
    from finance_adapters.persistence.auto_validation_gateway import (
        auto_validated_facts,
        uses_automatic_policy,
    )
    if uses_automatic_policy(filing):
        require_source_assurance(session, filing)
        verified = auto_validated_facts(session, filing, comparative=True)
        admitted_ids = {fact.id for fact in verified.values()}
        excluded = [fact.id for fact in session.scalars(select(FinancialFact).where(
            FinancialFact.filing_version_id == filing.id,
            FinancialFact.comparative_period.is_(True),
            FinancialFact.review_status != "superseded", FinancialFact.quality_status != "superseded",
        ).order_by(FinancialFact.id)) if fact.id not in admitted_ids]
        return verified, excluded
    from finance_adapters.persistence.review_gateway import _comparative_metadata_matches

    prior, excluded, seen = {}, [], set()
    prior_start, prior_end = date(filing.period_start.year - 1, 1, 1), date(filing.period_end.year - 1, 12, 31)
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
        evidence = list(session.scalars(select(EvidenceFragment).where(EvidenceFragment.fact_id == fact.id)))
        reviews = list(session.scalars(select(FactReview).where(FactReview.fact_id == fact.id)))
        scopes = [item.dimension_value for item in dimensions if item.dimension_name == "scope"]
        valid = (
            fact.quality_status == "facts_verified" and fact.review_status == "verified"
            and fact.period_start == prior_start and fact.period_end == prior_end
            and fact.currency == "CNY" and fact.unit_scale in {1, 1000, 10000, 1000000, 100000000}
            and Decimal(fact.value).is_finite() and fact.assurance_level == "audited_annual"
            and bool(scopes) and all(scope == filing.scope for scope in scopes)
            and has_current_approvals(fact, reviews, evidence, dimensions)
            and _comparative_metadata_matches(fact, filing, evidence)
            and validate_concept_metadata(fact.concept_code, fact.statement_type, fact.flow_or_stock)
        )
        if valid:
            prior[fact.concept_code] = fact
        else:
            excluded.append(fact.id)
    return prior, excluded


def metric_facts(session: Session, filing: FilingVersion) -> dict[str, FinancialFact]:
    """Keep actual prior periods intact; aliases identify formula input roles only."""
    current = current_verified_facts(session, filing)
    if not current:
        return {}
    prior, _ = verified_comparative_facts(session, filing)
    needed = {code for definition in METRIC_DEFINITIONS.values() for code in definition.beginning_concepts}
    return {**current, **{"beginning_" + code: fact for code, fact in prior.items() if code in needed}}


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
    facts = metric_facts(session, filing)
    if not facts:
        raise ValueError("STRICT_FACTS_REQUIRED")
    # Preserve historical values/IDs for audit bundles while preventing an old
    # admission policy from being presented as the current calculation.
    session.execute(update(MetricResult).where(
        MetricResult.filing_version_id == filing_id,
        MetricResult.formula_version != FORMULA_VERSION,
    ).values(applicability_status="superseded", quality_status="superseded"))
    company = session.get(Company, filing.company_id)
    classification = industry_classification(company.industry_code if company else None)
    snapshot = _snapshot(facts)
    values: list[MetricResult] = []
    for code, definition in METRIC_DEFINITIONS.items():
        inputs = [facts.get(concept) for concept in definition.input_keys]
        fact_ids = [fact.id for fact in inputs if fact is not None]
        explanation = None
        value = None
        if code in NON_FINANCIAL_METRICS and classification != "non_financial":
            status = "not_applicable" if classification == "financial" else "insufficient_data"
            explanation = ("FINANCIAL_INDUSTRY_NOT_APPLICABLE" if classification == "financial"
                           else "UNKNOWN_INDUSTRY_CLASSIFICATION")
        elif any(fact is None for fact in inputs):
            status = "not_available" if definition.operation == "identity" else "insufficient_data"
            explanation = ("REVIEWED_BEGINNING_BALANCE_REQUIRED" if definition.beginning_concepts
                           and any(facts.get("beginning_" + concept) is None for concept in definition.beginning_concepts)
                           else "MISSING_INPUT")
        else:
            try:
                value = definition.evaluate({key: Decimal(facts[key].value) for key in definition.input_keys})
                status = "calculated"
            except ValueError as exc:
                status, explanation = "insufficient_data", str(exc)
        values.append(_result(session, filing_id, code, definition.unit, value, status,
                              fact_ids, snapshot, explanation))
    session.commit()
    return values
