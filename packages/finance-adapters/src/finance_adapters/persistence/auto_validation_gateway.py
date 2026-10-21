"""Explicit native-fact policy, durable receipts and fail-closed strict reads.

The independent native verifier certifies supported disclosure bindings. Source
assurance and financial equations remain separate gates. No reviewer records
are manufactured, and an automatic policy never falls back to legacy approval.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.models import (
    AnalysisRun,
    EvidenceFragment,
    ExtractionJob,
    FactAutoValidationRun,
    FactDimension,
    FilingVersion,
    FinancialFact,
    MetricResult,
)
from finance_adapters.providers.fact_auto_validation import (
    POLICY_VERSION,
    FactAutoValidationInput,
    FactAutoValidationReceipt,
    validate_native_fact_snapshot,
    verify_native_fact_receipt,
)
from finance_adapters.security.file_gate import scanned_object_path

LEGACY_POLICY = "legacy_manual_v1"


def uses_automatic_policy(filing: FilingVersion) -> bool:
    policy = getattr(filing, "fact_validation_policy", None) or LEGACY_POLICY
    if policy not in {LEGACY_POLICY, POLICY_VERSION}:
        raise ValueError("FACT_VALIDATION_POLICY_UNSUPPORTED")
    return policy == POLICY_VERSION


def _active_facts(session: Session, filing_id: int) -> list[FinancialFact]:
    return list(session.scalars(select(FinancialFact).where(
        FinancialFact.filing_version_id == filing_id,
        FinancialFact.quality_status != "superseded",
        FinancialFact.review_status != "superseded",
    ).order_by(FinancialFact.id)))


def _job(session: Session, filing_id: int) -> ExtractionJob:
    job = session.scalar(select(ExtractionJob).where(
        ExtractionJob.filing_version_id == filing_id,
    ).order_by(ExtractionJob.id.desc()).limit(1))
    if job is None:
        raise ValueError("AUTO_FACT_EXTRACTION_REQUIRED")
    if (job.status != "completed" or job.extraction_method != "pdf_text"
            or job.page_numbers is not None):
        raise ValueError("AUTO_FACT_COMPLETED_FULL_NATIVE_JOB_REQUIRED")
    if job.parser_version != "pymupdf-statements-v5":
        raise ValueError("AUTO_FACT_NATIVE_PARSER_VERSION_UNSUPPORTED")
    return job


def _snapshot(session: Session, filing: FilingVersion, job: ExtractionJob) -> FactAutoValidationInput:
    # Explicit projection excludes mutable admission statuses, reviewer data,
    # credentials and ORM internals. Every financial/evidence field is bound.
    facts = _active_facts(session, filing.id)
    items = []
    for fact in facts:
        fields = {name: getattr(fact, name) for name in (
            "id", "filing_version_id", "concept_code", "concept_name", "statement_type",
            "period_start", "period_end", "currency", "unit_scale", "flow_or_stock",
            "assurance_level", "revision_no", "raw_value", "comparative_period", "is_restatement",
        )}
        fields["value"] = format(Decimal(fact.value), "f")
        fields["evidence"] = tuple({
            **{name: getattr(evidence, name) for name in (
                "id", "page_no", "table_name", "row_locator", "column_locator", "raw_label",
                "raw_value", "bounding_box", "page_sha256", "extractor",
            )},
            "confidence": format(Decimal(evidence.confidence), "f"),
        } for evidence in session.scalars(select(EvidenceFragment).where(
            EvidenceFragment.fact_id == fact.id,
        ).order_by(EvidenceFragment.id)))
        fields["dimensions"] = tuple({"dimension_name": dimension.dimension_name,
                                       "dimension_value": dimension.dimension_value}
                                     for dimension in session.scalars(select(FactDimension).where(
                                         FactDimension.fact_id == fact.id,
                                     ).order_by(FactDimension.dimension_name,
                                                FactDimension.dimension_value, FactDimension.id)))
        items.append(fields)
    return FactAutoValidationInput.model_validate({
        "extraction_job_id": job.id, "extraction_method": job.extraction_method,
        "parser_version": job.parser_version, "page_numbers": None,
        "filing": {name: getattr(filing, name) for name in (
            "id", "company_id", "file_sha256", "period_start", "period_end", "report_type",
            "period_basis", "scope", "accounting_standard",
        )},
        "facts": tuple(items),
    })


def _latest(session: Session, filing_id: int) -> FactAutoValidationRun | None:
    return session.scalar(select(FactAutoValidationRun).where(
        FactAutoValidationRun.filing_version_id == filing_id,
    ).order_by(FactAutoValidationRun.id.desc()).limit(1))


def current_auto_receipt(session: Session, filing: FilingVersion) -> FactAutoValidationReceipt:
    """Rebuild from the active DB snapshot and original bytes on every strict read."""
    if not uses_automatic_policy(filing):
        raise ValueError("AUTO_FACT_POLICY_NOT_SELECTED")
    record = _latest(session, filing.id)
    if record is None or record.policy_version != POLICY_VERSION:
        raise ValueError("AUTO_FACT_RECEIPT_REQUIRED")
    job = _job(session, filing.id)
    snapshot = _snapshot(session, filing, job)
    if (record.extraction_job_id != job.id or record.source_sha256 != filing.file_sha256
            or snapshot.model_dump(mode="json") != record.input_snapshot):
        raise ValueError("AUTO_FACT_SNAPSHOT_STALE")
    try:
        receipt = FactAutoValidationReceipt.model_validate_json(json.dumps(record.receipt, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("AUTO_FACT_RECEIPT_INVALID") from exc
    if (record.input_snapshot_hash != receipt.input_snapshot_sha256
            or record.output_hash != stable_hash(record.receipt)):
        raise ValueError("AUTO_FACT_RECEIPT_HASH_MISMATCH")
    original = scanned_object_path(session, filing).read_bytes()
    rebuilt = verify_native_fact_receipt(original, snapshot, receipt)
    if not rebuilt.source_bytes_match:
        raise ValueError("AUTO_FACT_SOURCE_BYTES_MISMATCH")
    return rebuilt


def auto_validated_facts(
    session: Session, filing: FilingVersion, *, comparative: bool = False,
) -> dict[str, FinancialFact]:
    receipt = current_auto_receipt(session, filing)
    decisions = {item.fact_id: item for item in receipt.decisions}
    result = {}
    for fact in _active_facts(session, filing.id):
        decision = decisions.get(fact.id)
        if decision is None:
            raise ValueError("AUTO_FACT_DECISION_MISSING")
        if fact.review_status != decision.status or fact.quality_status != decision.status:
            raise ValueError("AUTO_FACT_STATUS_MISMATCH")
        if fact.comparative_period != comparative or decision.status != "auto_passed":
            continue
        if fact.concept_code in result:
            raise ValueError("AUTO_FACT_ACTIVE_CONCEPT_PERIOD_AMBIGUOUS")
        result[fact.concept_code] = fact
    return result


def run_auto_validation(session: Session, filing_id: int) -> dict:
    """Persist one explicit policy selection; the caller rolls back on failure."""
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise ValueError("FILING_NOT_FOUND")
    uses_automatic_policy(filing)  # Reject unknown strategies, including on writes.
    job = _job(session, filing.id)
    snapshot = _snapshot(session, filing, job)
    if not snapshot.facts:
        raise ValueError("AUTO_FACT_CANDIDATES_REQUIRED")
    original = scanned_object_path(session, filing).read_bytes()
    receipt = validate_native_fact_snapshot(original, snapshot)
    if not receipt.source_bytes_match:
        raise ValueError("AUTO_FACT_SOURCE_BYTES_MISMATCH")
    data = receipt.model_dump(mode="json")
    record = FactAutoValidationRun(
        filing_version_id=filing.id, extraction_job_id=job.id, policy_version=POLICY_VERSION,
        source_sha256=filing.file_sha256, input_snapshot_hash=receipt.input_snapshot_sha256,
        output_hash=stable_hash(data), input_snapshot=snapshot.model_dump(mode="json"), receipt=data,
    )
    session.add(record)
    filing.fact_validation_policy = POLICY_VERSION
    by_id = {fact.id: fact for fact in _active_facts(session, filing.id)}
    for decision in receipt.decisions:
        fact = by_id[decision.fact_id]
        fact.review_status = fact.quality_status = decision.status
    # A new admission contract invalidates derived live views, preserving raw
    # responses and receipts. Re-selecting an unchanged contract is still logged.
    for analysis in session.scalars(select(AnalysisRun).where(AnalysisRun.filing_version_id == filing.id)):
        analysis.invalidated_at = datetime.now(UTC)
    for metric in session.scalars(select(MetricResult).where(MetricResult.filing_version_id == filing.id)):
        metric.applicability_status = metric.quality_status = "superseded"
    session.flush()
    record_audit_event(session, filing.id, category="validation", action="native_fact_auto_validation",
                       status="completed", tool_name="independent_native_primary_geometry",
                       tool_version=POLICY_VERSION, input_hash=receipt.input_snapshot_sha256,
                       output_hash=record.output_hash, source_ref=filing.object_key,
                       detail={"validation_run_id": record.id, "extraction_job_id": job.id,
                               "counts": dict(Counter(item.status for item in receipt.decisions)),
                               "reviewer_records_created": 0,
                               "financial_truth_verified": False, "free_text_semantics_verified": False})
    from finance_adapters.persistence.review_gateway import recompute_filing_quality
    recompute_filing_quality(session, filing.id)  # Atomic commit includes receipt, states and invalidation.
    return auto_validation_view(session, filing)


def auto_validation_view(session: Session, filing: FilingVersion) -> dict:
    record = _latest(session, filing.id)
    data = {"filing_version_id": filing.id,
            "policy_version": getattr(filing, "fact_validation_policy", None) or LEGACY_POLICY,
            "validation_run_id": record.id if record else None,
            "filing_status": filing.status, "receipt": record.receipt if record else None,
            "fresh": False, "failure_reason": None}
    if record is not None:
        try:
            current_auto_receipt(session, filing)
            data["fresh"] = True
        except (ValueError, OSError) as exc:
            data["failure_reason"] = str(exc)
    return data
