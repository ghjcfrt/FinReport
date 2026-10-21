"""Selected-page report candidates and independently scoped report analyses."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from finance_adapters.exports.reasoning import provider_reasoning
from finance_adapters.persistence.database import get_session
from finance_adapters.persistence.models import AnalysisRun, FilingVersion
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

router = APIRouter()


class ProviderReasoningField(BaseModel):
    field: str
    content: str


class ProviderReasoningCall(BaseModel):
    stage: str
    call_index: int
    status: Literal["returned", "disabled", "not_returned"]
    thinking_enabled: bool | None
    fields: list[ProviderReasoningField]


class ProviderReasoningView(BaseModel):
    status: Literal["available", "not_returned", "not_called"]
    calls: list[ProviderReasoningCall]


class SemanticLinkFindingView(BaseModel):
    finding_id: str
    topic: str
    obligation_id: str
    status: Literal["not_verified"] = "not_verified"
    reason: str


class SemanticLinkSummary(BaseModel):
    status: Literal["not_applicable", "not_verified", "legacy_not_recorded"]
    policy: str | None = None
    findings: list[SemanticLinkFindingView] = Field(default_factory=list)
    boundary: str


class ReportAnalysisView(BaseModel):
    analysis_run_id: int
    filing_version_id: int
    status: str
    output: dict | None
    failure_reason: str | None
    input_snapshot_hash: str | None
    tool_trace_hash: str | None
    orchestration_version: str | None
    invalidated_at: datetime | None
    provider_reasoning: ProviderReasoningView
    semantic_link_summary: SemanticLinkSummary


class ReportAnalysisEnvelope(BaseModel):
    data: ReportAnalysisView
    request_id: str
    data_as_of: datetime
    cache_generated_at: datetime
    rule_version: str


class AnalysisReasoningView(BaseModel):
    analysis_run_id: int
    status: str
    output: dict | None
    failure_reason: str | None
    input_snapshot_hash: str | None
    tool_trace_hash: str | None
    validator_version: str | None
    orchestration_version: str | None
    tool_trace: dict | None
    invalidated_at: datetime | None
    provider_reasoning: ProviderReasoningView


class AnalysisReasoningEnvelope(BaseModel):
    data: AnalysisReasoningView
    request_id: str
    data_as_of: datetime
    cache_generated_at: datetime
    rule_version: str


class ReportMeasureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(confirmed|rejected)$")
    reviewer_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reviewer_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A real reviewer identity and review reason are required.")
        return value.strip()


class ReportAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_document_ids: list[int] | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("selected_document_ids", mode="before")
    @classmethod
    def positive_document_ids(cls, value):
        if value is not None and (not isinstance(value, list)
                or any(type(item) is not int or item <= 0 for item in value)):
            raise ValueError("Selected document IDs must be positive integers.")
        return value


def _response(request: Request, data):
    from finreport_api.routes import envelope
    return envelope(request, data)


def _filing(session: Session, filing_id: int) -> FilingVersion:
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise HTTPException(404, "FILING_NOT_FOUND")
    return filing


def _run(session: Session, run_id: int) -> AnalysisRun:
    from finance_adapters.providers.report_analysis import REPORT_ANALYSIS_VERSION
    run = session.get(AnalysisRun, run_id)
    if run is None or run.orchestration_version != REPORT_ANALYSIS_VERSION:
        raise HTTPException(404, "REPORT_ANALYSIS_NOT_FOUND")
    return run


def _semantic_link_summary(output: dict | None) -> dict:
    workflow = output.get("agent_workflow") if isinstance(output, dict) else None
    if not isinstance(workflow, dict):
        return {
            "status": "not_applicable",
            "policy": None,
            "findings": [],
            "boundary": "本次输出没有多轮子命题工作流；没有可登记的子命题关联状态。",
        }
    policy = workflow.get("semantic_policy")
    if not policy:
        return {
            "status": "legacy_not_recorded",
            "policy": None,
            "findings": [],
            "boundary": "历史工作流未记录独立的子命题关联核验策略；不能把 finding 与 obligation_id 的并列视为语义关联已确认。",
        }
    findings = []
    for item in workflow.get("findings", []):
        if not isinstance(item, dict):
            continue
        if item.get("evidence_kind") != "company_disclosure" or not item.get("obligation_id"):
            continue
        findings.append({
            "finding_id": str(item.get("id", "")),
            "topic": str(item.get("topic", "")),
            "obligation_id": str(item["obligation_id"]),
            "status": "not_verified",
            "reason": "系统未保存独立的子命题关联核验结果；该原文仅作为候选引用，不构成该子命题结论。",
        })
    return {
        "status": "not_verified" if findings else "not_applicable",
        "policy": policy,
        "findings": findings,
        "boundary": ("公司原文 finding 的子命题关联均未保存独立核验结果；客户端不得仅凭 obligation_id 将其视为完成。"
                      if findings else "当前工作流没有登记需要提示的公司原文与子命题关联候选。"),
    }


def _background_report_analysis(run_id: int) -> None:
    from finance_adapters.persistence.database import SessionLocal
    from finance_adapters.providers.report_analysis import run_report_analysis
    with SessionLocal() as session:
        run_report_analysis(session, run_id)


@router.get("/api/v1/filings/{filing_version_id}/report-data")
def get_report_data(request: Request, filing_version_id: int,
                    session: Session = Depends(get_session)):
    from finance_adapters.persistence.report_measure_gateway import report_measure_context
    filing = _filing(session, filing_version_id)
    try:
        data = report_measure_context(session, filing)
        session.commit()
        return _response(request, data)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.patch("/api/v1/filings/{filing_version_id}/report-data/{candidate_id}/review")
def review_report_data(request: Request, filing_version_id: int, candidate_id: str,
                       payload: ReportMeasureReviewRequest, session: Session = Depends(get_session)):
    from finance_adapters.persistence.report_measure_gateway import review_report_measure
    filing = _filing(session, filing_version_id)
    try:
        data = review_report_measure(session, filing, candidate_id, **payload.model_dump())
        session.commit()
        return _response(request, data)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/v1/filings/{filing_version_id}/report-analyses", status_code=202)
def start_report_analysis(request: Request, filing_version_id: int, background: BackgroundTasks,
                          payload: ReportAnalysisRequest | None = None,
                          session: Session = Depends(get_session)):
    from finance_adapters.providers.report_analysis import create_report_analysis
    _filing(session, filing_version_id)
    try:
        run = create_report_analysis(session, filing_version_id,
                                    payload.selected_document_ids if payload else None)
        session.commit()
        background.add_task(_background_report_analysis, run.id)
        return _response(request, {"analysis_run_id": run.id, "status": run.status})
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/v1/report-analyses/{analysis_run_id}", response_model=ReportAnalysisEnvelope)
def get_report_analysis(request: Request, analysis_run_id: int,
                        session: Session = Depends(get_session)):
    run = _run(session, analysis_run_id)
    return _response(request, {"analysis_run_id": run.id, "filing_version_id": run.filing_version_id,
                              "status": run.status, "output": run.output,
                              "failure_reason": run.failure_reason,
                              "input_snapshot_hash": run.input_snapshot_hash,
                              "tool_trace_hash": run.tool_trace_hash,
                              "orchestration_version": run.orchestration_version,
                              "invalidated_at": run.invalidated_at,
                              "provider_reasoning": provider_reasoning(run.tool_trace),
                              "semantic_link_summary": _semantic_link_summary(run.output)})


@router.get("/api/v1/report-analyses/{analysis_run_id}/replay")
def replay_report_analysis_route(request: Request, analysis_run_id: int,
                                 session: Session = Depends(get_session)):
    from finance_adapters.providers.report_analysis import replay_report_analysis
    run = _run(session, analysis_run_id)
    try:
        return _response(request, replay_report_analysis(run))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
