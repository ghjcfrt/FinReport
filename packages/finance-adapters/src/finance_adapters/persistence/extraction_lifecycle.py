"""Durable extraction ownership, fenced transitions and expired-dispatch recovery.

All timestamps use naive UTC to match the database's existing DateTime columns.
A renewable owner token fences an old worker after another process recovers it.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from finance_adapters.persistence.audit import record_audit_event
from finance_adapters.persistence.models import ExtractionJob

DISPATCH_SECONDS = 60
LEASE_SECONDS = 300
HEARTBEAT_SECONDS = 20
logger = logging.getLogger(__name__)


def queue_extraction(job: ExtractionJob) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    job.status, job.stage, job.progress = "queued", "queued", 0
    job.owner_token = None
    job.queued_at = now
    job.lease_expires_at = now + timedelta(seconds=DISPATCH_SECONDS)
    job.error_code, job.error_detail, job.finished_at = None, None, None
    job.retryable = False


def claim_extraction(session: Session, job_id: int) -> str | None:
    token, now = uuid4().hex, datetime.now(UTC).replace(tzinfo=None)
    changed = session.execute(update(ExtractionJob).where(
        ExtractionJob.id == job_id,
        ExtractionJob.owner_token.is_(None),
        or_(ExtractionJob.status == "queued", and_(
            ExtractionJob.status == "failed", ExtractionJob.retryable.is_(True),
        )),
    ).values(
        status="running", stage="extracting", progress=5, owner_token=token,
        started_at=now, finished_at=None, error_code=None, error_detail=None,
        lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
    ), execution_options={"synchronize_session": False}).rowcount
    session.commit()
    return token if changed == 1 else None


def transition_extraction(session: Session, job_id: int, owner: str, **values) -> bool:
    """Do not commit: the final transition must be atomic with facts and audit."""
    values.setdefault("lease_expires_at", datetime.now(UTC).replace(tzinfo=None)
                      + timedelta(seconds=LEASE_SECONDS))
    changed = session.execute(update(ExtractionJob).where(
        ExtractionJob.id == job_id, ExtractionJob.status == "running",
        ExtractionJob.owner_token == owner,
    ).values(**values), execution_options={"synchronize_session": False}).rowcount
    return changed == 1


def recover_expired_extractions(session: Session, job_id: int | None = None) -> list[int]:
    now = datetime.now(UTC).replace(tzinfo=None)
    expired = or_(
        ExtractionJob.lease_expires_at <= now,
        and_(ExtractionJob.lease_expires_at.is_(None), or_(
            ExtractionJob.started_at <= now - timedelta(seconds=LEASE_SECONDS),
            and_(ExtractionJob.started_at.is_(None), or_(
                ExtractionJob.queued_at <= now - timedelta(seconds=DISPATCH_SECONDS),
                ExtractionJob.queued_at.is_(None),
            )),
        )),
    )
    query = select(ExtractionJob).where(ExtractionJob.status.in_(["queued", "running"]), expired)
    if job_id is not None:
        query = query.where(ExtractionJob.id == job_id)
    recovered = []
    for job in session.scalars(query):
        old_owner, old_status = job.owner_token, job.status
        changed = session.execute(update(ExtractionJob).where(
            ExtractionJob.id == job.id, ExtractionJob.status == old_status,
            ExtractionJob.owner_token == old_owner, expired,
        ).values(
            status="queued", stage="queued", owner_token=None, progress=0,
            queued_at=now, lease_expires_at=now + timedelta(seconds=DISPATCH_SECONDS),
            retry_count=ExtractionJob.retry_count + 1, error_code=None, error_detail=None,
        ), execution_options={"synchronize_session": False}).rowcount
        if not changed:
            continue
        record_audit_event(
            session, job.filing_version_id, category="workflow", action="extraction_recovered",
            status="queued", tool_name="extraction_lifecycle", tool_version="lease-v1",
            detail={"extraction_job_id": job.id, "previous_status": old_status,
                    "previous_owner": old_owner, "reason": "EXPIRED_DISPATCH_OR_WORKER_LEASE"},
        )
        recovered.append(job.id)
    session.commit()
    session.expire_all()
    return recovered


class ExtractionHeartbeat:
    """Renew during slow PDF/OCR calls, using an independent short transaction."""

    def __init__(self, session: Session, job_id: int, owner: str):
        self.bind, self.job_id, self.owner = session.get_bind(), job_id, owner
        self.stop = Event()
        self.thread = Thread(target=self._run, daemon=True, name=f"extract-lease-{job_id}")

    def _run(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                with Session(self.bind) as session:
                    if not transition_extraction(session, self.job_id, self.owner):
                        session.rollback()
                        return
                    session.commit()
            except Exception:
                logger.exception("Extraction lease heartbeat failed for job %s", self.job_id)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
