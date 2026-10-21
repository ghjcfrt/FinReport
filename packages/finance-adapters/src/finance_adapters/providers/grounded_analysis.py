"""Persisted, single-batch experimental generation from one selected filing.

This is a candidate pipeline, not a second financial-conclusion admission gate.
Preparation runs in the background; no model call occurs in the creation path.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import update
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.persistence.audit import record_audit_event, stable_hash
from finance_adapters.persistence.models import AnalysisRun, FilingVersion
from finance_adapters.providers import evidence_retrieval as retrieval
from finance_adapters.providers.analysis_agent import EVIDENCE_OBLIGATIONS
from finance_adapters.providers.disclosed_relations import extract_disclosed_relations
from finance_adapters.providers.disclosure_facets import extract_disclosure_facets
from finance_adapters.providers.evidence_context import (
    build_evidence_context,
    validate_evidence_context,
)
from finance_adapters.providers.grounded_generation import (
    GroundedGenerationInput,
    build_grounded_generation_request,
    generate_grounded_candidates,
    validate_grounded_response,
)
from finance_adapters.providers.grounded_source_read import supplemental_local_context
from finance_adapters.providers.model_clients import ModelClient, ModelConfig
from finance_adapters.providers.task_evidence_contract import task_evidence_registry
from finance_adapters.security.file_gate import scanned_object_path

GROUNDED_ANALYSIS_VERSION = "grounded-report-candidates-v5"
GROUNDED_REQUEST_PROFILES = {
    "grounded-report-candidates-v2": None,
    "grounded-report-candidates-v3": "explicit_schema_messages_v2",
    "grounded-report-candidates-v4": "quote_anchor_messages_v3",
    "grounded-report-candidates-v5": "task_evidence_messages_v4",
}
GROUNDED_REPLAY_VERSIONS = set(GROUNDED_REQUEST_PROFILES)
TASKS = {key: label for items in EVIDENCE_OBLIGATIONS.values() for key, label in items}


class GroundedAnalysisSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_numbers: list[int] = Field(min_length=1, max_length=12)
    obligation_ids: list[str] = Field(min_length=1, max_length=10)

    @field_validator("page_numbers")
    @classmethod
    def valid_pages(cls, value):
        if any(page <= 0 for page in value) or len(set(value)) != len(value):
            raise ValueError("GROUNDED_SELECTION_PAGES_INVALID")
        return sorted(value)

    @field_validator("obligation_ids")
    @classmethod
    def valid_tasks(cls, value):
        if len(set(value)) != len(value) or not set(value) <= TASKS.keys():
            raise ValueError("GROUNDED_SELECTION_TASKS_INVALID")
        return sorted(value)


def _descriptor(filing: FilingVersion) -> dict:
    return {"filing_version_id": filing.id, "company_id": filing.company_id,
            "source_sha256": filing.file_sha256, "scope": filing.scope,
            "accounting_standard": filing.accounting_standard,
            "period_start": filing.period_start.isoformat(), "period_end": filing.period_end.isoformat(),
            "period_basis": filing.period_basis, "report_type": filing.report_type}


def _disclosure_context(sources: list[dict]) -> tuple[list[dict], list[dict]]:
    """Collapse byte-identical text from the same physical page, keeping aliases.

    Similar wording, whitespace-normalized text and overlapping excerpts are
    not proven identical; they must never be merged by this narrow rule.
    """
    canonical, groups = {}, []
    for source in sources:
        key = (source["source_sha256"], source["page_no"], source["text_sha256"], source["text"])
        if key not in canonical:
            canonical[key] = source
            groups.append({"canonical_source_id": source["id"], "source_ids": [source["id"]],
                           "document_sha256": source["source_sha256"], "page_no": source["page_no"],
                           "text_sha256": source["text_sha256"]})
        else:
            group = next(item for item in groups if item["canonical_source_id"] == canonical[key]["id"])
            group["source_ids"].append(source["id"])
    return ([extract_disclosed_relations(source).model_dump(mode="json") for source in canonical.values()],
            [group for group in groups if len(group["source_ids"]) > 1])


def prepare_grounded_input(document: dict, snapshot: dict, selection: dict, run_id: str,
                           *, supplemental: dict | None = None,
                           protocol_version: str = GROUNDED_ANALYSIS_VERSION) -> dict:
    """Build bounded context only from a frozen, single-document corpus."""
    if protocol_version not in GROUNDED_REPLAY_VERSIONS:
        raise ValueError("GROUNDED_PROTOCOL_VERSION_UNKNOWN")
    selected = GroundedAnalysisSelection.model_validate(selection)
    documents = snapshot.get("documents", [])
    if (len(documents) != 1 or documents[0]["filing_version_id"] != document["filing_version_id"]
            or documents[0]["source_sha256"] != document["source_sha256"]
            or documents[0].get("document_sha256", document["source_sha256"]) != document["source_sha256"]):
        raise ValueError("GROUNDED_DOCUMENT_SNAPSHOT_MISMATCH")
    if any(documents[0].get(key) != document.get(key) for key in
           ("period_start", "period_end", "period_basis", "scope", "report_type")):
        raise ValueError("GROUNDED_DOCUMENT_SNAPSHOT_MISMATCH")
    expected = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    if stable_hash(expected) != snapshot.get("snapshot_hash"):
        raise ValueError("GROUNDED_SNAPSHOT_HASH_MISMATCH")
    sources = [{**chunk, "id": chunk["source_id"]} for chunk in documents[0]["chunks"]]
    # Validate every chunk before context selection or projecting model fields.
    # The wrapper hash cannot authorize rebinding a foreign/unknown chunk to
    # the selected document, even when that chunk is outside the center pages.
    if any(source.get("filing_version_id") != document["filing_version_id"]
           or source.get("source_sha256") != document["source_sha256"] for source in sources):
        raise ValueError("GROUNDED_RETRIEVAL_SOURCE_MISMATCH")
    if supplemental is not None:
        content = {key: value for key, value in supplemental.items() if key != "snapshot_hash"}
        if (stable_hash(content) != supplemental.get("snapshot_hash")
                or supplemental.get("base_snapshot_hash") != snapshot["snapshot_hash"]
                or supplemental.get("filing_version_id") != document["filing_version_id"]
                or supplemental.get("source_sha256") != document["source_sha256"]
                or supplemental.get("document_sha256", document["source_sha256"]) != document["source_sha256"]
                or supplemental.get("requested_pages") != selected.page_numbers):
            raise ValueError("GROUNDED_SUPPLEMENTAL_SNAPSHOT_MISMATCH")
        planned = {page + delta for page in selected.page_numbers for delta in (-1, 0, 1) if page + delta > 0}
        if supplemental.get("adjacent_pages") != 1 or supplemental.get("planned_page_numbers") != sorted(planned):
            raise ValueError("GROUNDED_SUPPLEMENTAL_SCOPE_MISMATCH")
        existing_ids = {source["id"] for source in sources}
        for source in supplemental["sources"]:
            if (source["id"] in existing_ids or source["filing_version_id"] != document["filing_version_id"]
                    or source["source_sha256"] != document["source_sha256"] or source["page_no"] not in planned):
                raise ValueError("GROUNDED_SUPPLEMENTAL_SOURCE_MISMATCH")
            existing_ids.add(source["id"])
        sources.extend(deepcopy(supplemental["sources"]))
    available_pages = {source["page_no"] for source in sources}
    if not set(selected.page_numbers) <= available_pages:
        # A page not present locally is a preparation gap, not a company-level
        # statement and not a reason to charge for a knowingly empty batch.
        raise ValueError("GROUNDED_SELECTED_PAGE_NOT_MATERIALIZED")
    centers = [source["id"] for source in sources if source["page_no"] in selected.page_numbers]
    context = build_evidence_context(sources, centers, documents=[document])
    if context["document_scope"] != {"filing_version_id": document["filing_version_id"],
                                     "source_sha256": document["source_sha256"],
                                     "document_sha256": document["source_sha256"]}:
        raise ValueError("GROUNDED_CONTEXT_DOCUMENT_MISMATCH")
    if not set(centers) <= set(context["generation_source_ids"]):
        raise ValueError("GROUNDED_CENTER_EXCEEDS_CONTEXT_BUDGET")
    # These are program-reconstructed, verbatim company disclosures, not model
    # explanations or verified business causes. Retain each full parent locally;
    # the model already receives it through sources, so do not send it twice.
    if protocol_version != "grounded-report-candidates-v2":
        disclosures, disclosure_aliases = _disclosure_context(context["sources"])
    else:
        disclosures = [extract_disclosed_relations(source).model_dump(mode="json")
                       for source in context["sources"]]
        disclosure_aliases = []
    disclosed_relations = [relation for result in disclosures for relation in result["relations"]]
    task_context = {}
    if protocol_version == "grounded-report-candidates-v5":
        # Each visible source has a complete, independently reconstructible
        # registry, including byte-identical aliases. These are source-bound
        # observations, never additive amounts or unique financial facts.
        facets = [extract_disclosure_facets(source).model_dump(mode="json")
                  for source in context["sources"]]
        task_context = {
            "task_evidence_registry": task_evidence_registry(selected.obligation_ids),
            "program_disclosure_facets": [item for result in facets for item in result["observations"]],
            "program_facet_unknowns": [{"source_id": result["parent_source"]["id"], **item}
                                      for result in facets for item in result["unknowns"]],
            "source_pages": {source["id"]: source["page_no"] for source in context["sources"]},
        }
    # The same page may contain current/comparative periods, stock dates and
    # cumulative/single-quarter columns. Filing dates are background only.
    period = {"start": None, "end": None, "basis": "unspecified"}
    scope = {"issuer_id": str(document["company_id"]),
             "consolidation": "unspecified",
             "accounting_standard": document["accounting_standard"],
             # A filing's metadata alone cannot settle units/currency of every
             # table, comparative column, or nonmonetary disclosure.
             "currency": "unspecified", "amount_unit": "unspecified"}
    inputs = GroundedGenerationInput.model_validate({
        "schema_version": "grounded_generation_input_v1", "run_id": run_id,
        "tasks": [{"obligation_id": key, "question": TASKS[key]} for key in selected.obligation_ids],
        "sources": [{"id": source["id"], "filing_version_id": document["filing_version_id"],
                     "document_sha256": document["source_sha256"], "text": source["text"],
                     "text_sha256": source["text_sha256"], "period": period, "scope": scope}
                    for source in context["sources"]],
        "calculations": [],
        "context_manifest": {
            **{key: context[key] for key in ("package_hash", "closure", "associations",
                                            "context_states", "next_local_read_ids")},
            "document_background": document,
            "program_disclosed_relations": disclosed_relations,
            **({"disclosed_source_aliases": disclosure_aliases,
                "generation_request_profile": GROUNDED_REQUEST_PROFILES[protocol_version]}
               if protocol_version != "grounded-report-candidates-v2" else {}),
            "disclosure_boundary": "逐字的公司披露归因，不是独立确认的业务因果，也不进入严格金额计算。",
            "boundary": "文件期间与合并口径仅为背景；原文列/行期间、母公司子表、单位及币种尚未逐项认证。",
            "supplemental_gaps": supplemental.get("gaps", []) if supplemental is not None else [],
            **task_context,
        },
    }).model_dump(mode="json")
    return {"document": document, "retrieval_snapshot": snapshot,
            "selection": selected.model_dump(mode="json"), "context": context, "inputs": inputs,
            "supplemental_snapshot": deepcopy(supplemental), "disclosed_extractions": disclosures,
            **({"disclosed_source_aliases": disclosure_aliases}
               if protocol_version != "grounded-report-candidates-v2" else {})}


def create_grounded_analysis(session: Session, filing_id: int, selection: dict) -> AnalysisRun:
    selected = GroundedAnalysisSelection.model_validate(selection)
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise ValueError("FILING_NOT_FOUND")
    # Background preparation will verify the shared scanned-object gate before
    # reading any PDF. Creation just authorizes and records the document ID.
    request = {"document": _descriptor(filing), "selection": selected.model_dump(mode="json")}
    trace = {"version": GROUNDED_ANALYSIS_VERSION, "request": request,
             "request_hash": stable_hash(request), "created_at": datetime.now(UTC).isoformat()}
    run = AnalysisRun(filing_version_id=filing_id, status="preparing",
                      orchestration_version=GROUNDED_ANALYSIS_VERSION, prompt_version="grounded_generation_v1",
                      validator_version="grounded_generation_validation_v1", input_snapshot_hash=stable_hash(request),
                      tool_trace=trace, tool_trace_hash=stable_hash(trace))
    session.add(run)
    session.flush()
    record_audit_event(session, filing_id, category="generation", action="grounded_candidates_preparing",
                       status="preparing", tool_name="grounded_report_candidates",
                       tool_version=GROUNDED_ANALYSIS_VERSION, input_hash=stable_hash(request),
                       detail={"analysis_run_id": run.id, "model_called": False})
    session.commit()
    return run


def _checked_state(session: Session, run: AnalysisRun, frozen: dict) -> None:
    session.refresh(run)
    filing = session.get(FilingVersion, run.filing_version_id)
    session.refresh(filing)
    if run.invalidated_at is not None or run.status == "invalidated":
        raise ValueError("GROUNDED_SOURCE_INVALIDATED")
    if _descriptor(filing) != frozen["document"]:
        raise ValueError("GROUNDED_DOCUMENT_CHANGED")
    scanned_object_path(session, filing)
    if retrieval.snapshot(session, [filing.id]) != frozen["retrieval_snapshot"]:
        raise ValueError("GROUNDED_TEXT_SNAPSHOT_CHANGED")
    if frozen.get("supplemental_snapshot") is not None:
        current = supplemental_local_context(session, filing, frozen["retrieval_snapshot"],
                                             frozen["selection"]["page_numbers"])
        if current != frozen["supplemental_snapshot"]:
            raise ValueError("GROUNDED_SUPPLEMENTAL_CONTEXT_CHANGED")


def _candidate_output(frozen: dict, batch: dict, *, version: str = GROUNDED_ANALYSIS_VERSION) -> dict:
    represented = sorted({item["candidate"]["obligation_id"] for item in batch["candidates"]
                          if item["candidate"] is not None and item["validation"]["provenance_valid"]})
    output = {"version": version, "mode": "experimental_candidates",
            "context_hash": frozen["context"]["package_hash"], "generation": batch,
            "company_disclosed_observations": [relation for result in frozen["disclosed_extractions"]
                                                for relation in result["relations"]],
            **({"disclosed_source_aliases": frozen["disclosed_source_aliases"]}
               if version != "grounded-report-candidates-v2" else {}),
            "disclosure_unknowns": [{"source_id": result["parent_source"]["id"], **item}
                                    for result in frozen["disclosed_extractions"] for item in result["unknowns"]],
            "unrepresented_obligation_ids": sorted(set(frozen["selection"]["obligation_ids"]) - set(represented)),
            "formal_findings": [], "formal_admitted_count": 0, "financial_analysis_complete": False,
            "limitations": ["结构化主张仅作为候选；来源有效不能证明解释语义正确。",
                            "上下文关联不证明单位、期间或因果闭合；不把本地缺页解释为公司未披露。",
                            "本入口一次批量调用，不自动重试、不进入严格金额计算或正式结论。"]}
    if version == "grounded-report-candidates-v5":
        output["program_disclosure_facets"] = frozen["inputs"]["context_manifest"]["program_disclosure_facets"]
        output["bounded_task_displays"] = [
            {"claim_id": item["raw_candidate"]["claim_id"],
             "obligation_id": item["raw_candidate"]["obligation_id"],
             "text": item["task_evidence_validation"]["safe_display"],
             "semantic_support_verified": False, "formal_admitted": False}
            for item in batch["candidates"]
            if item.get("task_evidence_validation", {}).get("display_eligible")
            and item["validation"]["provenance_valid"]
        ]
        output["task_completion_assessed"] = False
        displayed = {item["obligation_id"] for item in output["bounded_task_displays"]}
        output["task_contract_rejected_or_missing_obligation_ids"] = sorted(
            set(frozen["selection"]["obligation_ids"]) - displayed)
        output["unrepresented_obligation_ids_basis"] = (
            "仅按候选原文来源校验统计；不表示任务合同通过、模型充分使用来源或财务回答完整。"
        )
    return output


def run_grounded_analysis(session: Session, run_id: int) -> None:
    run = session.get(AnalysisRun, run_id)
    if run is None or run.orchestration_version not in GROUNDED_REPLAY_VERSIONS or run.status != "preparing":
        return
    claimed = session.execute(update(AnalysisRun).where(AnalysisRun.id == run_id,
        AnalysisRun.status == "preparing").values(status="preparing_active"))
    if claimed.rowcount != 1:
        session.rollback()
        return
    session.commit()
    trace = deepcopy(run.tool_trace or {})
    try:
        if stable_hash(trace) != run.tool_trace_hash or stable_hash(trace["request"]) != trace["request_hash"]:
            raise ValueError("GROUNDED_TRACE_HASH_MISMATCH")
        request = trace["request"]
        if run.input_snapshot_hash != stable_hash(request):
            raise ValueError("GROUNDED_REQUEST_HASH_MISMATCH")
        filing = session.get(FilingVersion, run.filing_version_id)
        if _descriptor(filing) != request["document"]:
            raise ValueError("GROUNDED_DOCUMENT_CHANGED")
        scanned_object_path(session, filing)
        trace["preparation"] = retrieval.materialize_document_text_chunks(session, filing.id)
        snapshot = retrieval.snapshot(session, [filing.id])
        supplemental = supplemental_local_context(session, filing, snapshot, request["selection"]["page_numbers"])
        frozen = prepare_grounded_input(request["document"], snapshot, request["selection"], str(run.id),
                                         supplemental=supplemental, protocol_version=run.orchestration_version)
        trace.update(input_snapshot=frozen, input_snapshot_hash=stable_hash(frozen))
        run.input_snapshot_hash = stable_hash(frozen)
        run.status = "running"
        run.tool_trace, run.tool_trace_hash = deepcopy(trace), stable_hash(trace)
        session.commit()
        _checked_state(session, run, frozen)
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError("LLM_CONFIGURATION_REQUIRED")
        client = ModelClient(ModelConfig(provider=settings.llm_provider, base_url=settings.llm_base_url,
            api_key=settings.llm_api_key, model=settings.llm_model, timeout=settings.llm_timeout_seconds,
            enable_thinking=settings.llm_enable_thinking, reasoning_effort=settings.llm_reasoning_effort,
            stream=getattr(settings, "llm_stream", False),
            provider_sort=getattr(settings, "llm_provider_sort", None)))
        run.model = run.model_version = settings.llm_model
        # Persist intention before calling. An interrupted running record is
        # never automatically replayed against the provider (duplicate charge).
        trace["provider_attempt_started"] = True
        run.tool_trace, run.tool_trace_hash = deepcopy(trace), stable_hash(trace)
        session.commit()
        trace["generation"] = generate_grounded_candidates(client, frozen["inputs"]).as_dict()
        # Save the paid response before checking whether evidence changed while
        # the provider was running; keep it for attribution even if unpublished.
        run.tool_trace, run.tool_trace_hash = deepcopy(trace), stable_hash(trace)
        session.commit()
        _checked_state(session, run, frozen)
        run.output = _candidate_output(frozen, trace["generation"], version=run.orchestration_version)
        # Formal exports select completed/partial_result/waiting_for_documents;
        # experimental candidates must not shadow a usable financial analysis.
        run.status = "failed" if trace["generation"]["status"] != "generated" else "candidate_ready"
        run.failure_reason = ",".join(trace["generation"]["errors"]) or None
        trace["output_hash"] = stable_hash(run.output)
        record_audit_event(session, filing.id, category="generation", action="grounded_candidates_generated",
            status=run.status, tool_name="grounded_report_candidates", tool_version=run.orchestration_version,
            input_hash=run.input_snapshot_hash, output_hash=stable_hash(run.output),
            detail={"analysis_run_id": run.id, "formal_admitted_count": 0, "model_calls": 1})
    except Exception as exc:  # noqa: BLE001 - persist failure and any paid response
        session.rollback()
        run = session.get(AnalysisRun, run_id)
        run.status = "invalidated" if run.invalidated_at is not None else "failed"
        run.failure_reason = str(exc)
        trace["error"] = str(exc)
    trace["completed_at"] = datetime.now(UTC).isoformat()
    run.tool_trace, run.tool_trace_hash = deepcopy(trace), stable_hash(trace)
    session.commit()


def replay_grounded_analysis(run: AnalysisRun) -> dict:
    """Rebuild context, exact request and every candidate receipt offline."""
    try:
        return _replay_grounded_analysis(run)
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError("GROUNDED_REPLAY_TRACE_INVALID") from exc


def _replay_grounded_analysis(run: AnalysisRun) -> dict:
    trace = run.tool_trace or {}
    if (run.orchestration_version not in GROUNDED_REPLAY_VERSIONS or stable_hash(trace) != run.tool_trace_hash
            or trace.get("version") != run.orchestration_version):
        raise ValueError("GROUNDED_TRACE_HASH_MISMATCH")
    if run.invalidated_at is not None or run.status == "invalidated":
        raise ValueError("GROUNDED_SOURCE_INVALIDATED")
    if trace.get("provider_attempt_started") is not True:
        raise ValueError("GROUNDED_PROVIDER_ATTEMPT_MISSING")
    frozen = trace.get("input_snapshot")
    if not isinstance(frozen, dict) or stable_hash(frozen) != run.input_snapshot_hash:
        raise ValueError("GROUNDED_INPUT_HASH_MISMATCH")
    if trace.get("input_snapshot_hash") != run.input_snapshot_hash:
        raise ValueError("GROUNDED_INPUT_HASH_MISMATCH")
    if run.filing_version_id != frozen["document"]["filing_version_id"]:
        raise ValueError("GROUNDED_DOCUMENT_ID_MISMATCH")
    if (stable_hash(trace["request"]) != trace.get("request_hash")
            or trace["request"] != {key: frozen[key] for key in ("document", "selection")}):
        raise ValueError("GROUNDED_REQUEST_HASH_MISMATCH")
    rebuilt = prepare_grounded_input(frozen["document"], frozen["retrieval_snapshot"],
                                     frozen["selection"], str(run.id), supplemental=frozen.get("supplemental_snapshot"),
                                     protocol_version=run.orchestration_version)
    if frozen != rebuilt:
        raise ValueError("GROUNDED_INPUT_REBUILD_MISMATCH")
    sources = [{**source, "id": source["source_id"]}
               for source in frozen["retrieval_snapshot"]["documents"][0]["chunks"]]
    sources.extend((frozen.get("supplemental_snapshot") or {}).get("sources", []))
    validate_evidence_context(frozen["context"], sources, [frozen["document"]])
    batch = trace["generation"]
    if batch["request"] != build_grounded_generation_request(frozen["inputs"]):
        raise ValueError("GROUNDED_GENERATION_REQUEST_MISMATCH")
    if batch["raw_response"] is None:
        raise ValueError("GROUNDED_PROVIDER_RESPONSE_UNAVAILABLE")
    recomputed = validate_grounded_response(batch["raw_response"], frozen["inputs"],
        provider_metadata=batch["provider_metadata"], model_calls=1).as_dict()
    if batch != recomputed:
        raise ValueError("GROUNDED_GENERATION_REBUILD_MISMATCH")
    output = _candidate_output(frozen, recomputed, version=run.orchestration_version)
    if run.output != output or trace.get("output_hash") != stable_hash(output):
        raise ValueError("GROUNDED_OUTPUT_REBUILD_MISMATCH")
    expected_status = "candidate_ready" if batch["status"] == "generated" else "failed"
    if run.status != expected_status:
        raise ValueError("GROUNDED_STATUS_MISMATCH")
    return {"status": "passed", "model_called": False, "database_used": False,
            "input_hash": stable_hash(frozen), "output_hash": stable_hash(output),
            "formal_admitted_count": 0, "new_model_conclusion_reproducibility_tested": False}


def grounded_source_freshness(session: Session, run: AnalysisRun) -> dict:
    """An offline replay proves frozen behavior, not freshness of today's PDF."""
    frozen = (run.tool_trace or {}).get("input_snapshot")
    if not isinstance(frozen, dict):
        return {"status": "not_prepared"}
    try:
        _checked_state(session, run, frozen)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        code = str(exc)
        return {"status": "changed_or_unavailable", "reason": code if code.startswith(
            ("GROUNDED_", "TRUSTED_", "FILE_SCAN_")) else "GROUNDED_LIVE_STATE_INVALID"}
    return {"status": "matches_frozen_input"}
