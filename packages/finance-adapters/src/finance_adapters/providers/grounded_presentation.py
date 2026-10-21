"""Versioned, read-only evidence view over fully reconstructed saved runs.

This does not change a historical protocol or validate its free prose. Only
program templates and re-extracted verbatim observations are displayable.
"""
from __future__ import annotations

import re
from collections import Counter
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_adapters.persistence.audit import stable_hash
from finance_adapters.persistence.models import AnalysisRun
from finance_adapters.providers.disclosed_arithmetic import (
    DisclosedArithmeticCheck,
    DisclosedArithmeticUnknown,
    check_disclosed_arithmetic,
)
from finance_adapters.providers.disclosure_facets import (
    DisclosureFacetObservation,
    extract_disclosure_facets,
)
from finance_adapters.providers.grounded_analysis import replay_grounded_analysis
from finance_adapters.providers.grounded_generation import GroundingIssue

EVIDENCE_VIEW_VERSION = "grounded_evidence_view_v2"
_PROTOCOL = "grounded-report-candidates-v5"
_ALLOWED_LEGACY_ISSUES = frozenset({
    "GROUNDING_SOURCE_CONTEXT_MISMATCH", "GROUNDING_SEMANTIC_SUPPORT_UNVERIFIED",
})


def _candidate_summary(receipt: dict) -> str | None:
    """Detach a fixed v1 template from any unverified provider attribution."""
    if receipt.get("version") != "task_evidence_contract_v1":
        return None
    text = receipt.get("safe_display")
    if not isinstance(text, str):
        return None
    prefixes = {
        "候选登记匹配（未认证）：模型将原文科目":
            "候选登记匹配（响应来源未认证）：当前候选记录将原文科目",
        "候选状态（未认证）：模型将":
            "候选状态（响应来源未认证）：当前候选记录将",
        "候选原文选项观察（未认证）：":
            "候选原文选项观察（响应来源未认证）：",
    }
    matched = [prefix for prefix in prefixes if text.startswith(prefix)]
    if len(matched) != 1:
        return None
    prefix = matched[0]
    text = prefixes[prefix] + text[len(prefix):]
    text = text.replace("不证明模型充分使用了这些材料", "不证明这些材料已被充分利用")
    return text if "模型" not in text else None


class EvidenceViewEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_ref: str
    saved_claim_id_sha256: str
    obligation_id: str
    text: str
    program_generated: Literal[True] = True
    candidate_origin: Literal["saved_response_origin_not_authenticated"] = "saved_response_origin_not_authenticated"
    legacy_provenance_valid: bool
    legacy_issues: list[GroundingIssue]
    task_evidence_sha256: str
    task_template_sha256: str
    summary_basis: Literal["task_evidence_contract_v1_with_provider_attribution_removed"] = (
        "task_evidence_contract_v1_with_provider_attribution_removed"
    )
    task_evidence_state: str
    scope: dict
    program_observations: list[DisclosureFacetObservation]
    quote_anchors: list[dict]
    checkbox_anchor: dict | None
    task_relevance_verified: Literal[False] = False
    semantic_support_verified: Literal[False] = False
    formal_admitted: Literal[False] = False


class SourceArithmeticView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal["disclosed_arithmetic_v1"] = "disclosed_arithmetic_v1"
    source_id: str
    document_sha256: str
    text_sha256: str
    page_no: int | None
    program_generated: Literal[True] = True
    source_full_text_included: Literal[False] = False
    checks: list[DisclosedArithmeticCheck]
    unknowns: list[DisclosedArithmeticUnknown]
    boundary: str
    formal_admitted: Literal[False] = False


class GroundedEvidenceView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["grounded_evidence_view_v2"] = EVIDENCE_VIEW_VERSION
    status: Literal["available", "rejected", "unsupported"]
    analysis_run_id: int
    orchestration_version: str
    local_reconstruction_verified: bool
    input_snapshot_hash: str | None = None
    tool_trace_hash: str | None = None
    historical_output_hash: str | None = None
    original_response_sha256: str | None = None
    export_sha256: str | None = None
    provider_metadata: dict = Field(default_factory=dict)
    provider_metadata_verified: Literal[False] = False
    source_arithmetic_checks: list[SourceArithmeticView] = Field(default_factory=list)
    entries: list[EvidenceViewEntry] = Field(default_factory=list)
    suppressed: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    view_sha256: str | None = None
    free_prose_displayed: Literal[False] = False
    historical_record_mutated: Literal[False] = False
    model_called: Literal[False] = False
    database_used: Literal[False] = False
    network_used: Literal[False] = False
    side_effect_flags_scope: Literal["view_reconstruction_only_api_may_read_saved_run"] = (
        "view_reconstruction_only_api_may_read_saved_run"
    )
    live_source_freshness_checked: Literal[False] = False
    provider_authenticity_verified: Literal[False] = False
    task_relevance_verified: Literal[False] = False
    semantic_support_verified: Literal[False] = False
    formal_admitted: Literal[False] = False
    financial_analysis_complete: Literal[False] = False
    new_model_conclusion_reproducibility_tested: Literal[False] = False
    boundary: str = (
        "本视图只读重建已保存输入、请求、响应及校验记录；候选可能来自真实服务或测试替身，"
        "服务元数据不证明其真实性。仅展示程序模板和逐字原文记录，模型自由正文仍被隔离。"
        "候选状态不证明公司未披露、事项不存在、没有异常、材料已被充分使用或分析任务完成；"
        "精确引用、程序匹配和离线回放均不认证任务相关性或自由叙述的财务语义。"
        "副作用标志仅指视图重构函数，API读取已保存运行记录仍需访问数据库。"
        "未读取当前PDF核验时效性，不作为新的正式财务结论发布。"
    )


def _finish(view: GroundedEvidenceView) -> dict:
    payload = view.model_dump(mode="json")
    payload["view_sha256"] = stable_hash({key: value for key, value in payload.items() if key != "view_sha256"})
    return payload


def build_grounded_evidence_view(run: AnalysisRun) -> dict:
    """Reconstruct first; never query storage, call a model, or mutate the row.

    A context mismatch in a free relation does not authorize that relation.
    It can only cease blocking this separate task-template/observation view.
    Every other legacy error remains a display barrier.
    """
    view = GroundedEvidenceView(status="rejected", analysis_run_id=run.id,
        orchestration_version=run.orchestration_version, local_reconstruction_verified=False)
    try:
        replay = replay_grounded_analysis(run)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError, UnicodeError) as exc:
        code = str(exc)
        view.errors = [code if re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", code)
                       else "GROUNDED_EVIDENCE_VIEW_RECONSTRUCTION_FAILED"]
        return _finish(view)
    view.local_reconstruction_verified = replay.get("status") == "passed"
    if not view.local_reconstruction_verified:
        view.errors = ["GROUNDED_EVIDENCE_VIEW_RECONSTRUCTION_FAILED"]
        return _finish(view)
    view.input_snapshot_hash = run.input_snapshot_hash
    view.tool_trace_hash = run.tool_trace_hash
    view.historical_output_hash = run.tool_trace["output_hash"]
    if run.orchestration_version != _PROTOCOL:
        view.status = "unsupported"
        view.errors = ["GROUNDED_EVIDENCE_VIEW_TASK_PROTOCOL_REQUIRED"]
        return _finish(view)

    generation = run.tool_trace["generation"]
    inputs = run.tool_trace["input_snapshot"]["inputs"]
    manifest = inputs["context_manifest"]
    view.original_response_sha256 = generation["raw_response_sha256"]
    # These remain labelled recorded, unverified metadata. Never include request,
    # response, stream events or arbitrary free model text in the new view.
    view.provider_metadata = {key: generation["provider_metadata"][key]
        for key in ("provider", "model") if key in generation["provider_metadata"]}
    records = generation["candidates"]
    parsed = [record.get("quote_candidate") for record in records]
    claim_counts = Counter(item["claim_id"] for item in parsed if item)
    task_counts = Counter(item["obligation_id"] for item in parsed if item)
    # Re-extract verbatim observations from the frozen source, rather than
    # merely trusting the receipt's matching ID or its display string.
    facets = {}
    for source in inputs["sources"]:
        source_with_page = {**source, "page_no": manifest["source_pages"].get(source["id"])}
        extracted = extract_disclosure_facets(source_with_page)
        for observation in extracted.observations:
            facets[observation.id] = observation
        arithmetic = check_disclosed_arithmetic(source_with_page)
        parent = arithmetic.parent_source
        view.source_arithmetic_checks.append(SourceArithmeticView(source_id=parent.id,
            document_sha256=parent.document_sha256, text_sha256=parent.text_sha256,
            page_no=parent.page_no, checks=list(arithmetic.checks), unknowns=list(arithmetic.unknowns),
            boundary=arithmetic.boundary))

    for index, record in enumerate(records):
        candidate = record.get("quote_candidate")
        validation = record["validation"]
        receipt = record.get("task_evidence_validation") or {}
        candidate_summary = _candidate_summary(receipt)
        resolution = record.get("quote_resolution") or {}
        reasons = []
        if not candidate or not record.get("candidate") or validation.get("structurally_valid") is not True:
            reasons.append("GROUNDED_EVIDENCE_VIEW_STRUCTURE_INVALID")
        if candidate:
            if claim_counts[candidate["claim_id"]] != 1 or task_counts[candidate["obligation_id"]] != 1:
                reasons.append("GROUNDED_EVIDENCE_VIEW_DUPLICATE_ID")
            if candidate["task_evidence"]["obligation_id"] != candidate["obligation_id"]:
                reasons.append("GROUNDED_EVIDENCE_VIEW_TASK_MISMATCH")
        anchors = resolution.get("anchors", [])
        if resolution.get("status") != "resolved" or not anchors or any(
                item.get("status") != "exact_unique" or item.get("match_count") != 1
                or not item.get("resolved_span") for item in anchors):
            reasons.append("GROUNDED_EVIDENCE_VIEW_QUOTES_UNRESOLVED")
        if receipt.get("contract_valid") is not True or receipt.get("display_eligible") is not True:
            reasons.append("GROUNDED_EVIDENCE_VIEW_TASK_CONTRACT_INVALID")
        if candidate_summary is None:
            reasons.append("GROUNDED_EVIDENCE_VIEW_TASK_TEMPLATE_UNSUPPORTED")
        issue_codes = {issue["code"] for issue in validation["issues"]}
        if (issue_codes - _ALLOWED_LEGACY_ISSUES
                or (validation["provenance_valid"] is False
                    and "GROUNDING_SOURCE_CONTEXT_MISMATCH" not in issue_codes)):
            reasons.append("GROUNDED_EVIDENCE_VIEW_LEGACY_ERROR")
        matched = receipt.get("matched_program_facet_ids", [])
        if any(identifier not in facets for identifier in matched):
            reasons.append("GROUNDED_EVIDENCE_VIEW_OBSERVATION_UNRESOLVED")
        if reasons:
            view.suppressed.append({"candidate_index": index, "codes": reasons,
                "legacy_issues": validation["issues"], "task_issues": receipt.get("issues", [])})
            continue
        view.entries.append(EvidenceViewEntry(candidate_ref=f"candidate:{index + 1}",
            saved_claim_id_sha256=sha256(candidate["claim_id"].encode("utf-8")).hexdigest(),
            obligation_id=candidate["obligation_id"], text=candidate_summary,
            legacy_provenance_valid=validation["provenance_valid"], legacy_issues=validation["issues"],
            task_evidence_sha256=receipt["task_evidence_sha256"],
            task_template_sha256=sha256(receipt["safe_display"].encode("utf-8")).hexdigest(),
            task_evidence_state=candidate["task_evidence"]["state"], scope=receipt["scope"],
            program_observations=[facets[identifier] for identifier in matched],
            quote_anchors=anchors, checkbox_anchor=receipt.get("checkbox_anchor")))
    view.status = "available"
    return _finish(view)
