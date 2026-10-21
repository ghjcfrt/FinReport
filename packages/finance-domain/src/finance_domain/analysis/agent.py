"""Public, evidence-linked results of the controlled financial-analysis loop."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

AnalysisTopic = Literal[
    "non_recurring_items", "accounting_basis_changes", "profit_cashflow_relationship",
    "performance_changes",
]


class EvidenceItemAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=160)
    status: Literal["discussed", "insufficient_evidence"]
    reason: str = Field(min_length=12, max_length=1200)


class WorkflowFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    topic: AnalysisTopic
    evidence_kind: Literal[
        "program_fact", "company_disclosure", "analysis_inference", "evidence_insufficient",
    ]
    assessment: Literal[
        "signal_found", "not_identified_in_checked_scope", "insufficient_evidence", "not_applicable",
        "observation",
    ]
    statement: str = Field(min_length=1, max_length=6000)
    rationale: str = Field(min_length=1, max_length=6000)
    source_ids: list[str] = Field(default_factory=list)
    metric_result_ids: list[int] = Field(default_factory=list)
    calculation_ids: list[str] = Field(default_factory=list)
    # v11+ findings may be tied to one atomic evidence obligation.  The field
    # is optional so saved v2-v11 runs without subtopic metadata still replay.
    obligation_id: str | None = None
    evidence_item_assessments: list[EvidenceItemAssessment] | None = None
    review_required: bool = True

    @model_serializer(mode="wrap")
    def _serialize_optional_coverage(self, handler):
        value = handler(self)
        if value.get("evidence_item_assessments") is None:
            value.pop("evidence_item_assessments", None)
        return value

    def model_dump(self, *args, **kwargs):
        """Do not add a null v11 field to legacy serialized findings."""
        value = super().model_dump(*args, **kwargs)
        if value.get("obligation_id") is None:
            value.pop("obligation_id", None)
        return value


class MaterialRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    topic: AnalysisTopic
    requirement_kind: Literal["document", "review", "disclosure"]
    report_type: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    concept_codes: list[str] = Field(default_factory=list)
    purpose: str = Field(min_length=1, max_length=2000)
    acceptable_alternatives: list[str] = Field(default_factory=list)
    status: Literal["missing", "needs_review", "provided"] = "missing"
    matching_document_ids: list[int] = Field(default_factory=list)


class TopicCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: AnalysisTopic
    status: Literal["checked", "signal_found", "insufficient_evidence", "needs_documents"]
    source_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    searched_queries: list[str] = Field(default_factory=list)
    coverage_note: str
    # Added after the topic-only protocol.  Older persisted checks omit it and
    # are intentionally accepted as legacy topic coverage.
    obligations: list[dict] = Field(default_factory=list)


class AgentWorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    status: Literal["completed", "partial_result", "waiting_for_documents"]
    stop_reason: str
    document_snapshot: list[dict] = Field(default_factory=list)
    document_snapshot_hash: str
    sources: list[dict] = Field(default_factory=list)
    calculations: list[dict] = Field(default_factory=list)
    findings: list[WorkflowFinding] = Field(default_factory=list)
    mandatory_checks: list[TopicCheck] = Field(default_factory=list)
    material_requirements: list[MaterialRequirement] = Field(default_factory=list)
    completed_rounds: int = 0
    checkpoint_id: str
    resumed_from_checkpoint: str | None = None
    limitations: list[str] = Field(default_factory=list)
    coverage_policy: str | None = None
    evidence_coverage: dict | None = None
    obligation_tasks: list[dict] | None = None
    # Frozen only for new runs.  Absence keeps historical replay semantics.
    semantic_policy: str | None = None
    source_unit_admission_policy: str | None = None

    @model_serializer(mode="wrap")
    def _serialize_optional_coverage(self, handler):
        value = handler(self)
        if value.get("evidence_coverage") is None:
            value.pop("evidence_coverage", None)
        if value.get("obligation_tasks") is None:
            value.pop("obligation_tasks", None)
        if value.get("semantic_policy") is None:
            value.pop("semantic_policy", None)
        if value.get("source_unit_admission_policy") is None:
            value.pop("source_unit_admission_policy", None)
        return value
