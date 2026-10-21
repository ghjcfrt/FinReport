"""Explicit experimental structured-candidate API; no financial admission."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from finance_adapters.persistence.database import get_session
from finance_adapters.persistence.models import AnalysisRun
from finance_adapters.providers.grounded_analysis import (
    GROUNDED_REPLAY_VERSIONS,
    TASKS,
    GroundedAnalysisSelection,
    create_grounded_analysis,
    grounded_source_freshness,
    replay_grounded_analysis,
)
from finance_adapters.providers.grounded_presentation import build_grounded_evidence_view
from finance_adapters.providers.grounded_recovery import plan_grounded_recovery
from sqlalchemy.orm import Session

router = APIRouter()


def _background(run_id: int) -> None:
    from finance_adapters.persistence.database import SessionLocal
    from finance_adapters.providers.grounded_analysis import run_grounded_analysis

    with SessionLocal() as session:
        run_grounded_analysis(session, run_id)


def _run(session: Session, run_id: int) -> AnalysisRun:
    run = session.get(AnalysisRun, run_id)
    if run is None or run.orchestration_version not in GROUNDED_REPLAY_VERSIONS:
        raise HTTPException(404, "GROUNDED_ANALYSIS_NOT_FOUND")
    return run


@router.get("/api/v1/grounded-analyses/tasks")
def grounded_tasks(request: Request):
    from finreport_api.routes import envelope

    return envelope(request, {"mode": "experimental_candidates", "tasks": TASKS})


@router.post("/api/v1/filings/{filing_version_id}/grounded-analyses", status_code=202)
def start_grounded_analysis(request: Request, filing_version_id: int, payload: GroundedAnalysisSelection,
                             background: BackgroundTasks, session: Session = Depends(get_session)):
    from finreport_api.routes import envelope

    try:
        run = create_grounded_analysis(session, filing_version_id, payload.model_dump(mode="json"))
        background.add_task(_background, run.id)
        return envelope(request, {"analysis_run_id": run.id, "status": run.status,
                                  "mode": "experimental_candidates"})
    except ValueError as exc:
        session.rollback()
        raise HTTPException(404 if str(exc) == "FILING_NOT_FOUND" else 409, str(exc)) from exc


@router.get("/api/v1/grounded-analyses/{analysis_run_id}")
def get_grounded_analysis(request: Request, analysis_run_id: int, session: Session = Depends(get_session)):
    from finreport_api.routes import envelope

    run = _run(session, analysis_run_id)
    return envelope(request, {"analysis_run_id": run.id, "filing_version_id": run.filing_version_id,
                              "status": run.status, "output": run.output,
                              "failure_reason": run.failure_reason, "tool_trace": run.tool_trace,
                              "source_freshness": grounded_source_freshness(session, run),
                              "input_snapshot_hash": run.input_snapshot_hash,
                              "tool_trace_hash": run.tool_trace_hash, "invalidated_at": run.invalidated_at})


@router.get("/api/v1/grounded-analyses/{analysis_run_id}/replay")
def replay_grounded_analysis_route(request: Request, analysis_run_id: int,
                                    session: Session = Depends(get_session)):
    from finreport_api.routes import envelope

    try:
        return envelope(request, replay_grounded_analysis(_run(session, analysis_run_id)))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/v1/grounded-analyses/{analysis_run_id}/export")
def export_grounded_analysis(analysis_run_id: int, session: Session = Depends(get_session)):
    """Download a frozen, detached replay record without another provider call.

    Keep the original trace and response intact for hash reconstruction. Export
    only persisted replay/version fields, never live settings, request
    credentials or ORM state. Persisted sanitized provider metadata remains
    part of the trace.
    """
    run = _run(session, analysis_run_id)
    payload = {"id": run.id, "filing_version_id": run.filing_version_id,
               "model": run.model, "model_version": run.model_version,
               "prompt_version": run.prompt_version, "validator_version": run.validator_version,
               "orchestration_version": run.orchestration_version, "status": run.status,
               "input_snapshot_hash": run.input_snapshot_hash, "tool_trace_hash": run.tool_trace_hash,
               "tool_trace": run.tool_trace, "output": run.output,
               "failure_reason": run.failure_reason, "invalidated_at": run.invalidated_at}
    return JSONResponse(jsonable_encoder(payload), headers={
        "Content-Disposition": f'attachment; filename="grounded-analysis-{run.id}.json"',
        "Cache-Control": "no-store",
    })


@router.get("/api/v1/grounded-analyses/{analysis_run_id}/recovery")
def grounded_analysis_recovery(request: Request, analysis_run_id: int,
                               session: Session = Depends(get_session)):
    from finreport_api.routes import envelope

    return envelope(request, plan_grounded_recovery(_run(session, analysis_run_id)))


@router.get("/api/v1/grounded-analyses/{analysis_run_id}/evidence-view")
def grounded_analysis_evidence_view(request: Request, analysis_run_id: int,
                                    session: Session = Depends(get_session)):
    """Display isolated program evidence without changing the saved run."""
    from finreport_api.routes import envelope

    return envelope(request, build_grounded_evidence_view(_run(session, analysis_run_id)))
