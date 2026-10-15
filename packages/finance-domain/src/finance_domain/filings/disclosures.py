"""Typed annual-report disclosures, distinct from audited primary statements."""
from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# An explicit shareholder label is necessary for post-nonrecurring profit.
DISCLOSURE_DEFINITIONS = {
    "net_income_attributable_excl_nonrecurring": {
        "name": "扣非归母净利润（公司披露）", "unit": "currency", "scope": "listed_company_shareholders",
        "labels": ("归属于上市公司股东的扣除非经常性损益的净利润", "归属于上市公司股东的扣除非经常性损益后的净利润"),
        "section": "主要会计数据和财务指标",
    },
    "basic_eps_disclosed": {"name": "基本每股收益（公司披露）", "unit": "currency_per_share",
        "scope": "listed_company_shareholders", "labels": ("基本每股收益",), "section": "主要会计数据和财务指标"},
    "diluted_eps_disclosed": {"name": "稀释每股收益（公司披露）", "unit": "currency_per_share",
        "scope": "listed_company_shareholders", "labels": ("稀释每股收益",), "section": "主要会计数据和财务指标"},
    "weighted_roe_disclosed": {"name": "加权平均净资产收益率（公司披露）", "unit": "ratio",
        "scope": "listed_company_shareholders", "labels": ("加权平均净资产收益率",), "section": "主要会计数据和财务指标"},
    "rd_investment_disclosed": {"name": "研发投入金额（公司披露）", "unit": "currency",
        "scope": "reporting_company_disclosed", "labels": ("研发投入金额",), "section": "公司研发投入情况"},
    "rd_capitalized_disclosed": {"name": "研发投入资本化金额（公司披露）", "unit": "currency",
        "scope": "reporting_company_disclosed", "labels": ("研发投入资本化的金额",), "section": "公司研发投入情况"},
    "rd_capitalization_ratio_disclosed": {"name": "研发投入资本化率（公司披露）", "unit": "ratio",
        "scope": "reporting_company_disclosed", "labels": ("资本化研发投入占研发投入的比例", "研发投入资本化率"),
        "section": "公司研发投入情况"},
}


def check_disclosure_currency(source: str, *, unit_context: bool = False) -> None:
    import re

    foreign = r"美元|港元|港币|欧元|日元|英镑|USD|HKD|EUR|JPY|GBP"
    text = re.sub(r"\s+", "", source)
    pattern = foreign if unit_context else r"(?:币种|单位|货币|本位币)[^。；;]{0,20}(?:" + foreign + r")"
    if re.search(pattern, text, re.IGNORECASE):
        raise ValueError("DISCLOSURE_CURRENCY_DECLARATION_CONFLICT")


def normalize_disclosure_value(raw: str, unit: str, *, unit_scale: int = 1) -> Decimal:
    """Keep ratios and per-share amounts out of monetary scale conversion."""
    import re

    cleaned = re.sub(r"\s+", "", raw).replace("，", ",").replace("（", "(").replace("）", ")")
    is_percent = cleaned.endswith(("%", "％"))
    if unit == "ratio":
        if not is_percent or unit_scale != 1:
            raise ValueError("DISCLOSURE_EXPLICIT_PERCENT_REQUIRED")
        cleaned = cleaned[:-1]
    elif is_percent or unit not in {"currency", "currency_per_share"}:
        raise ValueError("DISCLOSURE_UNIT_MISMATCH")
    number = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    if not re.fullmatch(rf"(?:[+-]?{number}|\({number}\))", cleaned):
        raise ValueError("DISCLOSURE_NUMERIC_VALUE_REQUIRED")
    negative = cleaned.startswith("(")
    value = Decimal((cleaned[1:-1] if negative else cleaned).replace(",", ""))
    if negative:
        value = value.copy_negate()
    if unit_scale not in {1, 1000, 10000, 1000000, 100000000} or (unit == "currency_per_share" and unit_scale != 1):
        raise ValueError("DISCLOSURE_UNIT_SCALE_INVALID")
    with localcontext() as context:
        context.prec = max(48, len(value.as_tuple().digits) + 12)
        return value / 100 if unit == "ratio" else value * unit_scale


class DisclosureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer_id: str = Field(min_length=1, max_length=128)
    decision: Literal["confirmed", "rejected"]
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reviewer_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("NONBLANK_TEXT_REQUIRED")
        return value.strip()


class OcrDisclosureMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    table_id: int = Field(gt=0)
    ocr_call_id: int = Field(gt=0)
    page_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    response_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=1)
    raw_label: str
    raw_value: str
    corrected_raw_value: str | None = None
    column_label: str
    unit_text: str
    unit_ocr_call_id: int | None = Field(default=None, gt=0)
    unit_page_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    unit_response_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    unit_scale: int = 1
    currency: Literal["CNY"] = "CNY"
    value_bbox: list[float] = Field(min_length=4, max_length=4)
    row_bbox: list[float] = Field(min_length=4, max_length=4)
    unit_bbox: list[float] = Field(min_length=4, max_length=4)
    reviewer_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reviewer_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("NONBLANK_TEXT_REQUIRED")
        return value.strip()
