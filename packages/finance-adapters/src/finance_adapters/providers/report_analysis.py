"""Selected-page report analysis using the shared, controlled evidence agent."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.models import AnalysisRun, FilingVersion
from finance_adapters.persistence.report_measure_gateway import (
    VERSION as MEASURE_VERSION,
)
from finance_adapters.persistence.report_measure_gateway import (
    rebuild_measure_bundle,
    report_measure_context,
    validate_measure_bundle,
)
from finance_adapters.providers import evidence_retrieval as retrieval
from finance_adapters.providers.document_context import build_document_context
from finance_adapters.providers.model_clients import ModelClient, ModelConfig

REPORT_ANALYSIS_VERSION = "report-evidence-analysis-v1"


def _data_review(bundle: dict) -> dict:
    items = bundle["items"]
    counts = {status: sum(item.get("review_status") == status for item in items)
              for status in ("reviewed", "needs_review", "rejected")}
    return {"candidate_count": len(items), **counts,
            "complete": bool(items) and counts["reviewed"] == len(items),
            "identity_authenticated": False,
            "calculation_review_required": any(item["status"] != "reviewed"
                                               for item in bundle["calculations"])}


def build_report_package(session: Session, filing_id: int,
                         selected_document_ids: list[int] | None = None) -> dict:
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise ValueError("FILING_NOT_FOUND")
    if selected_document_ids is not None and (not selected_document_ids or
            len(set(selected_document_ids)) != len(selected_document_ids) or
            any(type(x) is not int or x <= 0 for x in selected_document_ids)):
        raise ValueError("REPORT_SELECTED_DOCUMENT_IDS_INVALID")
    ids = {filing.id, *(selected_document_ids or [])}
    documents = list(session.scalars(select(FilingVersion).where(
        FilingVersion.company_id == filing.company_id,
        FilingVersion.scope == filing.scope,
        FilingVersion.accounting_standard == filing.accounting_standard,
        FilingVersion.id.in_(ids),
    ).order_by(FilingVersion.period_end, FilingVersion.id)))
    if {x.id for x in documents} != ids:
        raise ValueError("REPORT_SELECTED_DOCUMENT_NOT_AUTHORIZED")
    contexts = [report_measure_context(session, doc) for doc in documents]
    text_contexts = [{"filing_version_id": doc.id, **build_document_context(session, doc)}
                     for doc in documents]
    descriptor_keys = ("filing_version_id", "source_sha256", "company_id", "scope", "accounting_standard",
                       "period_start", "period_end", "report_type", "period_basis", "extraction_job_id",
                       "extraction_method", "selected_page_numbers", "processing_scope")
    bundle = rebuild_measure_bundle({
        "version": MEASURE_VERSION, "documents": [{k: ctx[k] for k in descriptor_keys} for ctx in contexts],
        "items": [item for ctx in contexts for item in ctx["items"]],
        "selection_mode": "independent" if selected_document_ids is None else "explicit_joint",
        "anchor_document_id": filing.id,
    })
    anchor_context = next(context for context in text_contexts if context["filing_version_id"] == filing.id)
    package = {
        "package_version": REPORT_ANALYSIS_VERSION, "company_id": filing.company_id,
        "filing_version_id": filing.id, "period_end": filing.period_end.isoformat(),
        "analysis_scope": {"period_start": filing.period_start.isoformat(), "period_end": filing.period_end.isoformat(),
                           "scope": filing.scope, "period_basis": filing.period_basis, "currency": "CNY"},
        "selection_mode": bundle["selection_mode"], "selected_document_ids": sorted(ids),
        # Keep the historical single-document context shape for compatibility.
        # Joint runs additionally persist each selected document's bounded text
        # context so the audit record cannot silently collapse the sources to
        # the anchor filing.
        "document_context": {key: value for key, value in anchor_context.items()
                             if key != "filing_version_id"},
        "report_measure_bundle": bundle, "metrics": [], "evidence": [],
        "limitations": ["披露金额与严格年度经审计事实分开；候选计算必须显示人工审核状态。",
                        "仅分析所选原页与所选文件；不得据此认定未选页未披露或公司没有财务异常。",
                        "跨报告差分仅在显式联合时计算，未独立剔除合并范围及会计政策影响。"],
    }
    if selected_document_ids is not None:
        package["document_contexts"] = text_contexts
    return package


def create_report_analysis(session: Session, filing_id: int,
                            selected_document_ids: list[int] | None = None) -> AnalysisRun:
    # Authorize the full selection before touching its retrieval corpus.  A
    # service fallback may replace old Docling chunks with native PDF chunks;
    # do that before the new run exists, not after it becomes running.
    package = build_report_package(session, filing_id, selected_document_ids)
    for document_id in package["selected_document_ids"]:
        retrieval.materialize_document_text_chunks(session, document_id)
    package = build_report_package(session, filing_id, selected_document_ids)
    text_snapshot = retrieval.snapshot(session, package["selected_document_ids"])
    trace = {"version": REPORT_ANALYSIS_VERSION, "input_snapshot": package,
             "input_snapshot_hash": stable_hash(package), "selection_mode": package["selection_mode"],
             "retrieval_input_snapshot": text_snapshot,
             "retrieval_input_snapshot_hash": stable_hash(text_snapshot),
             "started_at": datetime.now(UTC).isoformat()}
    run = AnalysisRun(filing_version_id=filing_id, input_snapshot_hash=stable_hash(package),
                      orchestration_version=REPORT_ANALYSIS_VERSION, prompt_version=REPORT_ANALYSIS_VERSION,
                      validator_version="report-agent-validator-v1", status="queued",
                      tool_trace=trace, tool_trace_hash=stable_hash(trace))
    session.add(run)
    session.flush()
    record_audit_event(session, filing_id, category="generation", action="report_analysis_queued", status="queued",
                       tool_name="controlled_report_analysis", tool_version=REPORT_ANALYSIS_VERSION,
                       input_hash=run.input_snapshot_hash,
                       detail={"analysis_run_id": run.id, "selected_document_ids": package["selected_document_ids"],
                               "selection_mode": package["selection_mode"]})
    session.commit()
    return run


def _validate_text_snapshot(session: Session, trace: dict, document_ids: list[int]) -> None:
    expected = trace.get("retrieval_input_snapshot")
    if not isinstance(expected, dict):
        raise ValueError("REPORT_ANALYSIS_TEXT_SNAPSHOT_REQUIRED")  # noqa: TRY004 - persisted run state
    if stable_hash(expected) != trace.get("retrieval_input_snapshot_hash"):
        raise ValueError("REPORT_ANALYSIS_TEXT_SNAPSHOT_HASH_MISMATCH")
    if retrieval.snapshot(session, document_ids) != expected:
        raise ValueError("REPORT_ANALYSIS_TEXT_SNAPSHOT_CHANGED")


def run_report_analysis(session: Session, run_id: int) -> None:
    from finance_adapters.providers.analysis_agent import (
        OBLIGATION_TASK_POLICY_V1,
        run_evidence_workflow,
    )
    run = session.get(AnalysisRun, run_id)
    if run is None or run.status != "queued":
        return
    claimed = session.execute(update(AnalysisRun).where(AnalysisRun.id == run_id,
                              AnalysisRun.status == "queued").values(status="running"))
    if claimed.rowcount != 1:
        session.rollback()
        return
    session.commit()
    trace = deepcopy(run.tool_trace or {})
    try:
        if stable_hash(trace) != run.tool_trace_hash:
            raise ValueError("REPORT_ANALYSIS_TRACE_HASH_MISMATCH")
        package = trace["input_snapshot"]
        if stable_hash(package) != run.input_snapshot_hash:
            raise ValueError("REPORT_ANALYSIS_INPUT_HASH_MISMATCH")
        explicit = package["selected_document_ids"] if package["selection_mode"] == "explicit_joint" else None
        if build_report_package(session, run.filing_version_id, explicit) != package:
            raise ValueError("REPORT_ANALYSIS_INPUT_CHANGED")
        _validate_text_snapshot(session, trace, package["selected_document_ids"])
        validate_measure_bundle(package["report_measure_bundle"])
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError("LLM_CONFIGURATION_REQUIRED")
        client = ModelClient(ModelConfig(provider=settings.llm_provider, base_url=settings.llm_base_url,
            api_key=settings.llm_api_key, model=settings.llm_model, timeout=settings.llm_timeout_seconds,
            enable_thinking=settings.llm_enable_thinking, reasoning_effort=settings.llm_reasoning_effort,
            stream=getattr(settings, "llm_stream", False),
            provider_sort=getattr(settings, "llm_provider_sort", None)))
        run.model = settings.llm_model
        run.model_version = settings.llm_model

        def checkpoint():
            run.tool_trace = deepcopy(trace)
            run.tool_trace_hash = stable_hash(trace)
            session.commit()
            # The workflow calls this after materialization and bootstrap,
            # before its first model request, and again after each round.
            # Preserve any invalidation/audit records before stopping; never
            # clear invalidated_at to publish a result against new sources.
            session.refresh(run)
            if run.invalidated_at is not None:
                raise ValueError("REPORT_ANALYSIS_SOURCE_REVIEW_CHANGED")
            _validate_text_snapshot(session, trace, package["selected_document_ids"])

        filing = session.get(FilingVersion, run.filing_version_id)
        workflow = run_evidence_workflow(session, filing, package, client, trace,
            save_checkpoint=checkpoint, coverage_policy=OBLIGATION_TASK_POLICY_V1)
        if workflow is None:
            raise ValueError("REPORT_ANALYSIS_SEARCHABLE_TEXT_REQUIRED")
        session.refresh(run)
        if run.invalidated_at is not None:
            raise ValueError("REPORT_ANALYSIS_SOURCE_REVIEW_CHANGED")
        _validate_text_snapshot(session, trace, package["selected_document_ids"])
        if build_report_package(session, run.filing_version_id, explicit) != package:
            raise ValueError("REPORT_ANALYSIS_INPUT_CHANGED_DURING_RUN")
        trace["agent_workflow"] = workflow
        data_review = _data_review(package["report_measure_bundle"])
        run.output = {"version": REPORT_ANALYSIS_VERSION, "analysis_scope": package["analysis_scope"],
                      "selection_mode": package["selection_mode"], "selected_document_ids": package["selected_document_ids"],
                      "report_measure_bundle": package["report_measure_bundle"], "agent_workflow": workflow,
                      "data_review": data_review,
                      "limitations": package["limitations"], "overall_review_required": True}
        run.status = workflow["status"]
        if run.status == "completed" and not data_review["complete"]:
            run.status = "partial_result"
        run.failure_reason = None
        trace["output_hash"] = stable_hash(run.output)
        record_audit_event(session, filing.id, category="generation", action="report_analysis_completed",
                           status=run.status, tool_name="controlled_report_analysis", tool_version=REPORT_ANALYSIS_VERSION,
                           input_hash=run.input_snapshot_hash, output_hash=stable_hash(run.output),
                           detail={"analysis_run_id": run.id, "selection_mode": package["selection_mode"],
                                   "selected_document_ids": package["selected_document_ids"], "stop_reason": workflow["stop_reason"]})
    except Exception as exc:  # noqa: BLE001 - persist background failure without losing the trace
        session.rollback()
        run = session.get(AnalysisRun, run_id)
        run.status, run.failure_reason = "failed", str(exc)
        trace["error"] = str(exc)
    trace["completed_at"] = datetime.now(UTC).isoformat()
    run.tool_trace, run.tool_trace_hash = deepcopy(trace), stable_hash(trace)
    session.commit()


def replay_report_analysis(run: AnalysisRun) -> dict:
    """Recompute arithmetic and revalidate saved raw model actions offline."""
    from finance_adapters.providers.analysis_agent import replay_evidence_workflow
    trace = run.tool_trace or {}
    if run.orchestration_version != REPORT_ANALYSIS_VERSION or stable_hash(trace) != run.tool_trace_hash:
        raise ValueError("REPORT_ANALYSIS_TRACE_HASH_MISMATCH")
    package = trace.get("input_snapshot")
    if package is None or stable_hash(package) != run.input_snapshot_hash:
        raise ValueError("REPORT_ANALYSIS_INPUT_HASH_MISMATCH")
    validate_measure_bundle(package["report_measure_bundle"])
    if run.output is None or stable_hash(run.output) != trace.get("output_hash"):
        raise ValueError("REPORT_ANALYSIS_OUTPUT_HASH_MISMATCH")
    expected = {key: package[key] for key in ("analysis_scope", "selection_mode", "selected_document_ids",
                                             "report_measure_bundle", "limitations")}
    expected.update(version=REPORT_ANALYSIS_VERSION, agent_workflow=trace.get("agent_workflow"),
                    data_review=_data_review(package["report_measure_bundle"]), overall_review_required=True)
    if run.output != expected:
        raise ValueError("REPORT_ANALYSIS_REPLAY_OUTPUT_MISMATCH")
    if run.invalidated_at is not None or run.status == "invalidated":
        raise ValueError("REPORT_ANALYSIS_SOURCE_INVALIDATED")
    expected_status = expected["agent_workflow"]["status"]
    if expected_status == "completed" and not expected["data_review"]["complete"]:
        expected_status = "partial_result"
    if run.status != expected_status:
        raise ValueError("REPORT_ANALYSIS_REPLAY_STATUS_MISMATCH")
    replayed = replay_evidence_workflow(trace, package)
    if replayed.get("status") != "passed":
        raise ValueError("REPORT_ANALYSIS_REPLAY_WORKFLOW_UNAVAILABLE")
    return {"status": "passed", "model_called": False, "database_used": False,
            "new_model_conclusion_reproducibility_tested": False,
            "input_snapshot_hash": run.input_snapshot_hash, "tool_trace_hash": run.tool_trace_hash,
            "output_hash": stable_hash(run.output), "selected_document_ids": package["selected_document_ids"],
            "agent_replay": replayed,
            "calculation_count": len(package["report_measure_bundle"]["calculations"])}
