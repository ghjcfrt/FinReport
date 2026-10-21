from __future__ import annotations

import hashlib
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event, Thread

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
from finance_adapters.exports.reasoning import provider_reasoning
from finance_adapters.exports.report_renderer import generate_export
from finance_adapters.parsing.pdf_parser import run_extraction
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.database import get_session, init_database
from finance_adapters.persistence.extraction_lifecycle import (
    queue_extraction,
    recover_expired_extractions,
)
from finance_adapters.persistence.metric_gateway import (
    FORMULA_VERSION,
    calculate_metrics,
    current_metric_snapshot,
    review_evidence_digest,
)
from finance_adapters.persistence.models import (
    AnalysisRun,
    AnomalyObservation,
    AuditEvent,
    Company,
    ComparisonRun,
    EvidenceFragment,
    ExtractionJob,
    FactDimension,
    FactReview,
    FileScan,
    FilingVersion,
    FinancialFact,
    MetricResult,
    OcrCall,
    TableCandidateRecord,
    ValidationResult,
)
from finance_adapters.persistence.peer_gateway import (
    PeerGateError,
    run_peer_comparison,
    save_peer_candidate,
)
from finance_adapters.persistence.review_gateway import recompute_filing_quality
from finance_adapters.providers.llm_analysis import build_evidence_package, run_analysis
from finance_adapters.providers.model_clients import BaiduUnlimitedOCRAdapter
from finance_adapters.providers.reproduction import build_reproduction_bundle
from finance_adapters.security import ScanDisposition, database_update, scanner
from finance_adapters.security.file_gate import scanned_object_path
from finance_application.workflows.runtime import WorkflowRuntime
from finance_domain.analysis.contracts import (
    ComparisonRequest,
    ExtractionRequest,
    MetricRequest,
    ModelDiscoveryRequest,
    ModelSettingsUpdate,
    ReviewRequest,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from finreport_api.analysis_workflow_routes import router as analysis_workflow_router
from finreport_api.annual_disclosure_routes import router as annual_disclosure_router
from finreport_api.auto_validation_routes import router as auto_validation_router
from finreport_api.chapter_analysis_routes import router as chapter_analysis_router
from finreport_api.comparison_routes import router as comparison_router
from finreport_api.grounded_analysis_routes import router as grounded_analysis_router
from finreport_api.ocr_mapping_routes import router as ocr_mapping_router
from finreport_api.report_analysis_routes import AnalysisReasoningEnvelope
from finreport_api.report_analysis_routes import router as report_analysis_router

app = FastAPI(title="FinReport API", version="1.0.0")
app.include_router(comparison_router)
app.include_router(ocr_mapping_router)
app.include_router(annual_disclosure_router)
app.include_router(analysis_workflow_router)
app.include_router(report_analysis_router)
app.include_router(grounded_analysis_router)
app.include_router(auto_validation_router)
app.include_router(chapter_analysis_router)
_recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extraction-recovery")
_recovery_stop = Event()
_recovery_thread: Thread | None = None


@app.on_event("startup")
def startup() -> None:
    global _recovery_thread
    journal_logger = logging.getLogger("finreport.run_journal")
    journal_logger.setLevel(logging.INFO)
    if not journal_logger.handlers:
        journal_logger.addHandler(logging.StreamHandler())
    init_database()
    _recover_extractions()
    _recovery_stop.clear()
    _recovery_thread = Thread(target=_recovery_loop, daemon=True, name="extraction-recovery-monitor")
    _recovery_thread.start()


def _recovery_loop() -> None:
    import logging

    while not _recovery_stop.wait(30):
        try:
            _recover_extractions()
        except Exception:
            logging.getLogger(__name__).exception("Extraction recovery scan failed")


@app.on_event("shutdown")
def shutdown() -> None:
    _recovery_stop.set()
    if _recovery_thread is not None:
        _recovery_thread.join(timeout=2)


def _recover_extractions(job_id: int | None = None) -> None:
    from finance_adapters.persistence.database import SessionLocal

    with SessionLocal() as session:
        recovered = recover_expired_extractions(session, job_id)
    for recovered_id in recovered:
        _recovery_executor.submit(_background_extract, recovered_id)


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
        "rule_version": FORMULA_VERSION,
    }


def get_filing(session: Session, filing_id: int) -> FilingVersion:
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise HTTPException(404, "FILING_NOT_FOUND")
    return filing


@app.get("/health")
def health(request: Request):
    return envelope(request, {"status": "ok"})


@app.get("/api/v1/security/scanner")
def scanner_status(request: Request):
    """Expose scanner/database readiness without exposing local paths or secrets."""
    return envelope(request, database_update.get_status())


@app.post("/api/v1/security/scanner/update", status_code=202)
async def update_scanner_database(request: Request):
    """Queue one controlled official ClamAV database update.

    The operation is asynchronous. The updater stages and validates a candidate
    before pointer activation; the Supervisor then validates and loads it. A
    running update is rejected to avoid competing freshclam writers.
    """
    try:
        result = await database_update.start_update()
    except RuntimeError as exc:
        code = str(exc)
        status = 409 if code == "AV_UPDATE_ALREADY_RUNNING" else 503
        raise HTTPException(status, code) from exc
    return envelope(request, result)


@app.get("/api/v1/settings/models")
def model_settings(request: Request):
    ocr_is_baidu = BaiduUnlimitedOCRAdapter.matches(settings.ocr_provider)
    ocr_requires_key = not ocr_is_baidu or (
        BaiduUnlimitedOCRAdapter.resolve_protocol(settings.ocr_base_url, settings.ocr_protocol) == "baidu_cloud"
    )
    ocr_configured = bool(
        settings.ocr_base_url and settings.ocr_model
        and (settings.ocr_api_key or not ocr_requires_key)
        and not BaiduUnlimitedOCRAdapter.is_retired_ocr(settings.ocr_provider, settings.ocr_model)
    )
    return envelope(request, {
        "analysis": {"provider": settings.llm_provider, "base_url": settings.llm_base_url, "model": settings.llm_model, "configured": bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model), "credentials_configured": bool(settings.llm_api_key)},
        "ocr": {"provider": settings.ocr_provider, "base_url": settings.ocr_base_url, "model": settings.ocr_model, "protocol": settings.ocr_protocol, "configured": ocr_configured, "credentials_configured": bool(settings.ocr_api_key)},
        "supported_providers": ["openai", "anthropic", "baidu-unlimited-ocr"],
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
    is_baidu_ocr = stage == "ocr" and BaiduUnlimitedOCRAdapter.matches(payload.provider)
    if is_baidu_ocr and BaiduUnlimitedOCRAdapter.resolve_protocol(payload.base_url, settings.ocr_protocol) == "baidu_cloud":
        # The managed parser has a fixed model and no OpenAI /models endpoint.
        return envelope(request, {"models": ["baidu/Unlimited-OCR"], "discovery_source": "provider_catalog"})
    configured_key = settings.llm_api_key if stage == "analysis" else settings.ocr_api_key
    api_key = payload.api_key or configured_key
    if not api_key and not is_baidu_ocr:
        raise HTTPException(422, "API_KEY_REQUIRED_FOR_MODEL_DISCOVERY")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
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
    if period_start > period_end or report_type != period_basis:
        raise HTTPException(422, "REPORTING_PERIOD_INVALID")
    if period_basis not in {"annual", "semi_annual", "quarterly"}:
        raise HTTPException(422, "REPORTING_PERIOD_UNSUPPORTED")
    if period_basis == "annual" and (
        period_start.year != period_end.year
        or (period_start.month, period_start.day) != (1, 1)
        or (period_end.month, period_end.day) != (12, 31)
    ):
        raise HTTPException(422, "ANNUAL_PERIOD_MUST_COVER_FULL_YEAR")
    if period_basis == "semi_annual" and (
        period_start.year != period_end.year
        or (period_start.month, period_start.day) != (1, 1)
        or (period_end.month, period_end.day) != (6, 30)
    ):
        raise HTTPException(422, "SEMI_ANNUAL_PERIOD_INVALID")
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
        existing_company = session.get(Company, duplicate.company_id)
        if (existing_company.stock_code != stock_code
                or duplicate.period_start != period_start or duplicate.period_end != period_end
                or duplicate.report_type != report_type or duplicate.period_basis != period_basis):
            raise HTTPException(409, "DUPLICATE_FILE_METADATA_MISMATCH")
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
    payload: ExtractionRequest | None = None,
):
    method = payload.extraction_method if payload else "pdf_text"
    pages = payload.page_numbers if payload else None
    filing = get_filing(session, filing_version_id)
    try:
        scanned_object_path(session, filing)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    existing = session.scalar(
        select(ExtractionJob)
        .where(
            ExtractionJob.filing_version_id == filing.id,
            ExtractionJob.status.in_(["queued", "running"]),
        )
        .order_by(ExtractionJob.id.desc())
    )
    if existing:
        if existing.extraction_method != method:
            raise HTTPException(409, "EXTRACTION_METHOD_CONFLICT_WITH_ACTIVE_JOB")
        if existing.page_numbers != pages:
            raise HTTPException(409, "EXTRACTION_PAGE_RANGE_CONFLICT_WITH_ACTIVE_JOB")
        for recovered_id in recover_expired_extractions(session, existing.id):
            workflow_runtime.dispatch_extract(recovered_id, background)
        session.refresh(existing)
        return envelope(request, {"job_id": existing.id, "status": existing.status, "reused": True,
                                  "extraction_method": existing.extraction_method,
                                  "review_required": True})
    latest = session.scalar(select(ExtractionJob).where(
        ExtractionJob.filing_version_id == filing.id,
    ).order_by(ExtractionJob.id.desc()).limit(1))
    if (latest and latest.status == "completed" and latest.extraction_method == method
            and latest.page_numbers == pages):
        return envelope(request, {"job_id": latest.id, "status": latest.status, "reused": True,
                                  "extraction_method": latest.extraction_method,
                                  "review_required": True})
    job = ExtractionJob(filing_version_id=filing.id, extraction_method=method, page_numbers=pages)
    queue_extraction(job)
    session.add(job)
    filing.status = "extracting"
    session.query(ValidationResult).filter(
        ValidationResult.filing_version_id == filing.id,
        ValidationResult.rule_code == "primary_statement_extraction",
    ).delete()
    session.add(ValidationResult(
        filing_version_id=filing.id, rule_code="primary_statement_extraction", severity="fatal",
        status="failed", actual_value="EXTRACTION_AND_HUMAN_REVIEW_PENDING",
        expected_value="completed_extraction_and_current_fact_approvals",
    ))
    session.query(AnalysisRun).filter(AnalysisRun.filing_version_id == filing.id).update(
        {AnalysisRun.invalidated_at: datetime.now(UTC), AnalysisRun.status: "invalidated"},
        synchronize_session=False,
    )
    record_audit_event(
        session, filing.id, category="workflow", action="extraction_method_selected",
        status="queued", tool_name="extraction_lifecycle", tool_version="method-v1",
        detail={"extraction_method": method, "page_numbers": pages,
                "processing_scope": "selected_pages" if pages else "whole_document", "review_required": True},
    )
    session.commit()
    workflow_runtime.dispatch_extract(job.id, background)
    return envelope(request, {"job_id": job.id, "status": job.status, "reused": False,
                              "extraction_method": job.extraction_method, "page_numbers": job.page_numbers,
                              "review_required": True})


def _background_extract(job_id: int) -> None:
    from finance_adapters.persistence.database import SessionLocal

    with SessionLocal() as session:
        run_extraction(session, job_id)


@app.get("/api/v1/jobs/{job_id}")
def get_job(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = session.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(404, "JOB_NOT_FOUND")
    for recovered_id in recover_expired_extractions(session, job.id):
        _recovery_executor.submit(_background_extract, recovered_id)
    session.refresh(job)
    review_count = session.scalar(select(func.count()).select_from(FinancialFact).where(
        FinancialFact.filing_version_id == job.filing_version_id,
        FinancialFact.review_status == "needs_review",
    ))
    if job.extraction_method == "ocr":
        review_count = session.scalar(select(func.count()).select_from(TableCandidateRecord).join(
            OcrCall, TableCandidateRecord.ocr_call_id == OcrCall.id,
        ).where(
            TableCandidateRecord.extraction_job_id == job.id,
            TableCandidateRecord.review_required.is_(True),
            OcrCall.status == "completed",
            OcrCall.started_at >= job.started_at,
        )) if job.started_at else 0
    return envelope(
        request,
        {
            "job_id": job.id,
            "filing_version_id": job.filing_version_id,
            "extraction_method": job.extraction_method,
            "review_required": True,
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
    newer_job = session.scalar(select(ExtractionJob.id).where(
        ExtractionJob.filing_version_id == job.filing_version_id,
        ExtractionJob.id > job.id,
    ).limit(1))
    if newer_job is not None:
        raise HTTPException(409, "EXTRACTION_JOB_SUPERSEDED")
    recovered = recover_expired_extractions(session, job.id)
    session.refresh(job)
    if recovered:
        workflow_runtime.dispatch_extract(job.id, background)
        return envelope(request, {"job_id": job.id, "status": job.status, "reused": True,
                                  "extraction_method": job.extraction_method,
                                  "review_required": True})
    if job.status != "failed" and not (job.status == "completed" and job.stage == "needs_review"):
        raise HTTPException(409, "JOB_NOT_RETRYABLE")
    try:
        scanned_object_path(session, get_filing(session, job.filing_version_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    queue_extraction(job)
    filing = get_filing(session, job.filing_version_id)
    filing.status = "extracting"
    session.query(ValidationResult).filter(
        ValidationResult.filing_version_id == filing.id,
        ValidationResult.rule_code == "primary_statement_extraction",
    ).delete()
    session.add(ValidationResult(
        filing_version_id=filing.id, rule_code="primary_statement_extraction", severity="fatal",
        status="failed", actual_value="EXTRACTION_AND_HUMAN_REVIEW_PENDING",
        expected_value="completed_extraction_and_current_fact_approvals",
    ))
    session.query(AnalysisRun).filter(AnalysisRun.filing_version_id == filing.id).update(
        {AnalysisRun.invalidated_at: datetime.now(UTC), AnalysisRun.status: "invalidated"},
        synchronize_session=False,
    )
    session.commit()
    workflow_runtime.dispatch_extract(job.id, background)
    return envelope(request, {"job_id": job.id, "status": job.status, "reused": True,
                              "extraction_method": job.extraction_method, "review_required": True})


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = session.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(404, "JOB_NOT_FOUND")
    changed = session.execute(update(ExtractionJob).where(
        ExtractionJob.id == job_id, ExtractionJob.status.in_(["queued", "running"]),
    ).values(
        status="cancelled", stage="cancelled", error_code="CANCELLED",
        error_detail="Cancelled by user", retryable=False, finished_at=datetime.now(UTC),
        owner_token=None, lease_expires_at=None,
    ), execution_options={"synchronize_session": False}).rowcount
    if not changed:
        session.rollback()
        raise HTTPException(409, "JOB_NOT_CANCELLABLE")
    filing = session.get(FilingVersion, job.filing_version_id)
    if filing is not None and filing.status == "extracting":
        filing.status = "failed"
    record_audit_event(
        session, job.filing_version_id, category="workflow", action="pdf_extraction_cancelled",
        status="cancelled", tool_name="extraction_lifecycle", tool_version="lease-v1",
        detail={"extraction_job_id": job_id, "reason": "USER_CANCELLED"},
    )
    session.commit()
    session.refresh(job)
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
    query = select(FinancialFact).where(
        FinancialFact.filing_version_id == filing_version_id,
        FinancialFact.review_status != "superseded",
    )
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
                "statement_type": fact.statement_type,
                "period_start": fact.period_start,
                "period_end": fact.period_end,
                "comparative_period": fact.comparative_period,
                "is_restatement": fact.is_restatement,
                "raw_value": fact.raw_value,
                "evidence": evidence,
                "extractor": evidence.extractor if evidence else None,
            }
        )
    return envelope(request, facts)


@app.get("/api/v1/filings/{filing_version_id}/validations")
def list_validations(request: Request, filing_version_id: int, session: Session = Depends(get_session)):
    filing = get_filing(session, filing_version_id)
    results = list(session.scalars(select(ValidationResult).where(
        ValidationResult.filing_version_id == filing.id,
    ).order_by(ValidationResult.id)))
    return envelope(request, {"filing_status": filing.status, "results": results})


@app.get("/api/v1/filings/{filing_version_id}/tables")
def list_tables(
    request: Request,
    filing_version_id: int,
    page_no: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
):
    """Read complete candidate tables and measured visual indentation for review."""
    get_filing(session, filing_version_id)
    latest_job = session.scalar(
        select(ExtractionJob)
        .where(ExtractionJob.filing_version_id == filing_version_id)
        .order_by(ExtractionJob.id.desc())
        .limit(1)
    )
    if latest_job is None:
        return envelope(request, [])
    query = select(TableCandidateRecord).where(
        TableCandidateRecord.extraction_job_id == latest_job.id
    )
    ocr_calls = {}
    if latest_job.extraction_method == "ocr":
        if latest_job.status != "completed" or latest_job.started_at is None:
            return envelope(request, [])
        latest_calls = {}
        for call in session.scalars(select(OcrCall).where(
            OcrCall.extraction_job_id == latest_job.id,
            OcrCall.started_at >= latest_job.started_at,
        ).order_by(OcrCall.id)):
            latest_calls[call.page_no] = call
        ocr_calls = {call.id: call for call in latest_calls.values() if call.status == "completed"}
        query = query.where(TableCandidateRecord.ocr_call_id.in_(ocr_calls))
    if page_no is not None:
        query = query.where(TableCandidateRecord.page_no == page_no)
    tables = []
    for candidate in session.scalars(query.order_by(TableCandidateRecord.page_no, TableCandidateRecord.id)):
        ocr_call = ocr_calls.get(candidate.ocr_call_id)
        records = candidate.rows
        measured = bool(records) and isinstance(records[0], dict) and records[0].get("kind") == "header"
        if measured:
            header = records[0]
            columns = header["cells"]
            rows = [row for row in records[1:] if row.get("kind") == "body"]
        else:
            header = {}
            columns = ["项目", "原数", "金额列"]
            body = records
            if candidate.extractor == "ocr-table-review-v1" and records:
                columns, body = records[0], records[1:]
            rows = [
                {"row_index": index, "cells": cells, "raw_label": cells[0] if cells else "",
                 "row_bbox": None, "label_layout": None}
                for index, cells in enumerate(body)
            ]
        tables.append(
            {
                "id": candidate.id,
                "extraction_job_id": candidate.extraction_job_id,
                "page_no": candidate.page_no,
                "table_name": header.get("table_name", "原表候选"),
                "statement_type": header.get("statement_type"),
                "columns": columns,
                "column_roles": header.get("column_roles", []),
                "rows": rows,
                "table_bbox": header.get("table_bbox"),
                "page_sha256": ocr_call.page_sha256 if ocr_call else header.get("page_sha256"),
                "source_text": ocr_call.response_text if ocr_call else None,
                "ocr_call_id": candidate.ocr_call_id,
                "response_sha256": ocr_call.response_hash if ocr_call else None,
                "layout_available": measured,
                "extractor": candidate.extractor,
                "confidence": candidate.confidence,
                "review_required": candidate.review_required,
            }
        )
    return envelope(request, tables)


@app.patch("/api/v1/facts/{fact_id}/review")
def review_fact(
    request: Request, fact_id: int, payload: ReviewRequest, session: Session = Depends(get_session)
):
    fact = session.get(FinancialFact, fact_id)
    if fact is None:
        raise HTTPException(404, "FACT_NOT_FOUND")
    if fact.review_status == "superseded":
        raise HTTPException(409, "FACT_SUPERSEDED")
    reviewer_id = payload.reviewer_id.strip()
    if not reviewer_id:
        raise HTTPException(422, "REVIEWER_REQUIRED")
    # Reject stale OCR jobs before saving an approval. Re-map corrections on the
    # page image so the original OCR observation and human correction stay linked.
    from finance_adapters.persistence.mapping_gateway import MAPPING_VERSION, latest_ocr_job
    try:
        latest_ocr_job(session, session.get(FilingVersion, fact.filing_version_id), required=False)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if payload.decision == "corrected":
        mapped = session.scalar(select(EvidenceFragment.id).where(
            EvidenceFragment.fact_id == fact.id, EvidenceFragment.extractor == MAPPING_VERSION,
        ))
        if mapped is not None:
            raise HTTPException(409, "OCR_CORRECTION_REQUIRES_IMAGE_MAPPING")
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
            assurance_level=fact.assurance_level,
            comparative_period=fact.comparative_period,
            is_restatement=fact.is_restatement,
        )
        session.add(new_fact)
        session.flush()
        for dimension in session.scalars(select(FactDimension).where(FactDimension.fact_id == fact.id)):
            session.add(FactDimension(fact_id=new_fact.id, dimension_name=dimension.dimension_name,
                                     dimension_value=dimension.dimension_value))
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
                    raw_value=old_evidence.raw_value,
                    bounding_box=old_evidence.bounding_box,
                    page_sha256=old_evidence.page_sha256,
                    confidence=old_evidence.confidence,
                    extractor=old_evidence.extractor,
                )
            )
        fact.review_status, fact.quality_status = "superseded", "superseded"
        fact = new_fact
    review = session.scalar(select(FactReview).where(
        FactReview.fact_id == fact.id, FactReview.reviewer_id == reviewer_id,
    ))
    session.flush()
    evidence_digest = review_evidence_digest(session, fact)
    if review is None:
        review = FactReview(
            fact_id=fact.id,
            reviewer_id=reviewer_id,
            decision=payload.decision,
            reason=payload.reason,
            evidence_digest=evidence_digest,
        )
        session.add(review)
    else:
        review.decision, review.reason = payload.decision, payload.reason
        review.reviewed_at = datetime.now(UTC)
        review.evidence_digest = evidence_digest
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
            tool_version=FORMULA_VERSION,
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
            .where(FilingVersion.company_id == company_id,
                   FilingVersion.status.in_(["facts_verified", "metrics_ready", "analysis_ready"]))
            .order_by(FilingVersion.period_end)
        )
    )
    if not filings:
        return envelope(request, [])
    comparable = len({(item.report_type, item.scope, item.accounting_standard, item.period_basis) for item in filings}) == 1
    if not comparable:
        return envelope(request, [{"status": "not_comparable", "explanation_code": "FILING_SCOPE_OR_PERIOD_MISMATCH", "filing_version_ids": [item.id for item in filings]}])
    periods: dict[tuple[date, date], list[int]] = {}
    for filing in filings:
        periods.setdefault((filing.period_start, filing.period_end), []).append(filing.id)
    ambiguous = [ids for ids in periods.values() if len(ids) > 1]
    if ambiguous:
        return envelope(request, [{
            "status": "not_comparable", "explanation_code": "AMBIGUOUS_FILING_VERSIONS_FOR_PERIOD",
            "filing_version_ids": [item for ids in ambiguous for item in ids],
        }])
    filing_by_id = {filing.id: filing for filing in filings}
    items = list(
        session.scalars(
            select(MetricResult).where(
                MetricResult.filing_version_id.in_([f.id for f in filings]),
                MetricResult.metric_code.in_(metric_codes),
                MetricResult.computation_mode == "strict_audited",
                MetricResult.quality_status == "strict_audited",
                MetricResult.formula_version == FORMULA_VERSION,
                MetricResult.scope == filings[0].scope,
                MetricResult.period_basis == filings[0].period_basis,
            )
        )
    )
    current_snapshots = {}
    for filing in filings:
        try:
            current_snapshots[filing.id] = current_metric_snapshot(session, filing)
        except ValueError:
            current_snapshots[filing.id] = None
    items.sort(key=lambda item: (filing_by_id[item.filing_version_id].period_end,
                                 item.metric_code, item.id))
    return envelope(
        request,
        [
            {
                "filing_version_id": item.filing_version_id,
                "metric_code": item.metric_code,
                "unit": item.unit,
                "period_start": filing_by_id[item.filing_version_id].period_start,
                "period_end": filing_by_id[item.filing_version_id].period_end,
                "value": item.value if current_snapshots[item.filing_version_id]
                and item.input_snapshot_hash == current_snapshots[item.filing_version_id] else None,
                "status": item.applicability_status if current_snapshots[item.filing_version_id]
                and item.input_snapshot_hash == current_snapshots[item.filing_version_id] else "stale_inputs",
                "explanation_code": item.explanation_code if current_snapshots[item.filing_version_id]
                and item.input_snapshot_hash == current_snapshots[item.filing_version_id] else "METRIC_INPUT_SNAPSHOT_CHANGED",
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
    # No authenticated approval/provenance adapter exists yet: production fails closed.
    return envelope(request, run_peer_comparison(session, payload.model_dump(mode="json")))


@app.post("/api/v1/peer-universes", status_code=201)
def create_peer_universe(request: Request, payload: dict, session: Session = Depends(get_session)):
    try:
        return envelope(request, save_peer_candidate(session, payload))
    except PeerGateError as exc:
        status = 409 if exc.code == "PEER_UNIVERSE_VERSION_ALREADY_EXISTS" else 422
        raise HTTPException(status, exc.code) from exc


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
    try:
        package = build_evidence_package(session, filing_version_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
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


@app.get("/api/v1/analyses/{analysis_run_id}", response_model=AnalysisReasoningEnvelope)
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
            "provider_reasoning": provider_reasoning(run.tool_trace),
        },
    )


@app.get("/api/v1/analyses/{analysis_run_id}/reproduction")
def export_reproduction(
    analysis_run_id: int, session: Session = Depends(get_session),
):
    run = session.get(AnalysisRun, analysis_run_id)
    if run is None:
        raise HTTPException(404, "ANALYSIS_NOT_FOUND")
    try:
        bundle = build_reproduction_bundle(run)
        if run.status == "completed":
            package = build_evidence_package(session, run.filing_version_id)
            if stable_hash(package) != run.input_snapshot_hash:
                raise ValueError("REPRODUCTION_INPUT_SNAPSHOT_CHANGED")
            current_input = {"status": "matches_execution_snapshot",
                             "current_snapshot_hash": stable_hash(package)}
        else:
            # Preserve failed execution evidence even after its inputs changed or
            # no longer meet strict gates; it is not a current financial report.
            try:
                package = build_evidence_package(session, run.filing_version_id)
                current_hash = stable_hash(package)
                current_input = {
                    "status": "matches_execution_snapshot" if current_hash == run.input_snapshot_hash
                    else "changed_since_execution", "current_snapshot_hash": current_hash,
                }
            except ValueError as exc:
                current_input = {"status": "unavailable_under_current_gates", "reason": str(exc)}
        bundle["payload"]["current_input_status"] = {
            **current_input, "execution_snapshot_hash": run.input_snapshot_hash,
            "export_kind": "current_completed_analysis" if run.status == "completed"
            else "historical_failed_analysis_diagnostic",
        }
        bundle["payload_sha256"] = stable_hash(bundle["payload"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return JSONResponse(
        content=bundle,
        headers={"Content-Disposition": f'attachment; filename="analysis-{run.id}-reproduction.json"'},
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
    try:
        path = generate_export(session, filing_version_id, output_format)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
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
