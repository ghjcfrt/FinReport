from __future__ import annotations

import os

from celery import Celery
from finance_adapters.parsing.pdf_parser import run_extraction
from finance_adapters.persistence.database import SessionLocal
from finance_adapters.providers.llm_analysis import run_analysis

broker_url = os.getenv("FINREPORT_REDIS_URL", "redis://127.0.0.1:6379/0")
celery_app = Celery("finreport", broker=broker_url, backend=broker_url)


@celery_app.task(name="finreport.extract")
def extract(job_id: int) -> None:
    with SessionLocal() as session:
        run_extraction(session, job_id)


@celery_app.task(name="finreport.analyze")
def analyze(run_id: int) -> None:
    with SessionLocal() as session:
        run_analysis(session, run_id)
