"""Chapter workflow API. All resume/replay operations are explicit and version-bound."""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from finance_adapters.exports.reasoning import provider_reasoning
from finance_adapters.persistence.database import SessionLocal, get_session
from finance_adapters.persistence.models import AnalysisRun
from finance_adapters.providers.chapter_analysis import (
    CHAPTER_ANALYSIS_VERSION,
    chapter_journal,
    create_chapter_analysis,
    replay_chapter_analysis,
    run_chapter_analysis,
)
from finance_adapters.providers.run_journal import JournalPersistenceError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import update
from sqlalchemy.orm import Session

router = APIRouter()


class ChapterAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_document_ids: list[int] | None = Field(default=None, min_length=1, max_length=50)
    limits: dict | None = None

    @field_validator("selected_document_ids", mode="before")
    @classmethod
    def selected_ids(cls, value):
        if value is not None and (not isinstance(value, list) or any(type(i) is not int or i <= 0 for i in value)):
            raise ValueError("Document IDs must be positive integers")
        return value


def _envelope(request, data):
    from finreport_api.routes import envelope
    return envelope(request, data)


def _run(session: Session, run_id: int) -> AnalysisRun:
    run = session.get(AnalysisRun, run_id)
    if run is None or run.orchestration_version != CHAPTER_ANALYSIS_VERSION:
        raise HTTPException(404, "CHAPTER_ANALYSIS_NOT_FOUND")
    return run


def _background(run_id: int):
    with SessionLocal() as session:
        run_chapter_analysis(session, run_id)


def _request_error(exc: Exception) -> str:
    """Return stable public codes, never arbitrary parser/provider exception text."""
    value = str(exc)
    return value if re.fullmatch(r"(?:CHAPTER|FILING)_[A-Z0-9_]+", value) else "CHAPTER_REQUEST_INVALID"


@router.post("/api/v1/filings/{filing_id}/chapter-analyses", status_code=202)
def start_chapter_analysis(request: Request, filing_id: int, background: BackgroundTasks,
                            payload: ChapterAnalysisRequest | None = None,
                            session: Session = Depends(get_session)):
    body = payload or ChapterAnalysisRequest()
    try:
        run = create_chapter_analysis(session, filing_id, body.selected_document_ids, limits=body.limits)
    except (ValueError, TypeError) as exc:
        session.rollback()
        raise HTTPException(409, _request_error(exc)) from None
    except (JournalPersistenceError, OSError):
        session.rollback()
        raise HTTPException(503, "CHAPTER_JOURNAL_UNAVAILABLE") from None
    background.add_task(_background, run.id)
    return _envelope(request, {"analysis_run_id": run.id, "status": run.status,
                                "orchestration_version": run.orchestration_version})


@router.get("/api/v1/chapter-analyses/{run_id}")
def get_chapter_analysis(request: Request, run_id: int, session: Session = Depends(get_session)):
    run = _run(session, run_id)
    return _envelope(request, {"analysis_run_id": run.id, "filing_version_id": run.filing_version_id,
        "status": run.status, "output": run.output, "failure_reason": run.failure_reason,
        "input_snapshot_hash": run.input_snapshot_hash, "tool_trace_hash": run.tool_trace_hash,
        "orchestration_version": run.orchestration_version, "invalidated_at": run.invalidated_at,
        "provider_reasoning": provider_reasoning({"attempts": (run.output or {}).get("provider_calls", [])}),
        "semantic_link_summary": {"status": "recorded_in_claim_receipts",
                                  "boundary": "逐条核验记录保存在输出 receipts；模型判定不等于绝对财务真值。"},
        "log_url": f"/api/v1/chapter-analyses/{run.id}/events",
        "recovery": {"inspection": "deferred_to_explicit_replay",
                     "url": f"/api/v1/chapter-analyses/{run.id}/replay"}})


@router.get("/api/v1/chapter-analyses/{run_id}/events")
def chapter_events(request: Request, run_id: int, after: int = Query(0, ge=0),
                    limit: int = Query(100, ge=1, le=1000),
                    min_level: Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
                    stage: str | None = None, task_id: str | None = None,
                    session: Session = Depends(get_session)):
    _run(session, run_id)
    return _envelope(request, chapter_journal(run_id).read_events(
        after=after, limit=limit, min_level=min_level, stage=stage, task_id=task_id))


@router.get("/api/v1/chapter-analyses/{run_id}/artifact")
def chapter_artifact(request: Request, run_id: int, path: str, sha256: str,
                        session: Session = Depends(get_session)):
    _run(session, run_id)
    try:
        value = chapter_journal(run_id).read_artifact({"path": path, "sha256": sha256})
        return _envelope(request, value)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, "CHAPTER_ARTIFACT_UNAVAILABLE_OR_INVALID") from exc


@router.post("/api/v1/chapter-analyses/{run_id}/cancel")
def cancel_chapter_analysis(request: Request, run_id: int, session: Session = Depends(get_session)):
    run = _run(session, run_id)
    if run.status in {"queued", "running"}:
        journal = chapter_journal(run_id)
        journal.request_cancel()
        if run.status == "queued":
            session.execute(update(AnalysisRun).where(
                AnalysisRun.id == run.id, AnalysisRun.status == "queued").values(status="cancelled"))
            session.commit()
            session.refresh(run)
    return _envelope(request, {"analysis_run_id": run.id, "status": run.status,
                                "cancellation_requested": True})


@router.post("/api/v1/chapter-analyses/{run_id}/resume", status_code=202)
def resume_chapter_analysis(request: Request, run_id: int, background: BackgroundTasks,
                            session: Session = Depends(get_session)):
    old = _run(session, run_id)
    snapshot = old.tool_trace["input_snapshot"]
    selected = snapshot["selected_document_ids"] if snapshot["selection_mode"] == "explicit_joint" else None
    try:
        run = create_chapter_analysis(session, old.filing_version_id, selected,
                                      limits=old.tool_trace.get("limits"), previous_run_id=run_id)
    except (ValueError, TypeError) as exc:
        session.rollback()
        raise HTTPException(409, _request_error(exc)) from None
    except (JournalPersistenceError, OSError):
        session.rollback()
        raise HTTPException(503, "CHAPTER_JOURNAL_UNAVAILABLE") from None
    background.add_task(_background, run.id)
    return _envelope(request, {"analysis_run_id": run.id, "status": run.status, "previous_run_id": run_id})


@router.get("/api/v1/chapter-analyses/{run_id}/replay")
def replay_chapter_route(request: Request, run_id: int, session: Session = Depends(get_session)):
    try:
        return _envelope(request, replay_chapter_analysis(_run(session, run_id)))
    except ValueError as exc:
        raise HTTPException(409, _request_error(exc)) from None


@router.get("/api/v1/chapter-analyses/{run_id}/report.md")
def chapter_report(run_id: int, session: Session = Depends(get_session)):
    run = _run(session, run_id)
    if not run.output:
        raise HTTPException(409, "CHAPTER_REPORT_NOT_READY")
    try:
        replay = replay_chapter_analysis(run)
    except (ValueError, OSError):
        raise HTTPException(409, "CHAPTER_REPORT_INTEGRITY_FAILED") from None
    if (not replay.get("publication_allowed") or run.invalidated_at is not None
            or run.status not in {"completed", "partial_result", "waiting_for_documents"}):
        raise HTTPException(409, "CHAPTER_REPORT_PUBLICATION_NOT_VALIDATED")
    output = run.output
    text = output.get("report_markdown")
    if not text:
        raise HTTPException(409, "CHAPTER_REPORT_NOT_VALIDATED")
    return Response(text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="finreport-chapter-{run_id}.md"'})
