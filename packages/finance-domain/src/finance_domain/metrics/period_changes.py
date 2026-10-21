"""Deterministic changes: amounts use positive-base YoY; ratios use points."""
from __future__ import annotations

from decimal import Decimal, DecimalException, localcontext

from finance_domain.decimal_policy import financial_context


def period_change(current: Decimal, prior: Decimal, unit: str) -> dict:
    if not isinstance(current, Decimal) or not isinstance(prior, Decimal):
        raise ValueError("INVALID_PERIOD_VALUE")  # noqa: TRY004 - public domain error contract
    if not current.is_finite() or not prior.is_finite():
        raise ValueError("NON_FINITE_PERIOD_VALUE")
    if not isinstance(unit, str) or unit not in {"currency", "ratio"}:
        raise ValueError("UNSUPPORTED_PERIOD_UNIT")
    try:
        with localcontext(financial_context()):
            delta = current - prior
            result = {"current_value": str(current), "prior_value": str(prior),
                      "absolute_change": str(delta), "relative_change": None,
                      "percentage_point_change": None,
                      "direction": "increase" if delta > 0 else "decrease" if delta < 0 else "unchanged"}
            if unit == "currency":
                result["change_unit"] = "CNY"
                if prior <= 0:
                    result.update(status="not_comparable", explanation_code="NON_POSITIVE_BASE",
                                  direction="turnaround" if prior < 0 < current else result["direction"])
                else:
                    result.update(status="calculated", explanation_code=None,
                                  relative_change=str(delta / prior))
            else:
                result.update(status="calculated", explanation_code=None, change_unit="ratio_points")
            return result
    except DecimalException as exc:
        raise ValueError("PERIOD_ARITHMETIC_OUT_OF_RANGE") from exc
