from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    industry_code: Mapped[str] = mapped_column(String(64), default="general_non_financial")
    industry_taxonomy_version: Mapped[str] = mapped_column(String(64), default="CSRC-2024")
    business_model_tags: Mapped[list] = mapped_column(JSON, default=list)


class FilingVersion(Base):
    __tablename__ = "filing_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    report_type: Mapped[str] = mapped_column(String(32))
    period_basis: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(32), default="consolidated")
    accounting_standard: Mapped[str] = mapped_column(String(64), default="PRC_ASBE")
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default="user_upload")
    source_priority: Mapped[int] = mapped_column(Integer, default=3)
    source_uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    object_key: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    # Existing filings retain their historical admission contract. Selecting an
    # automatic policy is explicit and must never fabricate legacy approvals.
    fact_validation_policy: Mapped[str] = mapped_column(String(64), default="legacy_manual_v1")
    supersedes_filing_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("filing_versions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FileScan(Base):
    __tablename__ = "file_scans"
    id: Mapped[int] = mapped_column(primary_key=True)
    quarantine_id: Mapped[str] = mapped_column(String(36), index=True)
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    disposition: Mapped[str] = mapped_column(String(32))
    threat_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database_version: Mapped[str] = mapped_column(String(128), default="unknown")
    engine_version: Mapped[str] = mapped_column(String(128), default="unknown")
    scanner_state: Mapped[str] = mapped_column(String(32), default="healthy")
    scan_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_version_id: Mapped[int] = mapped_column(ForeignKey("filing_versions.id"))
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    parser_version: Mapped[str] = mapped_column(String(64), default="pymupdf-text-v1")
    extraction_method: Mapped[str] = mapped_column(String(16), default="pdf_text")
    # None means the whole immutable source; otherwise these are original PDF
    # page numbers, never the renumbered positions of a temporary subset.
    page_numbers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditEvent(Base):
    """Immutable execution evidence shown in the filing audit timeline."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_version_id: Mapped[int] = mapped_column(ForeignKey("filing_versions.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class OcrCall(Base):
    __tablename__ = "ocr_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_job_id: Mapped[int] = mapped_column(ForeignKey("extraction_jobs.id"))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(200))
    page_no: Mapped[int] = mapped_column(Integer)
    page_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=datetime.utcnow)
    response_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class FinancialFact(Base):
    __tablename__ = "financial_facts"
    __table_args__ = (
        UniqueConstraint(
            "filing_version_id", "concept_code", "period_start", "period_end", "revision_no"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_version_id: Mapped[int] = mapped_column(ForeignKey("filing_versions.id"))
    concept_code: Mapped[str] = mapped_column(String(96))
    concept_name: Mapped[str] = mapped_column(String(200))
    statement_type: Mapped[str] = mapped_column(String(32))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    unit_scale: Mapped[int] = mapped_column(Integer, default=1)
    flow_or_stock: Mapped[str] = mapped_column(String(8))
    quality_status: Mapped[str] = mapped_column(String(32), default="candidate")
    review_status: Mapped[str] = mapped_column(String(32), default="needs_review")
    assurance_level: Mapped[str] = mapped_column(String(32), default="unknown")
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    raw_value: Mapped[str] = mapped_column(String(256))
    comparative_period: Mapped[bool] = mapped_column(default=False)
    is_restatement: Mapped[bool] = mapped_column(default=False)


class FactAutoValidationRun(Base):
    """Versioned source-bound receipts; product admission uses no reviewer rows."""

    __tablename__ = "fact_auto_validation_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_version_id: Mapped[int] = mapped_column(ForeignKey("filing_versions.id"), index=True)
    extraction_job_id: Mapped[int] = mapped_column(ForeignKey("extraction_jobs.id"))
    policy_version: Mapped[str] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    input_snapshot_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str] = mapped_column(String(64))
    input_snapshot: Mapped[dict] = mapped_column(JSON)
    receipt: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class Concept(Base):
    __tablename__ = "concepts"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(96), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    statement_type: Mapped[str] = mapped_column(String(32))
    normal_balance: Mapped[str] = mapped_column(String(16))
    mapping_version: Mapped[str] = mapped_column(String(64))


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(96), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    formula_version: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(32))
    required_concepts: Mapped[list] = mapped_column(JSON, default=list)
    applicable_sectors: Mapped[list] = mapped_column(JSON, default=list)
    exclusions: Mapped[list] = mapped_column(JSON, default=list)
    annualization_policy: Mapped[str] = mapped_column(String(64), default="none")
    status: Mapped[str] = mapped_column(String(32), default="planned")


class IndustryModule(Base):
    __tablename__ = "industry_modules"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="planned")
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    coverage_note: Mapped[str] = mapped_column(Text, default="")


class FactDimension(Base):
    __tablename__ = "fact_dimensions"
    id: Mapped[int] = mapped_column(primary_key=True)
    fact_id: Mapped[int] = mapped_column(ForeignKey("financial_facts.id"), index=True)
    dimension_name: Mapped[str] = mapped_column(String(96))
    dimension_value: Mapped[str] = mapped_column(String(256))


class EvidenceFragment(Base):
    __tablename__ = "evidence_fragments"
    id: Mapped[int] = mapped_column(primary_key=True)
    fact_id: Mapped[int] = mapped_column(ForeignKey("financial_facts.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    table_name: Mapped[str] = mapped_column(String(256), default="unclassified")
    row_locator: Mapped[str] = mapped_column(String(256))
    column_locator: Mapped[str] = mapped_column(String(256))
    raw_label: Mapped[str] = mapped_column(String(256))
    raw_value: Mapped[str] = mapped_column(String(256))
    bounding_box: Mapped[dict] = mapped_column(JSON)
    page_sha256: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    extractor: Mapped[str] = mapped_column(String(128), default="pymupdf-text-v1")


class DocumentTextChunk(Base):
    """Searchable text kept separate from admitted financial facts.

    A chunk may contain numbers or accounting-policy language, but it never
    becomes a strict fact merely because it was indexed.  The source hashes
    and page coordinates make every retrieval result traceable to the selected
    extraction attempt.
    """

    __tablename__ = "document_text_chunks"
    __table_args__ = (UniqueConstraint("filing_version_id", "chunk_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_version_id: Mapped[int] = mapped_column(ForeignKey("filing_versions.id"), index=True)
    chunk_key: Mapped[str] = mapped_column(String(160))
    page_no: Mapped[int] = mapped_column(Integer, index=True)
    page_sha256: Mapped[str] = mapped_column(String(64), default="")
    section: Mapped[str] = mapped_column(String(256), default="page")
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64), index=True)
    source_sha256: Mapped[str] = mapped_column(String(64), index=True)
    extraction_method: Mapped[str] = mapped_column(String(16), default="pdf_text")
    extractor_version: Mapped[str] = mapped_column(String(128), default="unknown")
    bounding_box: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    related_fact_ids: Mapped[list] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String(32), default="unreviewed")
    review_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TableCandidateRecord(Base):
    __tablename__ = "table_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_job_id: Mapped[int] = mapped_column(ForeignKey("extraction_jobs.id"))
    ocr_call_id: Mapped[int | None] = mapped_column(ForeignKey("ocr_calls.id"), nullable=True, index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    rows: Mapped[list] = mapped_column(JSON)
    extractor: Mapped[str] = mapped_column(String(128))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    review_required: Mapped[bool] = mapped_column(default=True)


class FactReview(Base):
    __tablename__ = "fact_reviews"
    __table_args__ = (UniqueConstraint("fact_id", "reviewer_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    fact_id: Mapped[int] = mapped_column(ForeignKey("financial_facts.id"))
    reviewer_id: Mapped[str] = mapped_column(String(128))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    evidence_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ValidationResult(Base):
    __tablename__ = "validation_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_version_id: Mapped[int] = mapped_column(ForeignKey("filing_versions.id"))
    rule_code: Mapped[str] = mapped_column(String(128))
    rule_version: Mapped[str] = mapped_column(String(64), default="v1")
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    actual_value: Mapped[str | None] = mapped_column(String(256), nullable=True)
    expected_value: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tolerance: Mapped[str | None] = mapped_column(String(128), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FxSourceRegistry(Base):
    __tablename__ = "fx_source_registry"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_name: Mapped[str] = mapped_column(String(128))
    release_id: Mapped[str] = mapped_column(String(128))
    license_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="frozen")


class FxRate(Base):
    __tablename__ = "fx_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_registry_id: Mapped[int] = mapped_column(ForeignKey("fx_source_registry.id"))
    quote_pair: Mapped[str] = mapped_column(String(32))
    quote_direction: Mapped[str] = mapped_column(String(16))
    rate_type: Mapped[str] = mapped_column(String(32))
    rate_date: Mapped[date] = mapped_column(Date)
    rate: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    fallback_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    version: Mapped[str] = mapped_column(String(64))


class MetricResult(Base):
    __tablename__ = "metric_results"
    __table_args__ = (UniqueConstraint("filing_version_id", "metric_code", "formula_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_version_id: Mapped[int] = mapped_column(ForeignKey("filing_versions.id"))
    metric_code: Mapped[str] = mapped_column(String(96))
    value: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    unit: Mapped[str] = mapped_column(String(32))
    formula_version: Mapped[str] = mapped_column(String(64), default="general-non-financial-v4")
    applicability_status: Mapped[str] = mapped_column(String(32))
    computation_mode: Mapped[str] = mapped_column(String(32))
    input_fact_ids: Mapped[list] = mapped_column(JSON, default=list)
    input_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    explanation_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quality_status: Mapped[str] = mapped_column(String(32), default="strict_audited")
    scope: Mapped[str] = mapped_column(String(32), default="consolidated")
    period_basis: Mapped[str] = mapped_column(String(32), default="annual")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_version_id: Mapped[int] = mapped_column(ForeignKey("filing_versions.id"))
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), default="claims-v1")
    input_snapshot_hash: Mapped[str] = mapped_column(String(64))
    tool_trace_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validator_version: Mapped[str] = mapped_column(String(64), default="claim-validator-v1")
    orchestration_version: Mapped[str] = mapped_column(String(64), default="analysis-v1")
    tool_trace: Mapped[list | None] = mapped_column(JSON, nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AnalysisClaim(Base):
    __tablename__ = "analysis_claims"
    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"))
    claim_type: Mapped[str] = mapped_column(String(64))
    claim_class: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    metric_result_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_fragment_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_relation: Mapped[str] = mapped_column(String(32))
    validation_status: Mapped[str] = mapped_column(String(32))
    review_status: Mapped[str] = mapped_column(String(32), default="not_required")


class PeerUniverse(Base):
    __tablename__ = "peer_universes"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(64), unique=True)
    industry_code: Mapped[str] = mapped_column(String(64))
    taxonomy_version: Mapped[str] = mapped_column(String(64))
    data_cutoff: Mapped[date] = mapped_column(Date)
    filing_ids: Mapped[list] = mapped_column(JSON, default=list)
    coverage: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    approval_trace: Mapped[list] = mapped_column(JSON, default=list)


class ComparisonRun(Base):
    __tablename__ = "comparison_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    peer_universe_version: Mapped[str] = mapped_column(String(64))
    metric_code: Mapped[str] = mapped_column(String(96))
    input_snapshot_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AnomalyObservation(Base):
    __tablename__ = "anomaly_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_version_id: Mapped[int] = mapped_column(ForeignKey("filing_versions.id"))
    rule_code: Mapped[str] = mapped_column(String(128))
    rule_version: Mapped[str] = mapped_column(String(64), default="anomaly-v1")
    status: Mapped[str] = mapped_column(String(32))
    explanation: Mapped[str] = mapped_column(Text)
    input_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    threshold: Mapped[str | None] = mapped_column(String(128), nullable=True)
    peer_universe_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suppression_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
