from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from finance_adapters.config import settings
from finance_adapters.exports.report_renderer import generate_export
from finance_adapters.parsing.pdf_parser import run_extraction
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.database import get_session, init_database
from finance_adapters.persistence.metric_gateway import calculate_metrics
from finance_adapters.persistence.models import (
    AnalysisRun,
    AnomalyObservation,
    AuditEvent,
    Company,
    ComparisonRun,
    EvidenceFragment,
    ExtractionJob,
    FactReview,
    FileScan,
    FilingVersion,
    FinancialFact,
    MetricResult,
    PeerUniverse,
)
from finance_adapters.persistence.review_gateway import recompute_filing_quality
from finance_adapters.providers.llm_analysis import build_evidence_package, run_analysis
from finance_adapters.security import ScanDisposition, scanner
from finance_application.workflows.runtime import WorkflowRuntime
from finance_domain.analysis.contracts import (
    ComparisonRequest,
    MetricRequest,
    ModelDiscoveryRequest,
    ModelSettingsUpdate,
    ReviewRequest,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

app = FastAPI(title="FinReport API", version="1.0.0")


@app.on_event("startup")
def startup() -> None:
    init_database()


@app.middleware("http")
async def request_id(request: Request, call_next):
    if settings.api_key and request.url.path != "/health":
        supplied = request.headers.get("X-API-Key")
        if supplied != settings.api_key:
            return JSONResponse(status_code=401, content={"type": "about:blank", "title": "UNAUTHORIZED", "status": 401})
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(HTTPException)
async def api_error(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": "about:blank",
            "title": exc.detail if isinstance(exc.detail, str) else "Request failed",
            "status": exc.status_code,
            "request_id": request.headers.get("X-Request-ID", ""),
        },
    )


def envelope(request: Request, data):
    return {
        "data": jsonable_encoder(data),
        "request_id": getattr(request.state, "request_id", request.headers.get("X-Request-ID", "")),
        "data_as_of": datetime.now(UTC).isoformat(),
        "cache_generated_at": datetime.now(UTC).isoformat(),
        "rule_version": "general-non-financial-v1",
    }


def get_filing(session: Session, filing_id: int) -> FilingVersion:
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise HTTPException(404, "FILING_NOT_FOUND")
    return filing


@app.get("/health")
def health(request: Request):
    return envelope(request, {"status": "ok"})


@app.get("/api/v1/settings/models")
def model_settings(request: Request):
    return envelope(request, {
        "analysis": {"provider": settings.llm_provider, "base_url": settings.llm_base_url, "model": settings.llm_model, "configured": bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model), "credentials_configured": bool(settings.llm_api_key)},
        "ocr": {"provider": settings.ocr_provider, "base_url": settings.ocr_base_url, "model": settings.ocr_model, "configured": bool(settings.ocr_base_url and settings.ocr_api_key and settings.ocr_model), "credentials_configured": bool(settings.ocr_api_key)},
        "supported_providers": ["openai", "anthropic"],
        "secrets_managed_server_side": True,
    })


@app.put("/api/v1/settings/models")
def update_model_settings(request: Request, payload: dict, session: Session = Depends(get_session)):
    analysis = ModelSettingsUpdate.model_validate(payload.get("analysis", {}))
    ocr = ModelSettingsUpdate.model_validate(payload.get("ocr", {}))
    if analysis.api_key:
        settings.update_model("analysis", provider=analysis.provider, base_url=analysis.base_url, api_key=analysis.api_key, model=analysis.model)
    else:
        settings.update_model("analysis", provider=analysis.provider, base_url=analysis.base_url, api_key=settings.llm_api_key or "", model=analysis.model)
    if ocr.api_key:
        settings.update_model("ocr", provider=ocr.provider, base_url=ocr.base_url, api_key=ocr.api_key, model=ocr.model)
    else:
        settings.update_model("ocr", provider=ocr.provider, base_url=ocr.base_url, api_key=settings.ocr_api_key or "", model=ocr.model)
    return model_settings(request)


@app.post("/api/v1/settings/models/{stage}/discover")
def discover_models(request: Request, stage: str, payload: ModelDiscoveryRequest):
    if stage not in {"analysis", "ocr"}:
        raise HTTPException(404, "MODEL_STAGE_NOT_FOUND")
    configured_key = settings.llm_api_key if stage == "analysis" else settings.ocr_api_key
    api_key = payload.api_key or configured_key
    if not api_key:
        raise HTTPException(422, "API_KEY_REQUIRED_FOR_MODEL_DISCOVERY")
    headers = {"Authorization": f"Bearer {api_key}"}
    if payload.provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    try:
        import httpx

        response = httpx.get(
            f"{payload.base_url.rstrip('/')}/models",
            headers=headers,
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        models = sorted(
            {item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)}
        )[:200]
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"MODEL_DISCOVERY_FAILED: {exc}") from exc
    return envelope(request, {"models": models})


@app.post("/api/v1/filings", status_code=202)
async def upload_filing(
    request: Request,
    file: UploadFile = File(...),
    stock_code: str = Form(...),
    company_name: str = Form(...),
    period_start: date = Form(...),
    period_end: date = Form(...),
    report_type: str = Form(...),
    period_basis: str = Form(...),
    industry_code: str = Form("general_non_financial"),
    source_uri: str | None = Form(None),
    source_priority: int = Form(3, ge=1, le=3),
    session: Session = Depends(get_session),
):
    if (
        not file.filename
        or not file.filename.lower().endswith(".pdf")
        or file.content_type not in {"application/pdf", "application/octet-stream"}
    ):
        raise HTTPException(415, "PDF_REQUIRED")
    try:
        scan = await scanner.register_and_enqueue(file, original_filename=file.filename)
    except ValueError as exc:
        raise HTTPException(413 if str(exc) == "FILE_TOO_LARGE" else 422, str(exc)) from exc
    session.add(
        FileScan(
            quarantine_id=scan.quarantine_id,
            file_sha256=scan.sha256,
            file_size=scan.file_size,
            original_filename=scan.original_filename,
            disposition=scan.disposition.value,
            threat_name=scan.threat_name,
            error_code=scan.error_code,
            database_version=scan.database_version,
            engine_version=scan.engine_version,
            scanner_state=scan.scanner_state,
            scan_time_ms=scan.scan_time_ms,
        )
    )
    session.commit()
    if scan.disposition is not ScanDisposition.CLEAN:
        status_code = 503 if scan.disposition in {ScanDisposition.UNAVAILABLE, ScanDisposition.ERROR} else 422
        raise HTTPException(status_code, scan.error_code or scan.threat_name or scan.disposition.value)
    sha256 = scan.sha256
    object_key = str(settings.storage_dir / "objects" / sha256[:2] / f"{sha256}.pdf")
    duplicate = session.scalar(select(FilingVersion).where(FilingVersion.file_sha256 == sha256))
    if duplicate:
        return envelope(
            request,
            {"filing_version_id": duplicate.id, "status": duplicate.status, "deduplicated": True},
        )
    company = session.scalar(select(Company).where(Company.stock_code == stock_code))
    if company is None:
        company = Company(stock_code=stock_code, name=company_name, industry_code=industry_code)
        session.add(company)
        session.flush()
    previous = session.scalar(
        select(FilingVersion)
        .where(
            FilingVersion.company_id == company.id,
            FilingVersion.period_start == period_start,
            FilingVersion.period_end == period_end,
            FilingVersion.report_type == report_type,
            FilingVersion.status != "superseded",
        )
        .order_by(FilingVersion.id.desc())
    )
    filing = FilingVersion(
        company_id=company.id,
        period_start=period_start,
        period_end=period_end,
        report_type=report_type,
        period_basis=period_basis,
        source_uri=source_uri,
        source_priority=source_priority,
        file_sha256=sha256,
        object_key=object_key,
        status="scan_clean",
        supersedes_filing_version_id=previous.id if previous else None,
    )
    if previous:
        previous.status = "superseded"
    session.add(filing)
    session.flush()
    record_audit_event(
        session,
        filing.id,
        category="file",
        action="file_ingested",
        status="completed",
        source_ref=filing.source_uri or filing.object_key,
        tool_name="malware_scan",
        tool_version=scan.engine_version,
        input_hash=filing.file_sha256,
        output_hash=stable_hash({"disposition": scan.disposition.value, "scanner": scan.scanner_state}),
        detail={
            "original_filename": scan.original_filename,
            "file_size": scan.file_size,
            "sha256": filing.file_sha256,
            "scan_disposition": scan.disposition.value,
            "scanner_state": scan.scanner_state,
        },
    )
    session.commit()
    return envelope(
        request, {"filing_version_id": filing.id, "status": filing.status, "deduplicated": False}
    )


@app.post("/api/v1/filings/{filing_version_id}/jobs", status_code=202)
def create_job(
    request: Request,
    filing_version_id: int,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
):
    filing = get_filing(session, filing_version_id)
    existing = session.scalar(
        select(ExtractionJob)
        .where(
            ExtractionJob.filing_version_id == filing.id,
            ExtractionJob.status.in_(["queued", "running", "completed"]),
        )
        .order_by(ExtractionJob.id.desc())
    )
    if existing:
        return envelope(request, {"job_id": existing.id, "status": existing.status, "reused": True})
    job = ExtractionJob(filing_version_id=filing.id)
    session.add(job)
    session.commit()
    workflow_runtime.dispatch_extract(job.id, background)
    return envelope(request, {"job_id": job.id, "status": job.status, "reused": False})


def _background_extract(job_id: int) -> None:
    from finance_adapters.persistence.database import SessionLocal

    with SessionLocal() as session:
        run_extraction(session, job_id)


@app.get("/api/v1/jobs/{job_id}")
def get_job(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = session.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(404, "JOB_NOT_FOUND")
    review_count = (
        session.scalar(
            select(FinancialFact)
            .where(
                FinancialFact.filing_version_id == job.filing_version_id,
                FinancialFact.review_status == "needs_review",
            )
            .count()
        )
        if False
        else len(
            list(
                session.scalars(
                    select(FinancialFact).where(
                        FinancialFact.filing_version_id == job.filing_version_id,
                        FinancialFact.review_status == "needs_review",
                    )
                )
            )
        )
    )
    return envelope(
        request,
        {
            "job_id": job.id,
            "filing_version_id": job.filing_version_id,
            "stage": job.stage,
            "status": job.status,
            "progress": job.progress,
            "error_code": job.error_code,
            "error_detail": job.error_detail,
            "retryable": job.retryable,
            "review_count": review_count,
        },
    )


@app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
def retry_job(request: Request, job_id: int, background: BackgroundTasks, session: Session = Depends(get_session)):
    job = session.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(404, "JOB_NOT_FOUND")
    if job.status not in {"failed", "needs_review"}:
        raise HTTPException(409, "JOB_NOT_RETRYABLE")
    job.status, job.stage, job.error_code, job.error_detail = "queued", "queued", None, None
    session.commit()
    workflow_runtime.dispatch_extract(job.id, background)
    return envelope(request, {"job_id": job.id, "status": job.status, "reused": True})


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = session.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(404, "JOB_NOT_FOUND")
    if job.status in {"completed", "failed", "cancelled"}:
        raise HTTPException(409, "JOB_NOT_CANCELLABLE")
    job.status, job.stage, job.error_code, job.error_detail = "cancelled", "cancelled", "CANCELLED", "Cancelled by user"
    session.commit()
    return envelope(request, {"job_id": job.id, "status": job.status})


@app.get("/api/v1/filings/{filing_version_id}/facts")
def list_facts(
    request: Request,
    filing_version_id: int,
    statement_type: str | None = None,
    quality_status: str | None = None,
    session: Session = Depends(get_session),
):
    get_filing(session, filing_version_id)
    query = select(FinancialFact).where(FinancialFact.filing_version_id == filing_version_id)
    if statement_type:
        query = query.where(FinancialFact.statement_type == statement_type)
    if quality_status:
        query = query.where(FinancialFact.quality_status == quality_status)
    facts = []
    for fact in session.scalars(query):
        evidence = session.scalar(
            select(EvidenceFragment).where(EvidenceFragment.fact_id == fact.id)
        )
        facts.append(
            {
                "id": fact.id,
                "concept_code": fact.concept_code,
                "concept_name": fact.concept_name,
                "value": fact.value,
                "currency": fact.currency,
                "unit_scale": fact.unit_scale,
                "flow_or_stock": fact.flow_or_stock,
                "quality_status": fact.quality_status,
                "review_status": fact.review_status,
                "revision_no": fact.revision_no,
                "evidence": evidence,
                "extractor": evidence.extractor if evidence else None,
            }
        )
    return envelope(request, facts)


@app.patch("/api/v1/facts/{fact_id}/review")
def review_fact(
    request: Request, fact_id: int, payload: ReviewRequest, session: Session = Depends(get_session)
):
    fact = session.get(FinancialFact, fact_id)
    if fact is None:
        raise HTTPException(404, "FACT_NOT_FOUND")
    if payload.decision == "corrected":
        if payload.corrected_value is None:
            raise HTTPException(422, "CORRECTED_VALUE_REQUIRED")
        new_fact = FinancialFact(
            filing_version_id=fact.filing_version_id,
            concept_code=fact.concept_code,
            concept_name=fact.concept_name,
            statement_type=fact.statement_type,
            period_start=fact.period_start,
            period_end=fact.period_end,
            value=payload.corrected_value,
            currency=fact.currency,
            unit_scale=fact.unit_scale,
            flow_or_stock=fact.flow_or_stock,
            raw_value=fact.raw_value,
            revision_no=fact.revision_no + 1,
        )
        session.add(new_fact)
        session.flush()
        old_evidence = session.scalar(
            select(EvidenceFragment).where(EvidenceFragment.fact_id == fact.id)
        )
        if old_evidence:
            session.add(
                EvidenceFragment(
                    fact_id=new_fact.id,
                    page_no=old_evidence.page_no,
                    table_name=old_evidence.table_name,
                    row_locator=old_evidence.row_locator,
                    column_locator=old_evidence.column_locator,
                    raw_label=old_evidence.raw_label,
                    raw_value=str(payload.corrected_value),
                    bounding_box=old_evidence.bounding_box,
                    page_sha256=old_evidence.page_sha256,
                    confidence=old_evidence.confidence,
                    extractor=old_evidence.extractor,
                )
            )
        fact.review_status, fact.quality_status = "superseded", "superseded"
        fact = new_fact
    session.add(
        FactReview(
            fact_id=fact.id,
            reviewer_id=payload.reviewer_id,
            decision=payload.decision,
            reason=payload.reason,
            evidence_digest=hashlib.sha256(json.dumps({"fact_id": fact.id, "value": str(fact.value), "reason": payload.reason}, sort_keys=True).encode()).hexdigest(),
        )
    )
    session.commit()
    status = recompute_filing_quality(session, fact.filing_version_id)
    session.query(AnalysisRun).filter(AnalysisRun.filing_version_id == fact.filing_version_id).update(
        {AnalysisRun.invalidated_at: datetime.now(UTC), AnalysisRun.status: "invalidated"},
        synchronize_session=False,
    )
    session.commit()
    return envelope(
        request, {"fact_id": fact.id, "filing_status": status, "review_status": fact.review_status}
    )


@app.post("/api/v1/filings/{filing_version_id}/metrics")
def calculate(
    request: Request,
    filing_version_id: int,
    payload: MetricRequest,
    session: Session = Depends(get_session),
):
    if not payload.strict:
        raise HTTPException(422, "ONLY_STRICT_MODE_SUPPORTED")
    try:
        values = calculate_metrics(session, filing_version_id)
        filing = session.get(FilingVersion, filing_version_id)
        if filing:
            filing.status = "metrics_ready"
        snapshot = next((item.input_snapshot_hash for item in values if item.input_snapshot_hash), None)
        record_audit_event(
            session,
            filing_version_id,
            category="calculation",
            action="strict_metrics_calculated",
            status="completed",
            tool_name="metric_gateway",
            tool_version="general-non-financial-v1",
            input_hash=snapshot,
            output_hash=stable_hash([
                {"code": item.metric_code, "value": str(item.value), "status": item.applicability_status}
                for item in values
            ]),
            detail={
                "mode": "strict_audited",
                "result_count": len(values),
                "calculated_count": sum(item.applicability_status == "calculated" for item in values),
                "metric_ids": [item.id for item in values],
            },
        )
        session.commit()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return envelope(request, values)


@app.get("/api/v1/companies/{company_id}/metric-trends")
def metric_trends(
    request: Request,
    company_id: int,
    metric_codes: list[str] = Query(...),
    session: Session = Depends(get_session),
):
    filings = list(
        session.scalars(
            select(FilingVersion)
            .where(FilingVersion.company_id == company_id, FilingVersion.status == "facts_verified")
            .order_by(FilingVersion.period_end)
        )
    )
    if not filings:
        return envelope(request, [])
    comparable = len({(item.report_type, item.scope, item.accounting_standard, item.period_basis) for item in filings}) == 1
    if not comparable:
        return envelope(request, [{"status": "not_comparable", "explanation_code": "FILING_SCOPE_OR_PERIOD_MISMATCH", "filing_version_ids": [item.id for item in filings]}])
    items = list(
        session.scalars(
            select(MetricResult).where(
                MetricResult.filing_version_id.in_([f.id for f in filings]),
                MetricResult.metric_code.in_(metric_codes),
                MetricResult.computation_mode == "strict_audited",
            )
        )
    )
    return envelope(
        request,
        [
            {
                "filing_version_id": item.filing_version_id,
                "metric_code": item.metric_code,
                "value": item.value,
                "status": item.applicability_status,
                "formula_version": item.formula_version,
                "quality_status": item.quality_status,
                "scope": item.scope,
                "period_basis": item.period_basis,
            }
            for item in items
        ],
    )


@app.post("/api/v1/comparisons")
def comparison(
    request: Request, payload: ComparisonRequest, session: Session = Depends(get_session)
):
    universe = session.scalar(select(PeerUniverse).where(PeerUniverse.version == payload.peer_universe_version, PeerUniverse.status == "active"))
    if universe is None or set(payload.peer_filing_ids) != set(universe.filing_ids) or len(universe.filing_ids) < 10 or universe.coverage < Decimal("0.80"):
        return envelope(
            request,
            {
                "status": "not_comparable",
                "explanation_code": "INSUFFICIENT_FROZEN_PEERS",
                "peer_universe_version": payload.peer_universe_version,
            },
        )
    results = list(
        session.scalars(
            select(MetricResult).where(
                MetricResult.filing_version_id.in_(payload.peer_filing_ids),
                MetricResult.metric_code == payload.metric_code,
                MetricResult.applicability_status == "calculated",
                MetricResult.computation_mode == "strict_audited",
            )
        )
    )
    if len(results) < 10 or len(results) / max(len(payload.peer_filing_ids), 1) < 0.80:
        return envelope(
            request,
            {
                "status": "not_comparable",
                "explanation_code": "INSUFFICIENT_QUALIFIED_PEERS",
                "peer_universe_version": payload.peer_universe_version,
            },
        )
    values = [float(item.value) for item in results if item.value is not None]
    if len(values) < 10:
        return envelope(request, {"status": "not_comparable", "explanation_code": "INSUFFICIENT_VALUES", "peer_universe_version": payload.peer_universe_version})
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    mad = statistics.median(deviations)
    snapshot = hashlib.sha256(__import__("json").dumps({"metric": payload.metric_code, "peers": payload.peer_filing_ids, "coverage": str(payload.coverage), "version": payload.peer_universe_version}, sort_keys=True).encode()).hexdigest()
    run = ComparisonRun(peer_universe_version=payload.peer_universe_version, metric_code=payload.metric_code, input_snapshot_hash=snapshot, status="completed", output={"sample_count": len(values), "coverage": str(payload.coverage), "median": median, "mad": mad})
    session.add(run)
    session.commit()
    return envelope(request, {"status": "completed", "comparison_run_id": run.id, "peer_universe_version": payload.peer_universe_version, "sample_count": len(values), "coverage": payload.coverage, "median": median, "mad": mad})


@app.post("/api/v1/peer-universes", status_code=201)
def create_peer_universe(request: Request, payload: dict, session: Session = Depends(get_session)):
    required = {"version", "industry_code", "taxonomy_version", "data_cutoff", "filing_ids", "coverage"}
    if not required.issubset(payload):
        raise HTTPException(422, "PEER_UNIVERSE_FIELDS_REQUIRED")
    ids = list(dict.fromkeys(int(value) for value in payload["filing_ids"]))
    coverage = Decimal(str(payload["coverage"]))
    status = "active" if len(ids) >= 10 and coverage >= Decimal("0.80") else "candidate"
    universe = PeerUniverse(version=str(payload["version"]), industry_code=str(payload["industry_code"]), taxonomy_version=str(payload["taxonomy_version"]), data_cutoff=date.fromisoformat(str(payload["data_cutoff"])), filing_ids=ids, coverage=coverage, status=status, approval_trace=payload.get("approval_trace", []))
    session.add(universe)
    session.commit()
    return envelope(request, {"version": universe.version, "status": universe.status, "sample_count": len(ids), "coverage": coverage})


@app.post("/api/v1/filings/{filing_version_id}/analyses", status_code=202)
def create_analysis(
    request: Request,
    filing_version_id: int,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
):
    filing = get_filing(session, filing_version_id)
    if filing.status not in {"facts_verified", "metrics_ready", "analysis_ready"}:
        raise HTTPException(409, "STRICT_AUDITED_INPUT_REQUIRED")
    package = build_evidence_package(session, filing_version_id)
    if not package["metrics"]:
        raise HTTPException(409, "STRICT_METRICS_REQUIRED")
    snapshot_hash = hashlib.sha256(
        __import__("json").dumps(package, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    run = AnalysisRun(filing_version_id=filing_version_id, input_snapshot_hash=snapshot_hash)
    session.add(run)
    session.flush()
    record_audit_event(
        session,
        filing_version_id,
        category="generation",
        action="analysis_queued",
        status="queued",
        tool_name="analysis_orchestrator",
        tool_version=run.orchestration_version,
        input_hash=snapshot_hash,
        detail={"analysis_run_id": run.id, "validator_version": run.validator_version},
    )
    session.commit()
    workflow_runtime.dispatch_analyze(run.id, background)
    return envelope(request, {"analysis_run_id": run.id, "status": run.status})


def _background_analysis(run_id: int) -> None:
    from finance_adapters.persistence.database import SessionLocal

    with SessionLocal() as session:
        run_analysis(session, run_id)


workflow_runtime = WorkflowRuntime(extract=_background_extract, analyze=_background_analysis)


@app.get("/api/v1/analyses/{analysis_run_id}")
def get_analysis(request: Request, analysis_run_id: int, session: Session = Depends(get_session)):
    run = session.get(AnalysisRun, analysis_run_id)
    if run is None:
        raise HTTPException(404, "ANALYSIS_NOT_FOUND")
    return envelope(
        request,
        {
            "analysis_run_id": run.id,
            "status": run.status,
            "output": run.output,
            "failure_reason": run.failure_reason,
            "input_snapshot_hash": run.input_snapshot_hash,
            "tool_trace_hash": run.tool_trace_hash,
            "validator_version": run.validator_version,
            "orchestration_version": run.orchestration_version,
            "tool_trace": run.tool_trace,
            "invalidated_at": run.invalidated_at,
        },
    )


@app.get("/api/v1/filings/{filing_version_id}/audit-trail")
def get_audit_trail(request: Request, filing_version_id: int, session: Session = Depends(get_session)):
    filing = get_filing(session, filing_version_id)
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.filing_version_id == filing.id)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )
    return envelope(
        request,
        {
            "filing_version_id": filing.id,
            "reproduction": {
                "file_sha256": filing.file_sha256,
                "reporting_period": f"{filing.period_start.isoformat()}~{filing.period_end.isoformat()}",
                "report_type": filing.report_type,
                "period_basis": filing.period_basis,
                "source_type": filing.source_type,
                "source_uri": filing.source_uri,
            },
            "events": [
                {
                    "id": item.id,
                    "category": item.category,
                    "action": item.action,
                    "status": item.status,
                    "source_ref": item.source_ref,
                    "tool_name": item.tool_name,
                    "tool_version": item.tool_version,
                    "input_hash": item.input_hash,
                    "output_hash": item.output_hash,
                    "detail": item.detail,
                    "occurred_at": item.occurred_at,
                }
                for item in events
            ],
        },
    )


@app.get("/api/v1/comparisons/{comparison_run_id}")
def get_comparison(request: Request, comparison_run_id: int, session: Session = Depends(get_session)):
    run = session.get(ComparisonRun, comparison_run_id)
    if run is None:
        raise HTTPException(404, "COMPARISON_NOT_FOUND")
    return envelope(request, {"comparison_run_id": run.id, "status": run.status, "peer_universe_version": run.peer_universe_version, "metric_code": run.metric_code, "input_snapshot_hash": run.input_snapshot_hash, "output": run.output, "failure_reason": run.failure_reason})


@app.post("/api/v1/filings/{filing_version_id}/anomalies")
def calculate_anomalies(request: Request, filing_version_id: int, session: Session = Depends(get_session)):
    filing = get_filing(session, filing_version_id)
    metrics = list(session.scalars(select(MetricResult).where(MetricResult.filing_version_id == filing.id, MetricResult.computation_mode == "strict_audited")))
    observations = []
    for metric in metrics:
        if metric.applicability_status in {"not_applicable", "not_available", "insufficient_data"}:
            observations.append(AnomalyObservation(filing_version_id=filing.id, rule_code="metric_data_quality", status="data_quality_risk", explanation=f"{metric.metric_code}:{metric.applicability_status}", input_snapshot_hash=metric.input_snapshot_hash, suppression_reason="STRICT_INPUT_UNAVAILABLE"))
    if not observations:
        observations.append(AnomalyObservation(filing_version_id=filing.id, rule_code="no_quality_failure", status="not_triggered", explanation="当前没有满足异常观察触发条件的质量问题"))
    session.add_all(observations)
    session.commit()
    return envelope(request, [{"id": item.id, "rule_code": item.rule_code, "status": item.status, "explanation": item.explanation, "suppression_reason": item.suppression_reason} for item in observations])


@app.post("/api/v1/exports")
def export(
    request: Request,
    filing_version_id: int = Query(...),
    output_format: str = Query("html", pattern="^(html|pdf|csv)$"),
    session: Session = Depends(get_session),
):
    get_filing(session, filing_version_id)
    path = generate_export(session, filing_version_id, output_format)
    contents = path.read_bytes()
    record_audit_event(
        session,
        filing_version_id,
        category="output",
        action="report_exported",
        status="completed",
        source_ref=str(path.name),
        tool_name="report_renderer",
        tool_version="export-v1",
        input_hash=stable_hash({"filing_id": filing_version_id, "format": output_format}),
        output_hash=hashlib.sha256(contents).hexdigest(),
        detail={"format": output_format, "file_name": path.name, "bytes": len(contents)},
    )
    session.commit()
    return envelope(
        request,
        {
            "status": "completed",
            "format": output_format,
            "download_url": f"/api/v1/exports/download/{path.name}",
        },
    )


@app.get("/api/v1/exports/download/{name}")
def download_export(name: str):
    path = settings.storage_dir / "exports" / Path(name).name
    if not path.exists():
        raise HTTPException(404, "EXPORT_NOT_FOUND")
    return FileResponse(path, filename=path.name)
