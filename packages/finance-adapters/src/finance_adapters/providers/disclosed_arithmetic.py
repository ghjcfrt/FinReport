"""Finite arithmetic checks on reconstructed, source-bound disclosure cells.

An arithmetic match is not financial comparability, source factual accuracy,
strict-fact admission, a verified rounding convention, or absence of anomalies.
This module deliberately does not modify the v1 extraction/semantic contracts.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_adapters.persistence.audit import stable_hash
from finance_adapters.providers.disclosed_relations import DisclosureSource, DisclosureSpan
from finance_adapters.providers.disclosure_facets import (
    DisclosureFacetObservation,
    FacetPeriodHeader,
    extract_disclosure_facets,
)

VERSION = "disclosed_arithmetic_v1"
FORMULA = "(current-comparative)/comparative*100"
_MAX_NUMERIC_CHARS = 256


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DisplayInterval(_Strict):
    displayed: Decimal
    quantum: Decimal
    lower_inclusive: Decimal
    upper_inclusive: Decimal


class ExactRateBound(_Strict):
    numerator: Decimal
    positive_denominator: Decimal
    approximate_percent: Decimal


class DisplayPrecisionCheck(_Strict):
    current: DisplayInterval
    comparative: DisplayInterval
    disclosed_percent: DisplayInterval
    calculated_lower: ExactRateBound
    calculated_upper: ExactRateBound
    intervals_overlap: bool
    arithmetic_precision: int
    comparison_method: Literal["exact_decimal_cross_products"] = "exact_decimal_cross_products"
    assumption: Literal["nearest_half_last_display_unit_inclusive_not_verified"] = (
        "nearest_half_last_display_unit_inclusive_not_verified"
    )
    boundary: str = (
        "假设金额和百分比各按显示末位四舍五入，使用末位半单位的闭区间保守检查；"
        "不认证公司实际舍入约定。商仅作近似展示，区间判定使用精确十进制交叉乘积。"
    )


class DisclosedArithmeticCheck(_Strict):
    version: Literal["disclosed_arithmetic_v1"] = VERSION
    id: str = Field(pattern=r"^arithmetic:[a-f0-9]{24}$")
    source_id: str
    document_sha256: str
    text_sha256: str
    page_no: int | None
    subject: DisclosureSpan
    row_span: DisclosureSpan
    unit_anchor: DisclosureSpan
    period_headers: tuple[FacetPeriodHeader, ...]
    current_amount: DisclosureSpan
    comparative_amount: DisclosureSpan
    disclosed_change: DisclosureSpan
    supporting_facet_ids: tuple[str, ...]
    formula: Literal["(current-comparative)/comparative*100"] = FORMULA
    status: Literal[
        "displayed_arithmetic_matches", "compatible_with_display_precision",
        "outside_display_precision", "unassessed_negative_comparative", "unassessed_zero_comparative",
    ]
    calculated_rate_percent_approx: Decimal | None
    displayed_formula_exactly_matches: bool | None
    precision_check: DisplayPrecisionCheck | None
    arithmetic_comparison_performed: bool
    company_disclosure_error_asserted: Literal[False] = False
    comparable_accounting_basis_verified: Literal[False] = False
    source_financial_truth_verified: Literal[False] = False
    formal_admitted: Literal[False] = False


class DisclosedArithmeticUnknown(_Strict):
    code: Literal["NO_SUPPORTED_ARITHMETIC_ROW", "NO_COMPARATIVE_COLUMN", "INCOMPLETE_ROW",
                  "AMBIGUOUS_ROW", "UNSUPPORTED_NUMERIC_PRECISION"]
    message: str
    subject: DisclosureSpan | None = None
    row_span: DisclosureSpan | None = None
    supporting_facet_ids: tuple[str, ...] = ()


class DisclosedArithmeticExtraction(_Strict):
    version: Literal["disclosed_arithmetic_v1"] = VERSION
    parent_source: DisclosureSource
    checks: tuple[DisclosedArithmeticCheck, ...]
    unknowns: tuple[DisclosedArithmeticUnknown, ...]
    boundary: str = (
        "仅核对同来源、同行、同单位和逐字期间表头的两期显示金额与显示变动率算术；"
        "不认证期间映射、会计可比性、公司实际舍入规则、原始事实正确性或公司没有财务异常。"
        "负基数与零基数不套用通用增长率；横杠不当零，四列表没有比较期时不倒推。"
        "未匹配语法不表示原文未披露；所有结果不进入严格事实或正式金融结论。"
    )


def _role_anchor(item: DisclosureFacetObservation, role: str) -> DisclosureSpan | None:
    anchors = [anchor.span for anchor in item.anchors if anchor.role == role]
    return anchors[0] if len(anchors) == 1 else None


def _row_identity(item: DisclosureFacetObservation) -> tuple:
    row, unit = _role_anchor(item, "row"), _role_anchor(item, "unit")
    return (item.source_id, item.document_sha256, item.text_sha256, item.page_no,
            item.subject.start, item.subject.end,
            None if row is None else (row.start, row.end, row.quote),
            None if unit is None else (unit.start, unit.end, unit.quote),
            tuple((h.role, h.span.start, h.span.end, h.span.quote) for h in item.period_headers))


def _interval(quote: str, *, percent: bool = False) -> DisplayInterval:
    raw = quote.removesuffix("%") if percent else quote
    numeric = Decimal(raw.replace(",", ""))
    quantum = Decimal(1).scaleb(numeric.as_tuple().exponent)
    half = quantum / Decimal(2)
    return DisplayInterval(displayed=numeric, quantum=quantum,
        lower_inclusive=numeric - half, upper_inclusive=numeric + half)


def _bounds(current: DisplayInterval, prior: DisplayInterval) -> tuple[tuple, tuple]:
    corners = [((amount - base) * Decimal(100), base)
               for amount in (current.lower_inclusive, current.upper_inclusive)
               for base in (prior.lower_inclusive, prior.upper_inclusive)]
    lower = upper = corners[0]
    for fraction in corners[1:]:
        if fraction[0] * lower[1] < lower[0] * fraction[1]:
            lower = fraction
        if fraction[0] * upper[1] > upper[0] * fraction[1]:
            upper = fraction
    return lower, upper


def _check_row(items: list[DisclosureFacetObservation]) -> DisclosedArithmeticCheck:
    lookup = {(item.kind, item.column_role): item for item in items}
    current, prior, change = (lookup[key] for key in
        (("value", "current_column"), ("value", "comparative_column"), ("change", "current_column")))
    anchors = [_role_anchor(item, role) for item, role in
               ((current, "value"), (prior, "value"), (change, "change"))]
    numeric_chars = sum(len(anchor.quote) for anchor in anchors)
    arithmetic_precision = max(80, numeric_chars * 4 + 32)
    # Do not inherit a caller's precision, exponent limits, rounding or Inexact
    # trap. Finite cross-products stay exact; explanatory repeating quotients
    # may round under this explicit, versioned display context.
    with localcontext(Context(prec=arithmetic_precision, rounding=ROUND_HALF_EVEN,
                             Emin=-999999, Emax=999999, clamp=0,
                             traps=[InvalidOperation, DivisionByZero, Overflow])):
        amounts = [_interval(anchors[0].quote), _interval(anchors[1].quote),
                   _interval(anchors[2].quote, percent=True)]
        a, b, p = (value.displayed for value in amounts)
        precision_check = rate = exact = None
        if b <= 0:
            status = "unassessed_negative_comparative" if b < 0 else "unassessed_zero_comparative"
        else:
            lower, upper = _bounds(amounts[0], amounts[1])
            overlaps = (lower[0] <= amounts[2].upper_inclusive * lower[1]
                        and upper[0] >= amounts[2].lower_inclusive * upper[1])
            exact = (a - b) * Decimal(100) == p * b
            status = ("displayed_arithmetic_matches" if exact else "compatible_with_display_precision"
                      if overlaps else "outside_display_precision")
            # Division is used for explanatory display only, never for a decision.
            with localcontext() as quotient_ctx:
                quotient_ctx.prec = max(60, numeric_chars + 12)
                rate = (a - b) * Decimal(100) / b
                shown_lower = ExactRateBound(numerator=lower[0], positive_denominator=lower[1],
                                             approximate_percent=lower[0] / lower[1])
                shown_upper = ExactRateBound(numerator=upper[0], positive_denominator=upper[1],
                                             approximate_percent=upper[0] / upper[1])
            precision_check = DisplayPrecisionCheck(current=amounts[0], comparative=amounts[1],
                disclosed_percent=amounts[2], calculated_lower=shown_lower, calculated_upper=shown_upper,
                intervals_overlap=overlaps, arithmetic_precision=arithmetic_precision)
    values = {"source_id": current.source_id, "document_sha256": current.document_sha256,
        "text_sha256": current.text_sha256, "page_no": current.page_no, "subject": current.subject,
        "row_span": _role_anchor(current, "row"), "unit_anchor": _role_anchor(current, "unit"),
        "period_headers": current.period_headers, "current_amount": anchors[0], "comparative_amount": anchors[1],
        "disclosed_change": anchors[2], "supporting_facet_ids": tuple(sorted(item.id for item in items)), "status": status,
        "calculated_rate_percent_approx": rate, "displayed_formula_exactly_matches": exact,
        "precision_check": precision_check, "arithmetic_comparison_performed": b > 0}
    encoded = {key: value.model_dump(mode="json") if isinstance(value, BaseModel) else
               [v.model_dump(mode="json") for v in value] if isinstance(value, tuple) and value and
               isinstance(value[0], BaseModel) else str(value) if isinstance(value, Decimal) else value
               for key, value in values.items()}
    return DisclosedArithmeticCheck(id="arithmetic:" + stable_hash(encoded)[:24], **values)


def check_disclosed_arithmetic(source: Mapping[str, Any] | DisclosureSource) -> DisclosedArithmeticExtraction:
    """Reconstruct from original text; never accept caller/model supplied facets."""
    extracted = extract_disclosure_facets(source)
    groups: dict[tuple, list[DisclosureFacetObservation]] = defaultdict(list)
    for item in extracted.observations:
        if item.kind in {"value", "change"}:
            groups[_row_identity(item)].append(item)
    checks, unknowns = [], []
    for items in groups.values():
        first = items[0]
        detail = {"subject": first.subject, "row_span": _role_anchor(first, "row"),
                  "supporting_facet_ids": tuple(sorted(item.id for item in items))}
        roles = {header.role for header in first.period_headers}
        required = {("value", "current_column"), ("value", "comparative_column"), ("change", "current_column")}
        keys = [(item.kind, item.column_role) for item in items]
        if not {"current_column", "comparative_column"} <= roles:
            code, message = "NO_COMPARATIVE_COLUMN", "原文没有可配对的比较期列；不从本期、累计金额或同比倒推比较期。"
        elif len(keys) != len(set(keys)):
            code, message = "AMBIGUOUS_ROW", "同行同列观察不唯一，不选择首项或跨行补齐。"
        elif not required <= set(keys) or any(_role_anchor(first, role) is None for role in ("row", "unit")):
            code, message = "INCOMPLETE_ROW", "同一行两期金额、变动率、单位或表头不完整；横杠不归零。"
        elif any(len(_role_anchor(item, item.kind).quote) > _MAX_NUMERIC_CHARS for item in items):
            code, message = "UNSUPPORTED_NUMERIC_PRECISION", "数值长度超出本版本256字符界限，保留未核验。"
        else:
            checks.append(_check_row(items))
            continue
        unknowns.append(DisclosedArithmeticUnknown(code=code, message=message, **detail))
    if not groups:
        unknowns.append(DisclosedArithmeticUnknown(code="NO_SUPPORTED_ARITHMETIC_ROW",
            message="本来源未匹配受支持的两期数值行；不表示原文未披露或金额为零。"))
    return DisclosedArithmeticExtraction(parent_source=extracted.parent_source,
        checks=tuple(sorted(checks, key=lambda item: (item.subject.start, item.id))), unknowns=tuple(unknowns))


def validate_disclosed_arithmetic(check: Mapping[str, Any] | DisclosedArithmeticCheck,
                                  source: Mapping[str, Any] | DisclosureSource) -> DisclosedArithmeticCheck:
    """Validate a saved observation by exact complete reconstruction from source."""
    candidate = check.model_dump(mode="json") if isinstance(check, BaseModel) else dict(check)
    for expected in check_disclosed_arithmetic(source).checks:
        if candidate == expected.model_dump(mode="json"):
            return expected
    raise ValueError("DISCLOSED_ARITHMETIC_NOT_PROGRAM_RECONSTRUCTED")
