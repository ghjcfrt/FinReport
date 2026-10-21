"""Read-only inspection of experimental grounded-run checkpoints.

This module neither resumes a worker nor reconstructs a result. A persisted
provider-attempt marker cannot establish whether a remote request completed;
a persisted response cannot establish that frozen input is reconstructible.
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict

from finance_adapters.persistence.audit import stable_hash
from finance_adapters.persistence.models import AnalysisRun
from finance_adapters.providers.grounded_analysis import GROUNDED_REPLAY_VERSIONS


class GroundedRecoveryInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["grounded_recovery_inspection_v1"] = "grounded_recovery_inspection_v1"
    analysis_run_id: int
    orchestration_version: str
    recorded_status: str
    classification: Literal["invalid_checkpoint", "invalidated", "provider_outcome_unknown",
                            "saved_response_requires_validation", "preparation_pending_or_active",
                            "terminal_record", "unknown_run_state"]
    inspection_only: Literal[True] = True
    record_mutated: Literal[False] = False
    model_called: Literal[False] = False
    automatic_retry_allowed: Literal[False] = False
    local_reconstruction_verified: Literal[False] = False
    worker_liveness: Literal["not_established"] = "not_established"
    may_have_live_worker: Literal[True] = True
    abandonment_established: Literal[False] = False
    provider_attempt_state: Literal["recorded", "not_recorded", "invalid"]
    provider_outcome: Literal["outcome_unknown", "response_persisted_not_validated", "not_established"]
    raw_response_state: Literal["present", "absent", "invalid", "hash_mismatch"]
    checkpoint_checks: dict[str, bool]
    required_next_checks: list[str]
    boundary: str = (
        "只检查已保存检查点，不判定工作进程已停止或任务已废弃；原始响应存在与哈希一致不等于已可恢复。"
        "本接口不重发请求、不改写历史、不执行本地收尾或确认财务语义。"
    )


def _hash_matches(value: object, expected: object) -> bool:
    if not isinstance(expected, str):
        return False
    try:
        return stable_hash(value) == expected
    except (ValueError, TypeError, RecursionError):
        return False


def plan_grounded_recovery(run: AnalysisRun) -> dict:
    """Describe saved state without querying storage or changing the ORM row."""
    if run.orchestration_version not in GROUNDED_REPLAY_VERSIONS:
        raise ValueError("GROUNDED_RECOVERY_PROTOCOL_UNREGISTERED")
    trace = run.tool_trace if isinstance(run.tool_trace, dict) else {}
    frozen = trace.get("input_snapshot")
    frozen = frozen if isinstance(frozen, dict) else {}
    generation = trace.get("generation")
    generation = generation if isinstance(generation, dict) else {}
    request = trace.get("request")
    document = frozen.get("document")
    attempt = trace.get("provider_attempt_started")
    attempt_state = "recorded" if attempt is True else "not_recorded" if attempt is None or attempt is False else "invalid"
    raw = generation.get("raw_response")
    if raw is None:
        raw_state = "absent"
    elif not isinstance(raw, str):
        raw_state = "invalid"
    else:
        try:
            raw_state = ("present" if hashlib.sha256(raw.encode("utf-8")).hexdigest()
                         == generation.get("raw_response_sha256") else "hash_mismatch")
        except UnicodeError:
            raw_state = "invalid"
    checks = {
        "trace_is_object": isinstance(run.tool_trace, dict),
        "trace_hash_matches": _hash_matches(run.tool_trace, run.tool_trace_hash),
        "protocol_matches_trace": trace.get("version") == run.orchestration_version,
        "provider_attempt_recorded": attempt is True,
        "request_is_object": isinstance(request, dict),
        "request_hash_matches": _hash_matches(request, trace.get("request_hash")),
        "input_snapshot_present": bool(frozen),
        "input_snapshot_shape_present": bool(frozen) and all(isinstance(frozen.get(key), dict)
            for key in ("document", "retrieval_snapshot", "selection", "context", "inputs")),
        "input_snapshot_hash_matches": bool(frozen) and _hash_matches(frozen, run.input_snapshot_hash),
        "input_snapshot_hash_matches_trace": bool(frozen) and trace.get("input_snapshot_hash") == run.input_snapshot_hash,
        "document_matches_run": isinstance(document, dict) and document.get("filing_version_id") == run.filing_version_id,
        "generation_request_is_object": isinstance(generation.get("request"), dict),
        "generation_request_hash_matches": _hash_matches(generation.get("request"), generation.get("request_sha256")),
        "generation_input_hash_matches": bool(frozen) and _hash_matches(frozen.get("inputs"), generation.get("input_sha256")),
        "provider_response_hash_matches": raw_state == "present",
    }
    provider_outcome = ("outcome_unknown" if attempt is True and raw_state == "absent"
                        else "response_persisted_not_validated" if raw_state == "present" else "not_established")
    terminal = run.status in {"candidate_ready", "failed", "invalidated"}
    invalid_base = not all(checks[key] for key in
                           ("trace_is_object", "trace_hash_matches", "protocol_matches_trace"))
    if invalid_base or attempt_state == "invalid" or raw_state in {"invalid", "hash_mismatch"}:
        classification = "invalid_checkpoint"
    elif run.invalidated_at is not None or run.status == "invalidated":
        classification = "invalidated"
    elif attempt is True and raw_state == "absent":
        classification = "provider_outcome_unknown"
    elif raw_state == "present":
        classification = "saved_response_requires_validation"
    elif run.status in {"preparing", "preparing_active", "running"}:
        classification = "preparation_pending_or_active"
    elif terminal:
        classification = "terminal_record"
    else:
        classification = "unknown_run_state"
    next_checks = ["establish_worker_liveness_without_assuming_abandonment"]
    if raw_state == "present":
        next_checks.extend(["validate_complete_frozen_input_and_source_identity",
                            "rebuild_versioned_input_and_request",
                            "validate_versioned_original_response_and_all_receipts",
                            "check_live_source_freshness_before_any_new_publication",
                            "design_explicit_atomic_finalization_before_any_record_change"])
    elif attempt is True:
        next_checks.append("provider_attempt_outcome_is_unknown_do_not_automatically_resend")
    else:
        next_checks.append("inspect_preparation_checkpoint_without_starting_a_new_provider_call")
    return GroundedRecoveryInspection(analysis_run_id=run.id,
        orchestration_version=run.orchestration_version, recorded_status=run.status,
        classification=classification,
        provider_attempt_state=attempt_state, provider_outcome=provider_outcome, raw_response_state=raw_state,
        checkpoint_checks=checks, required_next_checks=next_checks).model_dump(mode="json")
