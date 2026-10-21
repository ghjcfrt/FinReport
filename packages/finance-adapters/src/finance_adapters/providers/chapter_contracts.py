"""Versioned wire contracts and deterministic gates for chapter evidence analysis.

These gates establish literal/source/dependency integrity, not financial truth.
A semantic receipt records a model's support judgment under a frozen contract;
it is never an audit opinion or permission to promote a fact to ``auto_passed``.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "chapter_evidence_contract_v1"
POLICY_VERSION = "chapter_evidence_admission_v1"
TOOL_VERSION = "chapter_evidence_tools_v1"
Identifier = Annotated[str, Field(min_length=1, max_length=240, pattern=r"\S")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Verdict = Literal["supported", "partially_supported", "insufficient_evidence", "contradicted"]
ROLE_NAMES = (
    "chapter_plan_v1", "chapter_analysis_v1", "financial_topic_v1",
    "uncovered_material_v1", "claim_evidence_verify_v1", "claim_evidence_verify_v2", "report_synthesis_v1",
)
FINANCIAL_TOPICS = (
    "year_on_year", "quarter_on_quarter", "non_recurring_profit",
    "accounting_changes_and_restatement", "consolidation_scope", "eps_basis",
    "going_concern", "profit_cashflow_divergence", "operating_cashflow_drivers",
)


def canonical_hash(value: Any) -> str:
    """Content fingerprint only: not a signature or external authenticity claim."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FinancialScope(StrictModel):
    subject: str | None = None
    object: str | None = None
    period: str | None = None
    basis: str | None = None
    unit: str | None = None
    currency: str | None = None
    cumulative: bool | None = None
    restated: bool | None = None
    conditions: list[str] = Field(default_factory=list, max_length=40)


class EvidenceUnit(StrictModel):
    unit_id: Identifier
    document_id: Identifier
    chapter_id: Identifier
    title: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    locator: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""

    @model_validator(mode="after")
    def bind_content(self):
        expected = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.content_hash and self.content_hash != expected:
            raise ValueError("CHAPTER_EVIDENCE_CONTENT_HASH_MISMATCH")
        self.content_hash = expected
        return self

    @property
    def binding_hash(self) -> str:
        # Include report/period/locator metadata: unchanged text may change identity.
        return canonical_hash(self)


class EvidenceRef(StrictModel):
    evidence_id: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    role: Literal["support", "counter", "qualification", "context"] = "support"

    @model_validator(mode="after")
    def ordered_span(self):
        if self.end <= self.start or len(self.quote) != self.end - self.start:
            raise ValueError("CHAPTER_EVIDENCE_SPAN_LENGTH_MISMATCH")
        return self


class ChapterClaim(StrictModel):
    claim_id: Identifier
    task_id: Identifier
    statement: str = Field(min_length=1, max_length=16000)
    claim_type: Literal["disclosure", "calculation", "inference", "limitation"]
    revision: int = Field(default=1, ge=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=100)
    calculation_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    scope: FinancialScope = Field(default_factory=FinancialScope)
    rationale: str = Field(default="", max_length=12000)
    limitations: list[str] = Field(default_factory=list, max_length=80)
    assumptions: list[str] = Field(default_factory=list, max_length=80)
    checked_evidence_ids: list[Identifier] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def require_basis(self):
        if not self.statement.strip():
            raise ValueError("CHAPTER_CLAIM_BLANK")
        if len(self.calculation_ids) != len(set(self.calculation_ids)):
            raise ValueError("CHAPTER_CLAIM_DUPLICATE_CALCULATION")
        if len(self.checked_evidence_ids) != len(set(self.checked_evidence_ids)):
            raise ValueError("CHAPTER_CLAIM_DUPLICATE_CHECKED_EVIDENCE")
        if self.claim_type == "calculation" and not self.calculation_ids:
            raise ValueError("CHAPTER_CALCULATION_CLAIM_REQUIRES_PROGRAM_CALCULATION")
        if self.claim_type == "limitation":
            if not self.limitations:
                raise ValueError("CHAPTER_LIMITATION_REQUIRES_EXPLICIT_BOUNDARY")
        elif not self.evidence_refs and not self.calculation_ids:
            raise ValueError("CHAPTER_CLAIM_REQUIRES_EVIDENCE")
        return self

    @property
    def revision_hash(self) -> str:
        return canonical_hash(self)


class FactOperand(StrictModel):
    fact_id: Identifier
    value: str
    validation_status: Literal["candidate", "auto_passed", "needs_review", "rejected"]
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    scope: FinancialScope = Field(default_factory=FinancialScope)

    @model_validator(mode="after")
    def finite_decimal(self):
        try:
            if not Decimal(self.value).is_finite():
                raise ValueError("CHAPTER_OPERAND_NONFINITE")
        except InvalidOperation as exc:
            raise ValueError("CHAPTER_OPERAND_DECIMAL_REQUIRED") from exc
        return self


class CalculationRecord(StrictModel):
    calculation_id: Identifier
    formula: str = Field(min_length=1)
    operands: list[FactOperand] = Field(min_length=1)
    value: str | None = None
    status: Literal["succeeded", "rejected", "failed"]
    scope: FinancialScope = Field(default_factory=FinancialScope)
    assumptions: list[str] = Field(default_factory=list)
    policy_version: str = "chapter_decimal_v1"

    @model_validator(mode="after")
    def valid_result(self):
        if self.status == "succeeded":
            if self.value is None:
                raise ValueError("CHAPTER_CALCULATION_VALUE_REQUIRED")
            try:
                if not Decimal(self.value).is_finite():
                    raise ValueError("CHAPTER_CALCULATION_NONFINITE")
            except InvalidOperation as exc:
                raise ValueError("CHAPTER_CALCULATION_DECIMAL_REQUIRED") from exc
            if any(item.validation_status != "auto_passed" for item in self.operands):
                raise ValueError("CHAPTER_CALCULATION_INPUT_NOT_AUTO_PASSED")
        return self


class MaterialDisposition(StrictModel):
    evidence_id: Identifier
    disposition: Literal[
        "checked", "support", "counter", "qualification", "new_task",
        "relevant_unreferenced", "duplicate", "irrelevant", "unresolved",
    ]
    reason: str = Field(min_length=1, max_length=12000)
    claim_ids: list[Identifier] = Field(default_factory=list)
    representative_evidence_id: Identifier | None = None

    @model_validator(mode="after")
    def duplicate_mapping(self):
        if self.disposition == "duplicate":
            if not self.representative_evidence_id or self.representative_evidence_id == self.evidence_id:
                raise ValueError("CHAPTER_DUPLICATE_REQUIRES_OTHER_REPRESENTATIVE")
        elif self.representative_evidence_id is not None:
            raise ValueError("CHAPTER_REPRESENTATIVE_ONLY_FOR_DUPLICATE")
        return self


class TaskSpec(StrictModel):
    task_id: Identifier
    kind: Literal["chapter", "financial_topic", "uncovered_material"]
    objective: str = Field(min_length=1, max_length=12000)
    document_ids: list[Identifier] = Field(min_length=1, max_length=100)
    chapter_ids: list[Identifier] = Field(default_factory=list)
    evidence_ids: list[Identifier] = Field(default_factory=list)
    parent_task_id: Identifier | None = None
    dependencies: list[Identifier] = Field(default_factory=list)
    topic: str | None = None

    @model_validator(mode="after")
    def identity_sets(self):
        for items in (self.document_ids, self.chapter_ids, self.evidence_ids, self.dependencies):
            if len(items) != len(set(items)):
                raise ValueError("CHAPTER_TASK_DUPLICATE_ID")
        if self.task_id in self.dependencies or self.task_id == self.parent_task_id:
            raise ValueError("CHAPTER_TASK_SELF_DEPENDENCY")
        return self


class MissingDocument(StrictModel):
    report_name: str = Field(min_length=1)
    period: str | None = None
    required_content: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class TaskAnswer(StrictModel):
    claims: list[ChapterClaim] = Field(default_factory=list, max_length=100)
    material_dispositions: list[MaterialDisposition] = Field(default_factory=list, max_length=1000)
    new_tasks: list[TaskSpec] = Field(default_factory=list, max_length=100)
    missing_documents: list[MissingDocument] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=100)
    completion: Literal["completed", "partial", "waiting_for_documents"] = "completed"

    @model_validator(mode="after")
    def distinct_ids(self):
        for items in ([claim.claim_id for claim in self.claims],
                      [task.task_id for task in self.new_tasks],
                      [item.evidence_id for item in self.material_dispositions]):
            if len(items) != len(set(items)):
                raise ValueError("CHAPTER_TASK_ANSWER_DUPLICATE_ID")
        if self.completion == "waiting_for_documents" and not self.missing_documents:
            raise ValueError("CHAPTER_WAITING_REQUIRES_DOCUMENT_REQUEST")
        return self


class ToolArguments(StrictModel):
    query: str | None = None
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    document_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    chapter_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    top_k: int = Field(default=8, ge=1, le=40)
    context_radius: int = Field(default=1, ge=0, le=3)
    start: int = Field(default=0, ge=0)
    end: int | None = Field(default=None, gt=0)
    calculation_id: Identifier | None = None
    formula: Literal[
        "difference", "sum", "ratio", "growth_rate", "yoy", "qoq", "single_quarter",
    ] | None = None
    fact_ids: list[Identifier] = Field(default_factory=list, max_length=100)


class ToolRequest(StrictModel):
    call_id: Identifier
    name: Literal[
        "directory", "search_evidence", "read_evidence", "read_context", "query_facts", "calculate",
    ]
    arguments: ToolArguments

    @model_validator(mode="after")
    def required_tool_arguments(self):
        args = self.arguments
        if args.end is not None and args.end <= args.start:
            raise ValueError("CHAPTER_TOOL_READ_RANGE_INVALID")
        if self.name in {"search_evidence", "query_facts"} and not (args.query and args.query.strip()):
            raise ValueError("CHAPTER_TOOL_QUERY_REQUIRED")
        if self.name in {"read_evidence", "read_context"} and not args.evidence_ids:
            raise ValueError("CHAPTER_TOOL_EVIDENCE_IDS_REQUIRED")
        if self.name == "calculate":
            if args.calculation_id is not None:
                if args.formula is not None or args.fact_ids:
                    raise ValueError("CHAPTER_TOOL_CALCULATION_READ_OR_EXECUTE_ONLY")
            elif not args.formula or not args.fact_ids:
                raise ValueError("CHAPTER_TOOL_CALCULATION_ARGUMENTS_REQUIRED")
        if self.name != "calculate" and (args.formula or args.fact_ids or args.calculation_id):
            raise ValueError("CHAPTER_TOOL_UNEXPECTED_CALCULATION_ARGUMENTS")
        return self


class TaskPlan(StrictModel):
    tasks: list[TaskSpec] = Field(default_factory=list, max_length=1000)
    missing_documents: list[MissingDocument] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_tasks(self):
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("CHAPTER_PLAN_DUPLICATE_TASK")
        return self


class SemanticAssessment(StrictModel):
    claim_id: Identifier
    verdict: Verdict
    reason: str = Field(min_length=1, max_length=12000)
    issue_fields: list[str] = Field(default_factory=list, max_length=80)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    follow_up_queries: list[str] = Field(default_factory=list, max_length=40)


class VerificationReceipt(StrictModel):
    receipt_id: Identifier
    claim_id: Identifier
    claim_revision: int = Field(ge=1)
    claim_revision_hash: Hash
    execution_status: Literal["succeeded", "failed", "protocol_failed", "source_invalidated"]
    verdict: Verdict | None
    reason: str
    evidence_hashes: dict[str, Hash]
    calculation_hashes: dict[str, Hash]
    read_evidence_ids: list[Identifier]
    read_ranges: dict[str, list[list[int]]]
    program_issues: list[str]
    issue_fields: list[str]
    follow_up_queries: list[str]
    policy_version: str
    prompt_hash: Hash
    source_snapshot_hash: Hash
    verifier_model: str | None = None
    created_at_utc: str
    invalidated_by: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def execution_not_semantics(self):
        if (self.execution_status == "succeeded") != (self.verdict is not None):
            raise ValueError("CHAPTER_RECEIPT_EXECUTION_VERDICT_MISMATCH")
        return self

    @property
    def admitted(self) -> bool:
        return (self.execution_status == "succeeded" and self.verdict == "supported"
                and not self.program_issues and not self.invalidated_by)


class ReportJudgment(StrictModel):
    text: str = Field(min_length=1, max_length=16000)
    claim_ids: list[Identifier] = Field(min_length=1, max_length=100)


class ReportSection(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    judgments: list[ReportJudgment] = Field(default_factory=list, max_length=1000)


class SynthesisConflict(StrictModel):
    claim_ids: list[Identifier] = Field(min_length=2, max_length=100)
    reason: str = Field(min_length=1, max_length=12000)
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def distinct_conflict_members(self):
        if (len(self.claim_ids) != len(set(self.claim_ids))
                or len(self.evidence_ids) != len(set(self.evidence_ids))):
            raise ValueError("CHAPTER_SYNTHESIS_CONFLICT_DUPLICATE_ID")
        if not self.reason.strip():
            raise ValueError("CHAPTER_SYNTHESIS_CONFLICT_REASON_REQUIRED")
        return self


class SynthesisReport(StrictModel):
    sections: list[ReportSection] = Field(default_factory=list, max_length=100)
    new_claims: list[ChapterClaim] = Field(default_factory=list, max_length=100)
    unresolved_items: list[str] = Field(default_factory=list, max_length=1000)
    conflicts: list[SynthesisConflict] = Field(default_factory=list, max_length=100)


class TaskTurn(StrictModel):
    tool_requests: list[ToolRequest] = Field(default_factory=list, max_length=12)
    answer: TaskAnswer | None = None

    @model_validator(mode="after")
    def exclusive(self):
        if bool(self.tool_requests) == (self.answer is not None):
            raise ValueError("CHAPTER_TURN_REQUIRES_TOOLS_OR_ANSWER")
        return self


class PlanTurn(StrictModel):
    tool_requests: list[ToolRequest] = Field(default_factory=list, max_length=12)
    plan: TaskPlan | None = None

    @model_validator(mode="after")
    def exclusive(self):
        if bool(self.tool_requests) == (self.plan is not None):
            raise ValueError("CHAPTER_TURN_REQUIRES_TOOLS_OR_PLAN")
        return self


class VerifierTurn(StrictModel):
    tool_requests: list[ToolRequest] = Field(default_factory=list, max_length=12)
    assessments: list[SemanticAssessment] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def exclusive(self):
        if bool(self.tool_requests) == bool(self.assessments):
            raise ValueError("CHAPTER_TURN_REQUIRES_TOOLS_OR_ASSESSMENTS")
        ids = [item.claim_id for item in self.assessments]
        if len(ids) != len(set(ids)):
            raise ValueError("CHAPTER_VERIFIER_DUPLICATE_CLAIM")
        return self


class SynthesisTurn(StrictModel):
    tool_requests: list[ToolRequest] = Field(default_factory=list, max_length=12)
    synthesis: SynthesisReport | None = None

    @model_validator(mode="after")
    def exclusive(self):
        if bool(self.tool_requests) == (self.synthesis is not None):
            raise ValueError("CHAPTER_TURN_REQUIRES_TOOLS_OR_SYNTHESIS")
        return self


def _unit_map(units: Mapping[str, Any] | Sequence[Any]) -> dict[str, EvidenceUnit]:
    values = units.values() if isinstance(units, Mapping) else units
    result = {}
    for value in values:
        unit = value if isinstance(value, EvidenceUnit) else EvidenceUnit.model_validate(value)
        if unit.unit_id in result:
            raise ValueError("CHAPTER_DUPLICATE_EVIDENCE_ID")
        result[unit.unit_id] = unit
    return result


def _calculation_map(calculations: Mapping[str, Any] | Sequence[Any] | None) -> dict[str, CalculationRecord]:
    values = calculations.values() if isinstance(calculations, Mapping) else calculations or []
    result = {}
    for value in values:
        record = value if isinstance(value, CalculationRecord) else CalculationRecord.model_validate(value)
        if record.calculation_id in result:
            raise ValueError("CHAPTER_DUPLICATE_CALCULATION_ID")
        result[record.calculation_id] = record
    return result


def _ref_issues(ref: EvidenceRef, units: dict[str, EvidenceUnit], authorized: set[str]) -> list[str]:
    unit = units.get(ref.evidence_id)
    if unit is None:
        return [f"EVIDENCE_UNKNOWN:{ref.evidence_id}"]
    issues = []
    if unit.document_id not in authorized:
        issues.append(f"DOCUMENT_NOT_AUTHORIZED:{unit.document_id}")
    if unit.text[ref.start:ref.end] != ref.quote:
        issues.append(f"EVIDENCE_SPAN_MISMATCH:{ref.evidence_id}")
    return issues


def validate_claim(claim: ChapterClaim, units: Mapping[str, Any] | Sequence[Any],
                   authorized_document_ids: Sequence[str] | set[str],
                   calculations: Mapping[str, Any] | Sequence[Any] | None = None) -> list[str]:
    """Literal/dependency checks; no arbitrary semantic entailment is inferred."""
    registry, authorized = _unit_map(units), set(authorized_document_ids)
    records = _calculation_map(calculations)
    issues = []
    for ref in claim.evidence_refs:
        issues.extend(_ref_issues(ref, registry, authorized))
    for evidence_id in claim.checked_evidence_ids:
        unit = registry.get(evidence_id)
        if unit is None:
            issues.append(f"CHECKED_EVIDENCE_UNKNOWN:{evidence_id}")
        elif unit.document_id not in authorized:
            issues.append(f"DOCUMENT_NOT_AUTHORIZED:{unit.document_id}")
    if claim.claim_type == "limitation" and not claim.checked_evidence_ids:
        issues.append("LIMITATION_CHECKED_SCOPE_REQUIRED")
    for calculation_id in claim.calculation_ids:
        calculation = records.get(calculation_id)
        if calculation is None:
            issues.append(f"CALCULATION_UNKNOWN:{calculation_id}")
            continue
        if calculation.status != "succeeded":
            issues.append(f"CALCULATION_NOT_SUCCEEDED:{calculation_id}")
        for operand in calculation.operands:
            if operand.validation_status != "auto_passed":
                issues.append(f"CALCULATION_INPUT_NOT_AUTO_PASSED:{operand.fact_id}")
            for ref in operand.evidence_refs:
                issues.extend(_ref_issues(ref, registry, authorized))
        for field in ("period", "basis", "unit", "currency", "cumulative", "restated"):
            expected, actual = getattr(claim.scope, field), getattr(calculation.scope, field)
            if expected is not None and actual is not None and expected != actual:
                issues.append(f"CLAIM_CALCULATION_SCOPE_MISMATCH:{calculation_id}:{field}")
    return sorted(set(issues))


def source_snapshot_hash(units: Mapping[str, Any] | Sequence[Any],
                         authorized_document_ids: Sequence[str] | set[str]) -> str:
    registry, authorized = _unit_map(units), set(authorized_document_ids)
    return canonical_hash({"authorized_document_ids": sorted(authorized), "units": {
        key: unit.binding_hash for key, unit in sorted(registry.items())
        if unit.document_id in authorized}})


def _range_covers(ranges: list[list[int]], start: int, end: int) -> bool:
    cursor = start
    for left, right in sorted(ranges):
        if left > cursor:
            break
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def _reading_issues(claim: ChapterClaim, registry: dict[str, EvidenceUnit],
                    records: dict[str, CalculationRecord], read: set[str],
                    ranges: dict[str, list[list[int]]]) -> list[str]:
    issues = []
    if set(ranges) != read:
        issues.append("READ_RANGE_ID_SET_MISMATCH")
    for evidence_id in read:
        unit = registry.get(evidence_id)
        spans = ranges.get(evidence_id, [])
        if not spans:
            issues.append(f"READ_RANGE_MISSING:{evidence_id}")
        if unit is not None:
            for span in spans:
                if (len(span) != 2 or any(type(value) is not int for value in span)
                        or not 0 <= span[0] < span[1] <= len(unit.text)):
                    issues.append(f"READ_RANGE_INVALID:{evidence_id}")
    valid_ranges = {key: [span for span in spans if len(span) == 2
                         and all(type(value) is int for value in span)
                         and key in registry and key in read
                         and 0 <= span[0] < span[1] <= len(registry[key].text)]
                    for key, spans in ranges.items()}
    refs = list(claim.evidence_refs)
    for calculation_id in claim.calculation_ids:
        if calculation_id in records:
            refs.extend(ref for operand in records[calculation_id].operands
                        for ref in operand.evidence_refs)
    for ref in refs:
        if not _range_covers(valid_ranges.get(ref.evidence_id, []), ref.start, ref.end):
            issues.append(f"VERIFIER_DID_NOT_READ_SPAN:{ref.evidence_id}:{ref.start}:{ref.end}")
    for evidence_id in claim.checked_evidence_ids:
        if evidence_id in registry and not _range_covers(
                valid_ranges.get(evidence_id, []), 0, len(registry[evidence_id].text)):
            issues.append(f"VERIFIER_CHECKED_SCOPE_NOT_FULLY_READ:{evidence_id}")
    for ref in refs:
        if ref.evidence_id not in registry:
            continue
        context_ids = registry[ref.evidence_id].metadata.get("required_context_ids", [])
        if not isinstance(context_ids, list) or any(not isinstance(key, str) for key in context_ids):
            issues.append(f"EVIDENCE_CONTEXT_METADATA_INVALID:{ref.evidence_id}")
            continue
        for context_id in context_ids:
            if context_id not in registry:
                issues.append(f"EVIDENCE_CONTEXT_UNKNOWN:{context_id}")
            elif not _range_covers(valid_ranges.get(context_id, []), 0, len(registry[context_id].text)):
                issues.append(f"VERIFIER_REQUIRED_CONTEXT_NOT_READ:{context_id}")
    return issues


def build_receipt(assessment: SemanticAssessment, claim: ChapterClaim,
                  units: Mapping[str, Any] | Sequence[Any],
                  authorized_document_ids: Sequence[str] | set[str],
                  read_evidence_ids: Sequence[str],
                  calculations: Mapping[str, Any] | Sequence[Any] | None,
                  prompt_hash: str, policy_version: str = POLICY_VERSION,
                  verifier_model: str | None = None,
                  read_ranges: dict[str, list[list[int]]] | None = None) -> VerificationReceipt:
    registry, records = _unit_map(units), _calculation_map(calculations)
    authorized, read = set(authorized_document_ids), set(read_evidence_ids)
    ranges = (read_ranges if read_ranges is not None else {
        key: [[0, len(registry[key].text)]] for key in read if key in registry})
    issues = validate_claim(claim, registry, authorized, records)
    issues.extend(_reading_issues(claim, registry, records, read, ranges))
    if assessment.claim_id != claim.claim_id:
        issues.append("ASSESSMENT_CLAIM_ID_MISMATCH")
    required = {ref.evidence_id for ref in claim.evidence_refs} | set(claim.checked_evidence_ids)
    for calculation_id in claim.calculation_ids:
        if calculation_id in records:
            required.update(ref.evidence_id for operand in records[calculation_id].operands
                            for ref in operand.evidence_refs)
    for evidence_id in required - read:
        issues.append(f"VERIFIER_DID_NOT_READ_DEPENDENCY:{evidence_id}")
    for evidence_id in set(assessment.evidence_ids) - read:
        issues.append(f"ASSESSMENT_EVIDENCE_NOT_READ:{evidence_id}")
    for evidence_id in read:
        if evidence_id not in registry:
            issues.append(f"READ_EVIDENCE_UNKNOWN:{evidence_id}")
        elif registry[evidence_id].document_id not in authorized:
            issues.append(f"DOCUMENT_NOT_AUTHORIZED:{registry[evidence_id].document_id}")
    evidence_hashes = {key: registry[key].binding_hash for key in sorted(read) if key in registry}
    calculation_hashes = {key: canonical_hash(records[key]) for key in claim.calculation_ids
                          if key in records}
    receipt_payload = {"claim": claim.revision_hash, "assessment": assessment.model_dump(),
                       "evidence": evidence_hashes, "calculations": calculation_hashes,
                       "read_ranges": ranges, "program_issues": sorted(set(issues)),
                       "prompt_hash": prompt_hash, "policy_version": policy_version}
    return VerificationReceipt(
        receipt_id=f"receipt-{canonical_hash(receipt_payload)[:32]}", claim_id=claim.claim_id,
        claim_revision=claim.revision, claim_revision_hash=claim.revision_hash,
        execution_status="succeeded", verdict=assessment.verdict, reason=assessment.reason,
        evidence_hashes=evidence_hashes, calculation_hashes=calculation_hashes,
        read_evidence_ids=sorted(read), read_ranges=ranges, program_issues=sorted(set(issues)),
        issue_fields=assessment.issue_fields, follow_up_queries=assessment.follow_up_queries,
        policy_version=policy_version, prompt_hash=prompt_hash,
        source_snapshot_hash=source_snapshot_hash(registry, authorized),
        verifier_model=verifier_model, created_at_utc=datetime.now(UTC).isoformat(),
    )


def validate_receipt(receipt: VerificationReceipt, claim: ChapterClaim,
                     units: Mapping[str, Any] | Sequence[Any],
                     authorized_document_ids: Sequence[str] | set[str],
                     calculations: Mapping[str, Any] | Sequence[Any] | None,
                     prompt_hash: str, policy_version: str = POLICY_VERSION) -> list[str]:
    registry, records = _unit_map(units), _calculation_map(calculations)
    issues = validate_claim(claim, registry, authorized_document_ids, records)
    issues.extend(_reading_issues(claim, registry, records, set(receipt.read_evidence_ids),
                                  receipt.read_ranges))
    issues.extend(receipt.program_issues)
    if not receipt.admitted:
        issues.append("RECEIPT_NOT_ADMITTED")
    if (receipt.claim_id != claim.claim_id or receipt.claim_revision != claim.revision
            or receipt.claim_revision_hash != claim.revision_hash):
        issues.append("RECEIPT_CLAIM_REVISION_MISMATCH")
    if receipt.policy_version != policy_version or receipt.prompt_hash != prompt_hash:
        issues.append("RECEIPT_POLICY_OR_PROMPT_CHANGED")
    if receipt.source_snapshot_hash != source_snapshot_hash(registry, authorized_document_ids):
        # Conservatively invalidate on newly added possible counterevidence too.
        issues.append("RECEIPT_SOURCE_SNAPSHOT_CHANGED")
    for key, digest in receipt.evidence_hashes.items():
        if key not in registry or registry[key].binding_hash != digest:
            issues.append(f"RECEIPT_EVIDENCE_CHANGED:{key}")
    for key, digest in receipt.calculation_hashes.items():
        if key not in records or canonical_hash(records[key]) != digest:
            issues.append(f"RECEIPT_CALCULATION_CHANGED:{key}")
    required = {ref.evidence_id for ref in claim.evidence_refs} | set(claim.checked_evidence_ids)
    for calculation_id in claim.calculation_ids:
        if calculation_id in records:
            required.update(ref.evidence_id for operand in records[calculation_id].operands
                            for ref in operand.evidence_refs)
    if not required <= set(receipt.evidence_hashes):
        issues.append("RECEIPT_DEPENDENCY_BINDING_MISSING")
    if set(receipt.read_evidence_ids) != set(receipt.evidence_hashes):
        issues.append("RECEIPT_READ_BINDING_SET_MISMATCH")
    if set(receipt.calculation_hashes) != set(claim.calculation_ids):
        issues.append("RECEIPT_CALCULATION_BINDING_MISSING")
    return sorted(set(issues))


def validate_publication(synthesis: SynthesisReport, claims: Mapping[str, ChapterClaim],
                         receipts: Mapping[str, VerificationReceipt],
                         units: Mapping[str, Any] | Sequence[Any],
                         authorized_document_ids: Sequence[str] | set[str],
                         calculations: Mapping[str, Any] | Sequence[Any] | None,
                         prompt_hash: str, policy_version: str = POLICY_VERSION) -> list[str]:
    """Fail closed on revisions, unverified paraphrases and unresolved new claims."""
    issues = []
    if synthesis.new_claims:
        issues.append("SYNTHESIS_NEW_CLAIMS_REQUIRE_VERIFICATION")
    if synthesis.conflicts:
        issues.append("SYNTHESIS_UNRESOLVED_CLAIM_CONFLICTS")
        registry, authorized = _unit_map(units), set(authorized_document_ids)
        for conflict in synthesis.conflicts:
            for claim_id in conflict.claim_ids:
                if claim_id not in claims:
                    issues.append(f"SYNTHESIS_CONFLICT_CLAIM_UNKNOWN:{claim_id}")
            for evidence_id in conflict.evidence_ids:
                unit = registry.get(evidence_id)
                if unit is None:
                    issues.append(f"SYNTHESIS_CONFLICT_EVIDENCE_UNKNOWN:{evidence_id}")
                elif unit.document_id not in authorized:
                    issues.append(f"DOCUMENT_NOT_AUTHORIZED:{unit.document_id}")
    for section_index, section in enumerate(synthesis.sections):
        for judgment_index, judgment in enumerate(section.judgments):
            prefix = f"REPORT:{section_index}:{judgment_index}"
            if len(judgment.claim_ids) != 1:
                issues.append(f"{prefix}:COMPOSITE_OR_NEW_JUDGMENT_REQUIRES_OWN_CLAIM")
            for claim_id in judgment.claim_ids:
                claim, receipt = claims.get(claim_id), receipts.get(claim_id)
                if claim is None:
                    issues.append(f"{prefix}:CLAIM_UNKNOWN:{claim_id}")
                    continue
                if judgment.text != claim.statement:
                    issues.append(f"{prefix}:UNVERIFIED_PARAPHRASE:{claim_id}")
                if receipt is None:
                    issues.append(f"{prefix}:RECEIPT_MISSING:{claim_id}")
                    continue
                issues.extend(f"{prefix}:{claim_id}:{issue}" for issue in validate_receipt(
                    receipt, claim, units, authorized_document_ids, calculations,
                    prompt_hash, policy_version))
    return sorted(set(issues))
