"""Explicit automatic native-fact admission; test labels are never product approvals."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from finance_adapters.persistence.auto_validation_gateway import (
    auto_validation_view,
    run_auto_validation,
)
from finance_adapters.persistence.database import get_session
from finance_adapters.persistence.models import FilingVersion
from sqlalchemy.orm import Session

router = APIRouter()


@router.post("/api/v1/filings/{filing_version_id}/auto-validation")
def validate_filing_automatically(request: Request, filing_version_id: int,
                                 session: Session = Depends(get_session)):
    from finreport_api.routes import envelope

    try:
        return envelope(request, run_auto_validation(session, filing_version_id))
    except (ValueError, OSError) as exc:
        session.rollback()
        raise HTTPException(404 if str(exc) == "FILING_NOT_FOUND" else 409, str(exc)) from exc


@router.get("/api/v1/filings/{filing_version_id}/auto-validation")
def get_filing_auto_validation(request: Request, filing_version_id: int,
                               session: Session = Depends(get_session)):
    from finreport_api.routes import envelope

    filing = session.get(FilingVersion, filing_version_id)
    if filing is None:
        raise HTTPException(404, "FILING_NOT_FOUND")
    return envelope(request, auto_validation_view(session, filing))
