"""Task-specific evidence candidates, never model-issued semantic certificates.

The wire model separates missing facets from explicit form selections.  The
application supplies frozen sources and a *program-built* facet registry; this
module checks that registry's literal anchors, not arbitrary entailment or
business causality.  Every receipt remains quarantined.  Fixed display text
reports candidate/registry status and cannot be used as a nondisclosure claim.
No dependency on grounded_generation is intentional: request profiles may use
this standalone contract without an import cycle or rewriting older traces.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from finance_adapters.providers.disclosure_facets import extract_disclosure_facets

CONTRACT_VERSION = "task_evidence_contract_v1"
Identifier = Annotated[str, Field(min_length=1, max_length=200)]
ObligationId = Literal[
    "non_recurring_composition", "non_recurring_sustainability",
    "accounting_policy_changes", "prior_period_restatement",
    "consolidation_scope_changes", "same_control_combination_conditions",
    "eps_basis_and_adjustment", "going_concern_assessment",
    "cashflow_reconciliation", "cashflow_driver_explanation",
]
EvidenceFacet = Literal[
    "value", "change", "cause", "reconciliation", "composition", "sustainability",
    "policy", "restatement", "consolidation_scope", "combination_conditions",
    "eps_basis", "going_concern",
]
ColumnRole = Literal[
    "current_column", "comparative_column", "reporting_period", "year_to_date", "unspecified",
]
HeaderRole = Literal[
    "current_column", "comparative_column", "change_column", "reporting_period",
    "reporting_period_change", "year_to_date", "year_to_date_change",
]
EvidenceState = Literal[
    "observed", "not_established_in_scope", "explicit_not_applicable",
    "explicit_applicable", "explicit_yes", "explicit_no",
]

_TASKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "non_recurring_composition": ("非经常性损益项目构成", ("composition", "value")),
    "non_recurring_sustainability": ("非经常性损益持续性影响", ("sustainability",)),
    "accounting_policy_changes": ("会计政策或会计估计变更", ("policy",)),
    "prior_period_restatement": ("追溯调整、前期差错更正或重述", ("restatement", "value", "change")),
    "consolidation_scope_changes": ("合并范围变化", ("consolidation_scope",)),
    "same_control_combination_conditions": ("同一控制下企业合并条件性披露", ("combination_conditions",)),
    "eps_basis_and_adjustment": ("每股收益及调整股数口径", ("eps_basis", "value")),
    "going_concern_assessment": ("持续经营假设与重大不确定性", ("going_concern",)),
    "cashflow_reconciliation": ("经营现金流与净利润调节", ("reconciliation",)),
    "cashflow_driver_explanation": ("经营现金流变动原因", ("cause",)),
}
_FACET_LABELS = {
    "value": "指标数值", "change": "指标变动", "cause": "变动原因",
    "reconciliation": "净利润与经营现金流调节", "composition": "项目构成",
    "sustainability": "持续性依据", "policy": "政策或估计变更说明",
    "restatement": "重述或差错调整依据", "consolidation_scope": "合并范围变更说明",
    "combination_conditions": "同一控制合并条件", "eps_basis": "每股收益计算及调整口径",
    "going_concern": "持续经营及重大不确定性说明",
}
_STATE_SELECTION = {
    "explicit_not_applicable": "not_applicable", "explicit_applicable": "applicable",
    "explicit_yes": "yes", "explicit_no": "no",
}
_OPTION_LABELS = {"not_applicable": "不适用", "applicable": "适用", "yes": "是", "no": "否"}
_CHECKED = frozenset("☑☒✓✔\uf052")
_UNCHECKED = frozenset("□☐")
_MARKER = "[□☐☑☒✓✔\uf052]"
_OPTION_PAIR = re.compile(
    rf"\s*(?P<first>{_MARKER}\s*(?P<first_label>不适用|适用|是|否))"
    rf"\s*(?P<second>{_MARKER}\s*(?P<second_label>不适用|适用|是|否))\s*"
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaskEvidenceHeader(_StrictModel):
    role: HeaderRole
    quote: str = Field(min_length=1, max_length=500)


class TaskEvidenceSubject(_StrictModel):
    source_id: Identifier
    quote: str = Field(min_length=1, max_length=1200)
    column_role: ColumnRole
    period_headers: list[TaskEvidenceHeader] = Field(max_length=12)

    @model_validator(mode="after")
    def unique_headers(self):
        if not self.quote.strip():
            raise ValueError("TASK_EVIDENCE_SUBJECT_BLANK")
        pairs = [(header.role, header.quote) for header in self.period_headers]
        if len(pairs) != len(set(pairs)):
            raise ValueError("TASK_EVIDENCE_DUPLICATE_PERIOD_HEADER")
        return self


class TaskEvidenceCheckbox(_StrictModel):
    source_id: Identifier
    question_quote: str = Field(min_length=1, max_length=4000)
    options_quote: str = Field(min_length=1, max_length=500)
    selected_option_quote: str = Field(min_length=1, max_length=250)
    selected_state: Literal["applicable", "not_applicable", "yes", "no"]


class TaskEvidence(_StrictModel):
    obligation_id: ObligationId
    target_facet: EvidenceFacet
    state: EvidenceState
    scope_source_ids: list[Identifier] = Field(max_length=80)
    subject: TaskEvidenceSubject | None
    observation_refs: list[Identifier] = Field(max_length=80)
    checkbox: TaskEvidenceCheckbox | None

    @model_validator(mode="after")
    def consistent_task_state(self):
        if self.target_facet not in _TASKS[self.obligation_id][1]:
            raise ValueError("TASK_EVIDENCE_FACET_NOT_ALLOWED_FOR_OBLIGATION")
        if len(self.scope_source_ids) != len(set(self.scope_source_ids)):
            raise ValueError("TASK_EVIDENCE_DUPLICATE_SCOPE_SOURCE")
        if len(self.observation_refs) != len(set(self.observation_refs)):
            raise ValueError("TASK_EVIDENCE_DUPLICATE_OBSERVATION_REF")
        if self.subject is not None and self.subject.source_id not in self.scope_source_ids:
            raise ValueError("TASK_EVIDENCE_SUBJECT_OUTSIDE_SCOPE")
        if self.state == "observed":
            if self.subject is None or not self.observation_refs or self.checkbox is not None:
                raise ValueError("TASK_EVIDENCE_OBSERVED_REQUIRES_SUBJECT_AND_PROGRAM_REFS")
        elif self.state == "not_established_in_scope":
            if self.observation_refs or self.checkbox is not None:
                raise ValueError("TASK_EVIDENCE_GAP_CANNOT_DOUBLE_AS_OBSERVATION")
        else:
            if self.checkbox is None or self.observation_refs:
                raise ValueError("TASK_EVIDENCE_FORM_STATE_REQUIRES_CHECKBOX_ONLY")
            if self.checkbox.selected_state != _STATE_SELECTION[self.state]:
                raise ValueError("TASK_EVIDENCE_FORM_STATE_SELECTION_MISMATCH")
            if self.checkbox.source_id not in self.scope_source_ids:
                raise ValueError("TASK_EVIDENCE_CHECKBOX_OUTSIDE_SCOPE")
        return self


def task_evidence_registry(task_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Project the finite registry, not inferred company facts, into a request."""
    if len(task_ids) != len(set(task_ids)) or any(task_id not in _TASKS for task_id in task_ids):
        raise ValueError("TASK_EVIDENCE_TASK_REGISTRY_INVALID")
    return {
        "version": CONTRACT_VERSION,
        "obligations": {task_id: {"label": _TASKS[task_id][0],
            "allowed_target_facets": list(_TASKS[task_id][1])} for task_id in task_ids},
        "facet_labels": dict(_FACET_LABELS),
        "states": ["observed", "not_established_in_scope", *_STATE_SELECTION],
        "subject_identity": "Exact source subject, column role and verbatim period headers; no synonym or accounting-context inference.",
        "boundary": "Model-selected fields are candidates, not verified semantic support. A checkbox only reports its own question's selected option; a gap means support was not established in the designated visible-source scope, never company nondisclosure.",
    }


def task_evidence_example(obligation_id: str, source_ids: list[str]) -> dict[str, Any]:
    """Schema-only format example: null subject deliberately invents no source quote.

    Callers must label the surrounding example format-only, not evidence.  This
    function does not inspect a source or establish an actual evidence gap.
    """
    if obligation_id not in _TASKS:
        raise ValueError("TASK_EVIDENCE_OBLIGATION_UNKNOWN")
    return TaskEvidence.model_validate({
        "obligation_id": obligation_id, "target_facet": _TASKS[obligation_id][1][0],
        "state": "not_established_in_scope", "scope_source_ids": source_ids,
        "subject": None, "observation_refs": [], "checkbox": None,
    }).model_dump(mode="json")


def _mapping(value: Any) -> dict[str, Any]:
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    if not isinstance(raw, dict):
        raise TypeError("TASK_EVIDENCE_MAPPING_REQUIRED")
    return raw


def _fingerprint(value: Any) -> str | None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        return None


def _source_registry(sources: list[Any] | tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    registry = {}
    for item in sources:
        source = _mapping(item)
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id or source_id in registry:
            raise ValueError("TASK_EVIDENCE_SOURCE_ID_INVALID")
        text = source.get("text")
        hashes = [source.get("text_sha256"), source.get("document_sha256")]
        if not isinstance(text, str) or any(not isinstance(h, str) or not re.fullmatch("[a-f0-9]{64}", h) for h in hashes):
            raise ValueError("TASK_EVIDENCE_SOURCE_BINDING_REQUIRED")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != source["text_sha256"]:
            raise ValueError("TASK_EVIDENCE_SOURCE_TEXT_HASH_MISMATCH")
        registry[source_id] = source
    return registry


def _exact_span(span: Any, text: str) -> bool:
    return (isinstance(span, dict) and type(span.get("start")) is int and type(span.get("end")) is int
        and isinstance(span.get("quote"), str) and 0 <= span["start"] < span["end"] <= len(text)
        and text[span["start"]:span["end"]] == span["quote"])


def _program_registry(program_facets: list[Any] | tuple[Any, ...],
                      sources: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Verify literal identity of application-built records, not semantic kind.

    Reconstruct each source once and compare the complete record.  A valid
    looking ID, kind, hash or anchor supplied by a model is not authority.
    The receipt still never certifies their independent financial truth.
    """
    registry = {}
    reconstructed = {source_id: {item.id: item.model_dump(mode="json")
        for item in extract_disclosure_facets(source).observations} for source_id, source in sources.items()}
    for item in program_facets:
        facet = _mapping(item)
        identity, source_id = facet.get("id"), facet.get("source_id")
        if not isinstance(identity, str) or not identity or identity in registry or source_id not in sources:
            raise ValueError("TASK_EVIDENCE_PROGRAM_REGISTRY_ID_INVALID")
        source = sources[source_id]
        if (facet.get("kind") not in _FACET_LABELS or not _exact_span(facet.get("subject"), source["text"])
                or facet.get("document_sha256") != source["document_sha256"]
                or facet.get("text_sha256") != source["text_sha256"]):
            raise ValueError("TASK_EVIDENCE_PROGRAM_FACET_BINDING_INVALID")
        role = facet.get("column_role", "unspecified")
        if role not in {"current_column", "comparative_column", "reporting_period", "year_to_date", "unspecified"}:
            raise ValueError("TASK_EVIDENCE_PROGRAM_COLUMN_ROLE_INVALID")
        headers = facet.get("period_headers", [])
        if not isinstance(headers, (list, tuple)) or any(
                not isinstance(h, dict) or not isinstance(h.get("role"), str)
                or not _exact_span(h.get("span"), source["text"]) for h in headers):
            raise ValueError("TASK_EVIDENCE_PROGRAM_PERIOD_HEADERS_INVALID")
        # page_no must come from frozen application context.  Never guess
        # it from a model record or a source identifier.
        if facet != reconstructed[source_id].get(identity):
            raise ValueError("TASK_EVIDENCE_PROGRAM_FACET_NOT_RECONSTRUCTED")
        registry[identity] = facet
    if set(registry) != {identity for observations in reconstructed.values() for identity in observations}:
        raise ValueError("TASK_EVIDENCE_PROGRAM_REGISTRY_INCOMPLETE")
    return registry


def _header_identity(headers: list[Any] | tuple[Any, ...], *, program: bool) -> set[tuple[str, str]]:
    return {(h["role"], h["span"]["quote"] if program else h["quote"]) for h in headers}


def _subject_match(facet: dict, subject: TaskEvidenceSubject,
                   sources: dict[str, dict[str, Any]]) -> Literal["different", "ambiguous", "same"]:
    if (facet["subject"]["quote"] != subject.quote
            or facet["document_sha256"] != sources[subject.source_id]["document_sha256"]):
        return "different"
    if facet["source_id"] != subject.source_id:
        # Identical generic headers on separate pages do not establish the
        # same row or accounting context.  Flag possible counterevidence,
        # without asserting a cross-page contradiction or positive match.
        return "ambiguous"
    role = facet.get("column_role", "unspecified")
    if role != "unspecified" and subject.column_role != "unspecified" and role != subject.column_role:
        return "different"
    if role == "unspecified" or subject.column_role == "unspecified":
        return "ambiguous"
    actual = _header_identity(facet.get("period_headers", []), program=True)
    claimed = _header_identity([h.model_dump(mode="json") for h in subject.period_headers], program=False)
    if not actual or not claimed:
        return "ambiguous"
    # Partial or mismatched table identities cannot establish a different
    # period.  Keep them unresolved rather than letting an omitted header
    # evade an existing record or treating a source-wide label as one period.
    return "same" if actual == claimed else "ambiguous"


def _checkbox_anchor(checkbox: TaskEvidenceCheckbox, source: dict) -> tuple[dict | None, str | None]:
    text, question = source["text"], checkbox.question_quote
    positions = [i for i in range(len(text) - len(question) + 1) if text.startswith(question, i)]
    if not question.strip() or len(positions) != 1:
        return None, "TASK_EVIDENCE_CHECKBOX_QUESTION_NOT_UNIQUE"
    start = positions[0]
    end = start + len(question)
    if text[text.rfind("\n", 0, start) + 1:start].strip():
        return None, "TASK_EVIDENCE_CHECKBOX_QUESTION_BOUNDARY_INVALID"
    option_start = end + len(text[end:]) - len(text[end:].lstrip())
    if not text.startswith(checkbox.options_quote, option_start):
        return None, "TASK_EVIDENCE_CHECKBOX_OPTIONS_NOT_ADJACENT"
    pair = _OPTION_PAIR.fullmatch(checkbox.options_quote)
    if pair is None or {pair["first_label"], pair["second_label"]} not in ({"适用", "不适用"}, {"是", "否"}):
        return None, "TASK_EVIDENCE_CHECKBOX_OPTION_PAIR_INVALID"
    option_end = option_start + len(checkbox.options_quote.rstrip())
    tail = text[option_end:]
    current_line, _, subsequent = tail.partition("\n")
    if current_line.strip():
        return None, "TASK_EVIDENCE_CHECKBOX_OPTION_REGION_INCOMPLETE"
    next_line = next((line.lstrip() for line in subsequent.splitlines() if line.strip()), "")
    if next_line and next_line[0] in _CHECKED | _UNCHECKED:
        return None, "TASK_EVIDENCE_CHECKBOX_OPTION_REGION_INCOMPLETE"
    options = [(pair[key], pair[f"{key}_label"]) for key in ("first", "second")]
    checked = [(option, label) for option, label in options if option[0] in _CHECKED]
    if len(checked) != 1 or any(option[0] not in _CHECKED | _UNCHECKED for option, _ in options):
        return None, "TASK_EVIDENCE_CHECKBOX_SELECTION_AMBIGUOUS"
    selected, label = checked[0]
    if selected != checkbox.selected_option_quote or label != _OPTION_LABELS[checkbox.selected_state]:
        return None, "TASK_EVIDENCE_CHECKBOX_SELECTED_OPTION_MISMATCH"
    return {"source_id": source["id"], "question_start": start, "question_end": end,
        "options_start": option_start, "options_end": option_start + len(checkbox.options_quote),
        "selected_option_quote": selected, "selected_label": label,
        "question_quote": question, "question_completeness_verified": False,
        "task_relevance_verified": False}, None


def _safe_display(evidence: TaskEvidence, scope: dict, matched: list[str], checkbox: dict | None) -> str:
    task, facet = _TASKS[evidence.obligation_id][0], _FACET_LABELS[evidence.target_facet]
    if checkbox is not None:
        return (f"候选原文选项观察（未认证）：片段“{checkbox['question_quote']}”紧接的选项中，“{checkbox['selected_label']}”被勾选。"
            "此处仅记录相邻原文选项，问项完整性与任务相关性尚未核验；不代表该分析任务已完成、指标不存在或没有财务异常。")
    if evidence.state == "observed":
        return (f"候选登记匹配（未认证）：模型将原文科目“{evidence.subject.quote}”分配给“{task}”；"
            f"该科目的“{facet}”标记匹配了{len(matched)}条程序原文记录。"
            "科目与任务的相关性尚未核验，该匹配不构成独立语义证明，也不代表整项分析任务完成。")
    scope_text = "本次没有提供可见原文来源" if not scope["source_ids"] else f"本次所列{len(scope['source_ids'])}个可见来源"
    return (f"候选状态（未认证）：模型将“{task}”的“{facet}”登记为在{scope_text}中尚未建立支持。"
        "这是候选取证状态，不表示公司未披露、事项不存在或没有财务异常，也不证明模型充分使用了这些材料。")


def validate_task_evidence(payload: TaskEvidence | dict, *, obligation_id: str,
                           sources: list[Any] | tuple[Any, ...],
                           program_facets: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    """Return a JSON receipt; model fields never certify their own semantics.

    A valid receipt only permits the fixed *candidate* display.  Arbitrary
    statement/relation/basis validation and formal financial admission remain
    separate.  Inputs, including existing model instances, are revalidated.
    """
    raw = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    issues: list[dict[str, str]] = []
    parsed = None
    source_map: dict[str, dict[str, Any]] = {}
    facts: dict[str, dict[str, Any]] = {}
    matched: list[str] = []
    checkbox_anchor = None
    try:
        parsed = TaskEvidence.model_validate(raw)
    except ValidationError as exc:
        issues.extend({"code": "TASK_EVIDENCE_SCHEMA_INVALID", "path": ".".join(map(str, error["loc"])),
            "message": error["type"]} for error in exc.errors(include_input=False, include_context=False))
    try:
        source_map = _source_registry(sources)
        facts = _program_registry(program_facets, source_map)
    except (ValueError, TypeError, UnicodeError) as exc:
        issues.append({"code": "TASK_EVIDENCE_CONTEXT_INVALID", "path": "program_context", "message": str(exc)})
    if parsed is not None:
        if parsed.obligation_id != obligation_id:
            issues.append({"code": "TASK_EVIDENCE_OBLIGATION_MISMATCH", "path": "obligation_id", "message": "候选问项与外层分配任务不同。"})
        unknown = set(parsed.scope_source_ids) - set(source_map)
        if unknown or (source_map and not parsed.scope_source_ids):
            issues.append({"code": "TASK_EVIDENCE_SCOPE_INVALID", "path": "scope_source_ids", "message": "只能登记本批实际可见的非空来源范围；不得扩大为整份财报。"})
        if parsed.subject is not None:
            source = source_map.get(parsed.subject.source_id)
            if source is None or parsed.subject.quote not in source["text"]:
                issues.append({"code": "TASK_EVIDENCE_SUBJECT_ANCHOR_INVALID", "path": "subject", "message": "科目必须逐字存在于指定的当前来源中。"})
            elif any(header.quote not in source["text"] for header in parsed.subject.period_headers):
                issues.append({"code": "TASK_EVIDENCE_SUBJECT_PERIOD_HEADER_INVALID", "path": "subject.period_headers", "message": "期间表头必须逐字存在于科目来源；不推断日期区间。"})
            else:
                for reference in parsed.observation_refs:
                    record = facts.get(reference)
                    if (record is None or record["source_id"] not in parsed.scope_source_ids
                            or record["kind"] != parsed.target_facet
                            or _subject_match(record, parsed.subject, source_map) != "same"):
                        issues.append({"code": "TASK_EVIDENCE_PROGRAM_REF_MISMATCH", "path": "observation_refs", "message": "程序记录必须同科目、文档、列角色、期间表头及目标facet。"})
                    else:
                        matched.append(reference)
                if parsed.state == "not_established_in_scope":
                    identities = [_subject_match(record, parsed.subject, source_map) for record in facts.values()
                        if record["kind"] == parsed.target_facet]
                    if "same" in identities:
                        issues.append({"code": "TASK_EVIDENCE_TARGET_ALREADY_REGISTERED", "path": "target_facet", "message": "该身份已存在同facet程序记录；不能通过缩小来源子集将其登记为未建立。"})
                    elif "ambiguous" in identities:
                        issues.append({"code": "TASK_EVIDENCE_TARGET_IDENTITY_AMBIGUOUS", "path": "subject", "message": "同科目已有记录，但列或期间身份未明确；不能将累计金额当成单季，也不能据此认证缺口。"})
        if parsed.checkbox is not None:
            source = source_map.get(parsed.checkbox.source_id)
            if source is None:
                code = "TASK_EVIDENCE_CHECKBOX_SOURCE_UNKNOWN"
            else:
                checkbox_anchor, code = _checkbox_anchor(parsed.checkbox, source)
            if code is not None:
                issues.append({"code": code, "path": "checkbox", "message": "问项、紧邻选项、唯一勾选与选中标签必须在同一来源完整匹配。"})
    scope_ids = list(parsed.scope_source_ids) if parsed is not None else []
    scope = {"kind": "no_visible_sources" if not source_map else "all_visible_sources" if set(scope_ids) == set(source_map)
        else "selected_visible_sources", "source_ids": scope_ids, "all_visible_source_ids": list(source_map),
        "is_full_document": False, "model_usage_verified": False}
    valid = parsed is not None and not issues
    return {"version": CONTRACT_VERSION, "task_evidence_sha256": _fingerprint(raw),
        "contract_valid": valid, "display_eligible": valid,
        "safe_display": _safe_display(parsed, scope, matched, checkbox_anchor) if valid else None,
        "issues": issues, "matched_program_facet_ids": matched, "scope": scope,
        "checkbox_anchor": checkbox_anchor, "task_relevance_verified": False,
        "semantic_support_verified": False,
        "formal_admitted": False, "admission": "quarantined"}
