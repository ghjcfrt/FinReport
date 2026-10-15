from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

PositiveId = Annotated[int, Field(strict=True, gt=0)]
NonBlankText = Annotated[str, Field(min_length=1)]


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_method: Literal["pdf_text", "ocr"] = "pdf_text"


ImageCoordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
ImageBox = Annotated[list[ImageCoordinate], Field(min_length=4, max_length=4)]


class OcrFactMappingRequest(BaseModel):
    """A human transcription from a located OCR table cell, never an approval."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    table_id: PositiveId
    ocr_call_id: PositiveId
    page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_index: int = Field(ge=0, strict=True)
    column_index: int = Field(ge=1, strict=True)
    concept_code: str = Field(min_length=1, max_length=96)
    raw_label: str = Field(min_length=1, max_length=256)
    raw_value: str = Field(min_length=1, max_length=256)
    corrected_raw_value: str | None = Field(default=None, min_length=1, max_length=256)
    unit_scale: Literal[1, 1000, 10000, 1000000, 100000000]
    currency: Literal["CNY"]
    period_role: Literal["current", "comparative"]
    period_start: date
    period_end: date
    scope: Literal["consolidated"]
    accounting_standard: Literal["PRC_ASBE"]
    is_restatement: StrictBool = False
    table_name: str = Field(min_length=1, max_length=256)
    column_label: str = Field(min_length=1, max_length=256)
    value_bbox: ImageBox
    row_bbox: ImageBox
    unit_text: str = Field(min_length=1, max_length=256)
    unit_bbox: ImageBox
    unit_ocr_call_id: PositiveId | None = None
    unit_page_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    unit_response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewer_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


class OcrAuditConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    ocr_call_id: PositiveId
    page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote: str = Field(min_length=20, max_length=12000)
    bbox: ImageBox
    opinion: Literal["standard_unqualified"]
    reviewer_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


class OcrMappingCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    job_id: PositiveId
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


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
    metric_result_id: PositiveId
    value: Decimal = Field(allow_inf_nan=False)
    unit: Literal["currency", "ratio"]
    scope: Literal["consolidated", "parent"]
    period_start: date
    period_end: date
    input_fact_ids: list[PositiveId] = Field(min_length=1)
    # Computable statements are selected structurally and evaluated by the server.
    predicate: Literal["positive", "negative", "zero", "nonnegative", "nonpositive"] | None = None

    @field_validator("input_fact_ids")
    @classmethod
    def unique_inputs(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("DUPLICATE_FACT_REFERENCE")
        return value


class AnalysisScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: date
    period_end: date
    scope: Literal["consolidated", "parent"]
    period_basis: Literal["annual", "semi_annual"]
    currency: str


class SourceAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    selected_answer: str | None = None


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
    scope: Literal["consolidated", "parent"]
    rationale: str
    metric_result_ids: list[PositiveId] = Field(min_length=1)
    evidence_fragment_ids: list[PositiveId] = Field(min_length=1)
    evidence_relation: Literal["direct", "inferred"]
    review_required: StrictBool
    observations: list[MetricObservation] = Field(min_length=1)
    source_assertions: list[SourceAssertion] = Field(default_factory=list)

    @field_validator("metric_result_ids", "evidence_fragment_ids")
    @classmethod
    def unique_references(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("DUPLICATE_REFERENCE")
        return value

    @field_validator("type")
    @classmethod
    def nonblank_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("NONBLANK_CLAIM_TYPE_REQUIRED")
        return value


class AnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_scope: AnalysisScope
    overall_conclusion: str
    qualitative_summary: str
    overall_review_required: Literal[True]
    claims: list[Claim]
    limitations: list[NonBlankText]
    report_sections: list[NonBlankText] = Field(min_length=1)

    @field_validator("overall_review_required", mode="before")
    @classmethod
    def review_flag_is_boolean(cls, value):
        if value is not True:
            raise ValueError("OVERALL_REVIEW_REQUIRED_MUST_BE_TRUE_BOOLEAN")
        return value

    @field_validator("limitations", "report_sections")
    @classmethod
    def nonblank_items(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("NONBLANK_TEXT_REQUIRED")
        return value


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
