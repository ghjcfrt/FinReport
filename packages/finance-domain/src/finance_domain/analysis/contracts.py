from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewRequest(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=128)
    decision: Literal["confirmed", "corrected", "rejected"]
    reason: str = Field(min_length=1, max_length=2000)
    corrected_value: Decimal | None = None


class MetricRequest(BaseModel):
    strict: bool = True


class MetricObservation(BaseModel):
    """Model-selected metric facts; numerical prose is rendered by the server."""

    model_config = ConfigDict(extra="forbid")
    metric_result_id: int
    value: Decimal = Field(allow_inf_nan=False)
    unit: Literal["currency", "ratio"]
    scope: Literal["consolidated", "parent"]
    period_start: date
    period_end: date
    input_fact_ids: list[int]


class AnalysisScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: date
    period_end: date
    scope: Literal["consolidated", "parent"]
    period_basis: Literal["annual", "semi_annual"]
    currency: str


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=64)
    claim_class: Literal[
        "fact_restatement",
        "calculated_observation",
        "evidence_supported_inference",
        "review_required_hypothesis",
    ]
    content: str
    scope: str
    rationale: str
    metric_result_ids: list[int]
    evidence_fragment_ids: list[int]
    evidence_relation: Literal["direct", "inferred"]
    review_required: bool
    observations: list[MetricObservation] = Field(default_factory=list)


class AnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_scope: AnalysisScope
    overall_conclusion: str
    qualitative_summary: str
    overall_review_required: Literal[True]
    claims: list[Claim]
    limitations: list[str]
    report_sections: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class ComparisonRequest(BaseModel):
    company_id: int
    metric_code: str
    peer_filing_ids: list[int]
    peer_universe_version: str
    coverage: Decimal = Field(ge=0, le=1)


class ModelSettingsUpdate(BaseModel):
    provider: Literal["openai", "anthropic"]
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(min_length=1, max_length=200)


class ModelDiscoveryRequest(BaseModel):
    provider: Literal["openai", "anthropic"]
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
