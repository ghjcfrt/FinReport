"""Manual, image-located OCR admission. Fact approvals remain separate."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from finance_adapters.persistence.database import get_session
from finance_adapters.persistence.mapping_gateway import (
    _source_call,
    complete_ocr_mapping,
    confirm_ocr_audit,
    latest_ocr_job,
    map_ocr_fact,
    mapping_context,
    render_ocr_page,
)
from finance_adapters.persistence.models import FilingVersion
from finance_adapters.persistence.review_gateway import recompute_filing_quality
from finance_domain.analysis.contracts import (
    OcrAuditConfirmationRequest,
    OcrFactMappingRequest,
    OcrMappingCompletionRequest,
)
from sqlalchemy.orm import Session

router = APIRouter()


def _filing(session: Session, filing_id: int) -> FilingVersion:
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise HTTPException(404, "FILING_NOT_FOUND")
    return filing


def _response(request: Request, data):
    from finreport_api.routes import envelope

    return envelope(request, data)


@router.get("/api/v1/filings/{filing_version_id}/ocr-mapping-context")
def get_ocr_mapping_context(request: Request, filing_version_id: int,
                            session: Session = Depends(get_session)):
    try:
        return _response(request, mapping_context(session, _filing(session, filing_version_id)))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/v1/filings/{filing_version_id}/ocr-pages/{page_no}.png")
def get_ocr_page_image(filing_version_id: int, page_no: int,
                        ocr_call_id: int = Query(gt=0), session: Session = Depends(get_session)):
    from finance_adapters.persistence.models import OcrCall

    filing = _filing(session, filing_version_id)
    try:
        job = latest_ocr_job(session, filing)
        candidate = session.get(OcrCall, ocr_call_id)
        if candidate is None or candidate.page_no != page_no:
            raise ValueError("OCR_PAGE_CALL_MISMATCH")
        call = _source_call(session, job, candidate.id, candidate.page_sha256, candidate.response_hash)
        png, _, _ = render_ocr_page(session, filing, call)
        return Response(content=png, media_type="image/png", headers={
            "ETag": f'"{call.page_sha256}"', "Cache-Control": "no-store",
        })
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/v1/filings/{filing_version_id}/ocr-fact-mappings", status_code=201)
def create_ocr_fact_mapping(request: Request, filing_version_id: int,
                             payload: OcrFactMappingRequest, session: Session = Depends(get_session)):
    filing = _filing(session, filing_version_id)
    try:
        fact = map_ocr_fact(session, filing, payload)
        status = recompute_filing_quality(session, filing.id)
        return _response(request, {"fact_id": fact.id, "filing_status": status, "review_required": True})
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/v1/filings/{filing_version_id}/ocr-audit-confirmations")
def create_ocr_audit_confirmation(request: Request, filing_version_id: int,
                                   payload: OcrAuditConfirmationRequest,
                                   session: Session = Depends(get_session)):
    filing = _filing(session, filing_version_id)
    try:
        confirm_ocr_audit(session, filing, payload)
        status = recompute_filing_quality(session, filing.id)
        return _response(request, {"filing_status": status, "confirmed": True})
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/v1/filings/{filing_version_id}/ocr-mapping-completion")
def create_ocr_mapping_completion(request: Request, filing_version_id: int,
                                  payload: OcrMappingCompletionRequest,
                                  session: Session = Depends(get_session)):
    filing = _filing(session, filing_version_id)
    try:
        result = complete_ocr_mapping(session, filing, payload)
        result["filing_status"] = recompute_filing_quality(session, filing.id)
        return _response(request, result)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
