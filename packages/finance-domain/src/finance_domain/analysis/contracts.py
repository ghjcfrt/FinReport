from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=128)
    decision: Literal["confirmed", "corrected", "rejected"]
    reason: str = Field(min_length=1, max_length=2000)
    corrected_value: Decimal | None = None


class MetricRequest(BaseModel):
    strict: bool = True


class Claim(BaseModel):
    type: str
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


class AnalysisOutput(BaseModel):
    analysis_scope: dict[str, Any]
    overall_conclusion: str
    claims: list[Claim]
    limitations: list[str]
    report_sections: list[str]


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
