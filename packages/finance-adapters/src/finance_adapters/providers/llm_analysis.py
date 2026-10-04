from __future__ import annotations

import hashlib
import json

from finance_domain.analysis.contracts import AnalysisOutput
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.persistence.audit import record_audit_event
from finance_adapters.persistence.models import (
    AnalysisClaim,
    AnalysisRun,
    EvidenceFragment,
    FilingVersion,
    FinancialFact,
    MetricResult,
)
from finance_adapters.providers.model_clients import ModelClient, ModelConfig, json_text

SYSTEM_PROMPT = """You are a financial-report evidence analyst. Return only a JSON object matching the supplied schema.
Use only the supplied evidence. Do not give investment advice, forecasts, ratings, fraud allegations,
or causal claims beyond the evidence. Every inference needs an included metric and evidence reference."""
FORBIDDEN = ("买入", "卖出", "看好", "看空", "投资建议", "评级", "欺诈", "持续经营")


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def build_evidence_package(session: Session, filing_id: int) -> dict:
    filing = session.get(FilingVersion, filing_id)
    metrics = list(
        session.scalars(
            select(MetricResult).where(
                MetricResult.filing_version_id == filing_id,
                MetricResult.applicability_status == "calculated",
                MetricResult.computation_mode == "strict_audited",
            )
        )
    )
    evidence = list(
        session.scalars(
            select(EvidenceFragment)
            .join(FinancialFact)
            .where(
                FinancialFact.filing_version_id == filing_id,
                FinancialFact.quality_status == "facts_verified",
            )
        )
    )
    return {
        "company_id": filing.company_id,
        "filing_version_id": filing.id,
        "period_end": filing.period_end.isoformat(),
        "limitations": ["仅基于经严格核验的输入。", "仅供研究与教学，不构成投资建议。"],
        "metrics": [
            {"id": item.id, "code": item.metric_code, "value": str(item.value), "unit": item.unit}
            for item in metrics
        ],
        "evidence": [
            {
                "id": item.id,
                "page_no": item.page_no,
                "label": item.raw_label,
                "value": item.raw_value,
                "table": item.table_name,
            }
            for item in evidence
        ],
    }


def _validate_claims(output: AnalysisOutput, package: dict) -> None:
    metric_ids = {entry["id"] for entry in package["metrics"]}
    evidence_ids = {entry["id"] for entry in package["evidence"]}
    for claim in output.claims:
        if any(word in claim.content for word in FORBIDDEN):
            raise ValueError("FORBIDDEN_CLAIM")
        if not set(claim.metric_result_ids).issubset(metric_ids) or not set(
            claim.evidence_fragment_ids
        ).issubset(evidence_ids):
            raise ValueError("UNAUTHORIZED_EVIDENCE_REFERENCE")
        if claim.claim_class == "evidence_supported_inference" and (
            not claim.metric_result_ids or not claim.evidence_fragment_ids
        ):
            raise ValueError("INFERENCE_REQUIRES_METRIC_AND_EVIDENCE")
        if claim.claim_class == "review_required_hypothesis" and not claim.review_required:
            raise ValueError("HYPOTHESIS_REQUIRES_REVIEW")
    if not output.limitations:
        raise ValueError("LIMITATIONS_REQUIRED")


def run_analysis(session: Session, run_id: int) -> None:
    run = session.get(AnalysisRun, run_id)
    if run is None:
        return
    try:
        if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
            raise ValueError("REGISTERED_LLM_NOT_CONFIGURED")
        package = build_evidence_package(session, run.filing_version_id)
        if not package["metrics"] or not package["evidence"]:
            raise ValueError("STRICT_EVIDENCE_REQUIRED")
        if _hash(package) != run.input_snapshot_hash:
            raise ValueError("INPUT_SNAPSHOT_CHANGED")
        client = ModelClient(ModelConfig(settings.llm_provider, settings.llm_base_url, settings.llm_api_key, settings.llm_model))
        content, data = client.complete(
            system=SYSTEM_PROMPT,
            user=json_text({"evidence_package": package, "schema": AnalysisOutput.model_json_schema()}),
            response_json=True,
        )
        output = AnalysisOutput.model_validate_json(content)
        _validate_claims(output, package)
        trace = {
            "provider_response_id": data.get("id"),
            "model": data.get("model", settings.llm_model),
            "input_hash": run.input_snapshot_hash,
            "output_hash": _hash(output.model_dump()),
            "tools": [{"name": "build_evidence_package", "result_hash": _hash(package)}],
        }
        run.status, run.output, run.tool_trace, run.tool_trace_hash, run.model, run.model_version = (
            "completed",
            output.model_dump(mode="json"),
            trace,
            _hash(trace),
            settings.llm_model,
            data.get("model", settings.llm_model),
        )
        record_audit_event(
            session,
            run.filing_version_id,
            category="generation",
            action="analysis_generated",
            status="completed",
            tool_name="model_completion",
            tool_version=data.get("model", settings.llm_model),
            input_hash=run.input_snapshot_hash,
            output_hash=trace["output_hash"],
            detail={
                "analysis_run_id": run.id,
                "provider_response_id": data.get("id"),
                "tool_trace_hash": run.tool_trace_hash,
                "validator_version": run.validator_version,
                "orchestration_version": run.orchestration_version,
                "claim_count": len(output.claims),
            },
        )
        for claim in output.claims:
            session.add(
                AnalysisClaim(
                    analysis_run_id=run.id,
                    claim_type=claim.type,
                    claim_class=claim.claim_class,
                    content=claim.content,
                    metric_result_ids=claim.metric_result_ids,
                    evidence_fragment_ids=claim.evidence_fragment_ids,
                    evidence_relation=claim.evidence_relation,
                    validation_status="passed",
                    review_status="required" if claim.review_required else "not_required",
                )
            )
    except Exception as exc:  # noqa: BLE001 - provider and contract failures are persisted as an audit record.
        run.status, run.failure_reason = "failed", str(exc)
        record_audit_event(
            session,
            run.filing_version_id,
            category="generation",
            action="analysis_generated",
            status="failed",
            tool_name="model_completion",
            tool_version=settings.llm_model or None,
            input_hash=run.input_snapshot_hash,
            detail={"analysis_run_id": run.id, "error": str(exc)},
        )
    session.commit()
