from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from finance_adapters.persistence.comparative_gateway import build_period_comparison
from finance_adapters.persistence.database import get_session
from sqlalchemy.orm import Session

router = APIRouter()


@router.get("/api/v1/filings/{filing_version_id}/period-comparison")
def period_comparison(
    request: Request, filing_version_id: int, session: Session = Depends(get_session),
):
    try:
        result = build_period_comparison(session, filing_version_id)
    except ValueError as exc:
        raise HTTPException(404 if str(exc) == "FILING_NOT_FOUND" else 409, str(exc)) from exc
    now = datetime.now(UTC).isoformat()
    return {"data": result, "request_id": getattr(request.state, "request_id", ""),
            "data_as_of": now, "cache_generated_at": now,
            "rule_version": result["version"]}
