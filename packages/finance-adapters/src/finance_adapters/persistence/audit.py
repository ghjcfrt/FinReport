from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditEvent


def stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def record_audit_event(
    session: Session,
    filing_id: int,
    *,
    category: str,
    action: str,
    status: str,
    source_ref: str | None = None,
    tool_name: str | None = None,
    tool_version: str | None = None,
    input_hash: str | None = None,
    output_hash: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        filing_version_id=filing_id,
        category=category,
        action=action,
        status=status,
        source_ref=source_ref,
        tool_name=tool_name,
        tool_version=tool_version,
        input_hash=input_hash,
        output_hash=output_hash,
        detail=detail or {},
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    return event
