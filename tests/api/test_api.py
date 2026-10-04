from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import fitz
from fastapi.testclient import TestClient
from finance_adapters.config import settings
from finance_adapters.parsing import pdf_parser as parsing
from finance_adapters.persistence.database import SessionLocal
from finance_adapters.persistence.metric_gateway import calculate_metrics
from finance_adapters.persistence.models import (
    Company,
    EvidenceFragment,
    FactReview,
    FileScan,
    FilingVersion,
    FinancialFact,
)
from finance_adapters.persistence.review_gateway import recompute_filing_quality
from finreport_api.main import app
from sqlalchemy import select


def test_upload_and_extracts_candidates(monkeypatch):
    monkeypatch.setattr(
        parsing,
        "CONCEPTS",
        {
            "Revenue": ("revenue", "income_statement", "flow"),
            "Operating Cost": ("operating_cost", "income_statement", "flow"),
        },
    )
    source = Path(__file__).parent / ".runtime" / f"sample-{uuid4()}.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        f"Income Statement\nRevenue 1000\nOperating Cost 600\n{uuid4()}",
    )
    document.save(source)
    document.close()
    with TestClient(app) as client:
        with source.open("rb") as handle:
            response = client.post(
                "/api/v1/filings",
                files={"file": ("sample.pdf", handle, "application/pdf")},
                data={
                    "stock_code": uuid4().hex[:8],
                    "company_name": "Test Company",
                    "period_start": "2025-01-01",
                    "period_end": "2025-12-31",
                    "report_type": "annual",
                    "period_basis": "annual",
                },
            )
        assert response.status_code == 202
        filing_id = response.json()["data"]["filing_version_id"]
        job = client.post(f"/api/v1/filings/{filing_id}/jobs")
        assert job.status_code == 202
        status = client.get(f"/api/v1/jobs/{job.json()['data']['job_id']}").json()["data"]
        assert status["status"] == "completed"
        facts = client.get(f"/api/v1/filings/{filing_id}/facts").json()["data"]
        assert facts
        assert all(item["quality_status"] == "candidate" for item in facts)
        trail = client.get(f"/api/v1/filings/{filing_id}/audit-trail")
        assert trail.status_code == 200
        audit = trail.json()["data"]
        assert audit["reproduction"]["file_sha256"]
        actions = [item["action"] for item in audit["events"]]
        assert "file_ingested" in actions
        assert "pdf_extraction_started" in actions
        assert "pdf_extraction_completed" in actions


def test_upload_fails_closed_and_audits_when_supervisor_is_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "av_mode", "supervisor")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/filings",
            files={"file": ("sample.pdf", b"%PDF-1.7\nblocked", "application/pdf")},
            data={
                "stock_code": uuid4().hex[:8],
                "company_name": "Blocked Company",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "report_type": "annual",
                "period_basis": "annual",
            },
        )
    assert response.status_code == 503
    with SessionLocal() as session:
        audit = session.scalars(select(FileScan).order_by(FileScan.id.desc())).first()
        assert audit is not None
        assert audit.disposition == "unavailable"
        assert audit.error_code == "SCANNER_CONTROL_PIPE_UNAVAILABLE"


def test_two_reviews_and_balance_are_required_for_strict_metrics():
    filing_id = 0
    with TestClient(app), SessionLocal() as session:
        company = Company(stock_code=uuid4().hex[:8], name="Review Company")
        session.add(company)
        session.flush()
        filing = FilingVersion(
            company_id=company.id,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            report_type="annual",
            period_basis="annual",
            file_sha256=uuid4().hex * 2,
            object_key="not-used.pdf",
        )
        session.add(filing)
        session.flush()
        for code, value in [
            ("total_assets", 100),
            ("total_liabilities", 60),
            ("total_equity", 40),
            ("revenue", 1000),
            ("operating_cost", 600),
            ("net_income_attributable", 100),
            ("net_income", 100),
            ("cashflow_operations", 120),
            ("capex_cash", 50),
        ]:
            fact = FinancialFact(
                filing_version_id=filing.id,
                concept_code=code,
                concept_name=code,
                statement_type="资产负债表" if code.startswith("total_") else "利润表",
                period_start=filing.period_start,
                period_end=filing.period_end,
                value=value,
                flow_or_stock="stock" if code.startswith("total_") else "flow",
                raw_value=str(value),
            )
            session.add(fact)
            session.flush()
            session.add(
                EvidenceFragment(
                    fact_id=fact.id,
                    page_no=1,
                    table_name="测试",
                    row_locator=code,
                    column_locator="本期",
                    raw_label=code,
                    raw_value=str(value),
                    bounding_box={},
                    page_sha256="a" * 64,
                    confidence=1,
                )
            )
            session.add(
                FactReview(
                    fact_id=fact.id,
                    reviewer_id="reviewer-a",
                    decision="confirmed",
                    reason="核对原文",
                )
            )
            session.add(
                FactReview(
                    fact_id=fact.id,
                    reviewer_id="reviewer-b",
                    decision="confirmed",
                    reason="独立复核",
                )
            )
        session.commit()
        filing_id = filing.id
        assert recompute_filing_quality(session, filing.id) == "facts_verified"
        results = calculate_metrics(session, filing.id)
        margin = next(item for item in results if item.metric_code == "gross_margin")
        assert margin.value == Decimal("0.4")
        assert margin.applicability_status == "calculated"
    with TestClient(app) as client:
        response = client.post(f"/api/v1/filings/{filing_id}/metrics", json={"strict": True})
        assert response.status_code == 200
        trail = client.get(f"/api/v1/filings/{filing_id}/audit-trail").json()["data"]
    calculation = next(item for item in trail["events"] if item["action"] == "strict_metrics_calculated")
    assert calculation["input_hash"]
    assert calculation["output_hash"]
    assert calculation["detail"]["mode"] == "strict_audited"
