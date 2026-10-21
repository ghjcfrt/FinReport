from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from finance_adapters.config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_database() -> None:
    from . import models

    Base.metadata.create_all(engine)
    # Development databases predate some audit columns. Apply additive changes
    # so local runs remain recoverable without rewriting existing user data.
    additions = {
        "companies": {"business_model_tags": "JSON"},
        "filing_versions": {
            "source_priority": "INTEGER DEFAULT 3",
            "fact_validation_policy": "VARCHAR(64) DEFAULT 'legacy_manual_v1'",
        },
        "extraction_jobs": {
            "retry_count": "INTEGER DEFAULT 0", "queued_at": "DATETIME",
            "owner_token": "VARCHAR(64)", "lease_expires_at": "DATETIME",
            "extraction_method": "VARCHAR(16) DEFAULT 'pdf_text'",
            "page_numbers": "JSON",
        },
        "ocr_calls": {"response_text": "TEXT", "started_at": "DATETIME"},
        "table_candidates": {"ocr_call_id": "INTEGER REFERENCES ocr_calls(id)"},
        "evidence_fragments": {"extractor": "VARCHAR(128) DEFAULT 'pymupdf-text-v1'"},
        "analysis_runs": {
            "orchestration_version": "VARCHAR(64) DEFAULT 'analysis-v1'",
            "tool_trace": "JSON",
            "invalidated_at": "DATETIME",
        },
        "metric_results": {
            "quality_status": "VARCHAR(32) DEFAULT 'strict_audited'",
            "scope": "VARCHAR(32) DEFAULT 'consolidated'",
            "period_basis": "VARCHAR(32) DEFAULT 'annual'",
        },
        "financial_facts": {"comparative_period": "BOOLEAN DEFAULT 0", "is_restatement": "BOOLEAN DEFAULT 0"},
        "fact_reviews": {"evidence_digest": "VARCHAR(64)"},
        "validation_results": {"tolerance": "VARCHAR(128)"},
        "analysis_claims": {"review_status": "VARCHAR(32) DEFAULT 'not_required'"},
        "file_scans": {"original_filename": "VARCHAR(255)"},
        "document_text_chunks": {
            "page_sha256": "VARCHAR(64) DEFAULT ''",
            "review_status": "VARCHAR(32) DEFAULT 'unreviewed'",
            "review_digest": "VARCHAR(64)", "reviewer_id": "VARCHAR(128)",
            "reviewed_at": "DATETIME",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
    for model in (models.FactDimension, models.EvidenceFragment, models.DocumentTextChunk,
                  models.TableCandidateRecord):
        for index in model.__table__.indexes:
            index.create(engine, checkfirst=True)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
