"""Typed company disclosures and explicit human review; no audited-fact upgrade."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from finance_adapters.persistence.database import get_session
from finance_adapters.persistence.disclosure_gateway import (
    annual_disclosure_context,
    extract_annual_disclosures,
    map_ocr_disclosure,
    review_annual_disclosure,
)
from finance_adapters.persistence.models import FilingVersion
from finance_domain.filings.disclosures import DisclosureReviewRequest, OcrDisclosureMappingRequest
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


@router.get("/api/v1/filings/{filing_version_id}/annual-disclosures")
def get_annual_disclosures(request: Request, filing_version_id: int,
                           session: Session = Depends(get_session)):
    try:
        return _response(request, annual_disclosure_context(session, _filing(session, filing_version_id)))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/api/v1/filings/{filing_version_id}/annual-disclosures/{candidate_id}/review")
def patch_annual_disclosure_review(request: Request, filing_version_id: int, candidate_id: str,
                                   payload: DisclosureReviewRequest, session: Session = Depends(get_session)):
    try:
        return _response(request, review_annual_disclosure(session, _filing(session, filing_version_id), candidate_id, payload))
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/v1/filings/{filing_version_id}/annual-disclosures/extract")
def post_annual_disclosure_extraction(request: Request, filing_version_id: int,
                                      session: Session = Depends(get_session)):
    try:
        return _response(request, extract_annual_disclosures(session, _filing(session, filing_version_id)))
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/v1/filings/{filing_version_id}/ocr-annual-disclosure-mappings", status_code=201)
def post_ocr_annual_disclosure_mapping(request: Request, filing_version_id: int,
                                       payload: OcrDisclosureMappingRequest, session: Session = Depends(get_session)):
    try:
        return _response(request, map_ocr_disclosure(session, _filing(session, filing_version_id), payload))
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
