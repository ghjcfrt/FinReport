"""Controlled evidence access and resumable financial-analysis sessions."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.database import get_session
from finance_adapters.persistence.models import AnalysisRun, AuditEvent, FilingVersion
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

router = APIRouter()


class TextReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(confirmed|rejected)$")
    reviewer_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    expected_text_sha256: str = Field(pattern="^[a-f0-9]{64}$")


def _response(request: Request, data):
    from finreport_api.routes import envelope
    return envelope(request, data)


def _filing(session: Session, filing_id: int) -> FilingVersion:
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise HTTPException(404, "FILING_NOT_FOUND")
    return filing


@router.post("/api/v1/analyses/{analysis_run_id}/resume", status_code=202)
def resume_analysis(request: Request, analysis_run_id: int, background: BackgroundTasks,
                    session: Session = Depends(get_session)):
    """Keep the checkpoint and admit newly supplied documents on the next round."""
    from finance_adapters.providers.llm_analysis import (
        STRICT_ANALYSIS_ORCHESTRATION_VERSIONS,
        build_evidence_package,
    )

    from finreport_api.routes import workflow_runtime

    run = session.get(AnalysisRun, analysis_run_id)
    if run is None:
        raise HTTPException(404, "ANALYSIS_NOT_FOUND")
    if getattr(run, "orchestration_version", None) not in STRICT_ANALYSIS_ORCHESTRATION_VERSIONS:
        raise HTTPException(409, "ANALYSIS_WORKFLOW_TYPE_MISMATCH")
    if run.status not in {"waiting_for_documents", "partial_result", "invalidated", "continued"}:
        raise HTTPException(409, "ANALYSIS_NOT_RESUMABLE")
    try:
        try:
            package = build_evidence_package(session, run.filing_version_id)
        except ValueError:
            if run.invalidated_at is None:
                raise
            from finance_adapters.persistence.metric_gateway import calculate_metrics
            calculate_metrics(session, run.filing_version_id)
            package = build_evidence_package(session, run.filing_version_id)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    current_hash = stable_hash(package)
    if run.invalidated_at is not None or current_hash != run.input_snapshot_hash:
        # A source review may invalidate the old strict snapshot.  Preserve it
        # and continue as a linked revision instead of mutating its evidence.
        links = session.scalars(select(AuditEvent).where(
            AuditEvent.filing_version_id == run.filing_version_id,
            AuditEvent.action == "analysis_continued",
        ).order_by(AuditEvent.id.desc()))
        for link in links:
            detail = link.detail or {}
            if detail.get("prior_analysis_run_id") != run.id:
                continue
            existing = session.get(AnalysisRun, detail.get("analysis_run_id"))
            if (existing is not None and existing.invalidated_at is None
                    and existing.input_snapshot_hash == current_hash
                    and existing.status != "failed"):
                return _response(request, {"analysis_run_id": existing.id, "status": existing.status,
                                           "continued_from_analysis_run_id": run.id})
        previous_status = run.status
        previous_reason = run.failure_reason
        claimed = session.execute(update(AnalysisRun).where(
            AnalysisRun.id == run.id, AnalysisRun.status == previous_status,
            (AnalysisRun.failure_reason.is_(None) if previous_reason is None
             else AnalysisRun.failure_reason == previous_reason),
        ).values(status="continuing"))
        if claimed.rowcount != 1:
            session.rollback()
            raise HTTPException(409, "ANALYSIS_ALREADY_RESUMING")
        if run.tool_trace is not None and stable_hash(run.tool_trace) != run.tool_trace_hash:
            session.rollback()
            raise HTTPException(409, "ANALYSIS_PREVIOUS_TRACE_HASH_MISMATCH")
        continuation = AnalysisRun(filing_version_id=run.filing_version_id,
                                   input_snapshot_hash=current_hash,
                                   tool_trace=deepcopy(run.tool_trace), tool_trace_hash=run.tool_trace_hash)
        session.add(continuation)
        session.flush()
        run.status = "continued"
        run.failure_reason = f"CONTINUED_AS_ANALYSIS:{continuation.id}"
        run.invalidated_at = run.invalidated_at or datetime.now(UTC)
        record_audit_event(
            session, run.filing_version_id, category="generation", action="analysis_continued",
            status="queued", tool_name="analysis_orchestrator",
            tool_version=run.orchestration_version, input_hash=current_hash,
            detail={"prior_analysis_run_id": run.id, "analysis_run_id": continuation.id,
                    "prior_input_snapshot_hash": run.input_snapshot_hash,
                    "previous_trace_hash": run.tool_trace_hash,
                    "previous_failure_reason": previous_reason,
                    "reason": "Reviewed source changed; original output retained as a historical revision."},
        )
        session.commit()
        workflow_runtime.dispatch_analyze(continuation.id, background)
        return _response(request, {"analysis_run_id": continuation.id, "status": "queued",
                                   "continued_from_analysis_run_id": run.id})
    previous_status = run.status
    # Claim the transition atomically: two resume clicks must not run two models.
    claimed = session.execute(update(AnalysisRun).where(
        AnalysisRun.id == run.id,
        AnalysisRun.status == previous_status,
        AnalysisRun.invalidated_at.is_(None),
    ).values(status="queued", failure_reason=None))
    if claimed.rowcount != 1:
        session.rollback()
        raise HTTPException(409, "ANALYSIS_ALREADY_RESUMING")
    record_audit_event(
        session, run.filing_version_id, category="generation", action="analysis_resumed",
        status="queued", tool_name="analysis_orchestrator",
        tool_version=run.orchestration_version, input_hash=run.input_snapshot_hash,
        detail={"analysis_run_id": run.id, "previous_status": previous_status,
                "previous_trace_hash": run.tool_trace_hash},
    )
    session.commit()
    workflow_runtime.dispatch_analyze(run.id, background)
    return _response(request, {"analysis_run_id": run.id, "status": "queued"})


@router.post("/api/v1/filings/{filing_version_id}/text-evidence/index")
def index_text_evidence(request: Request, filing_version_id: int,
                        session: Session = Depends(get_session)):
    from finance_adapters.providers.evidence_retrieval import (
        get_evidence_retriever,
        materialize_document_text_chunks,
    )
    _filing(session, filing_version_id)
    try:
        materialized = materialize_document_text_chunks(session, filing_version_id)
        indexed = get_evidence_retriever().index_chunks(session, filing_version_id)
        session.commit()
        return _response(request, {"materialized": materialized, "index": indexed})
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/v1/filings/{filing_version_id}/text-evidence")
def get_text_evidence(request: Request, filing_version_id: int,
                      page_no: int | None = Query(None, ge=1),
                      query: str | None = Query(None, min_length=1, max_length=2000),
                      limit: int = Query(50, ge=1, le=100),
                      session: Session = Depends(get_session)):
    from finance_adapters.providers.evidence_retrieval import (
        document_coverage,
        read_chunks,
        search_text,
    )
    _filing(session, filing_version_id)
    try:
        pages = [page_no] if page_no is not None else None
        result = (search_text(session, filing_version_id, query, limit=limit, page_numbers=pages)
                  if query else {"hits": read_chunks(session, filing_version_id,
                                                     page_numbers=pages, limit=limit)})
        coverage = document_coverage(session, filing_version_id)
        session.commit()
        return _response(request, {"coverage": coverage, **result})
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.patch("/api/v1/filings/{filing_version_id}/text-evidence/{chunk_id}/review")
def review_text_evidence(request: Request, filing_version_id: int, chunk_id: int,
                         payload: TextReviewRequest, session: Session = Depends(get_session)):
    from finance_adapters.providers.evidence_retrieval import review_text_chunk
    _filing(session, filing_version_id)
    try:
        result = review_text_chunk(session, filing_version_id, chunk_id,
                                   **payload.model_dump())
        session.commit()
        return _response(request, result)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/v1/filings/{filing_version_id}/text-evidence/review")
def get_text_evidence_for_review(request: Request, filing_version_id: int,
                                  page_no: int | None = Query(None, ge=1),
                                  review_status: str | None = Query(None, pattern="^(unreviewed|confirmed|rejected)$"),
                                  include_rejected: bool = Query(True),
                                  limit: int = Query(50, ge=1, le=100),
                                  session: Session = Depends(get_session)):
    """Keep rejected text visible for human correction and re-review."""
    from finance_adapters.providers.evidence_retrieval import (
        document_coverage,
        list_review_chunks,
    )
    _filing(session, filing_version_id)
    try:
        rows = list_review_chunks(
            session, filing_version_id, page_numbers=[page_no] if page_no is not None else None,
            review_status=review_status, include_rejected=include_rejected, limit=limit,
        )
        coverage = document_coverage(session, filing_version_id)
        session.commit()
        return _response(request, {"coverage": coverage, "hits": rows})
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


