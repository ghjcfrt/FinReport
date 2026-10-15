"""Deterministic changes: amounts use positive-base YoY; ratios use points."""
from __future__ import annotations

from decimal import Decimal, localcontext


def period_change(current: Decimal, prior: Decimal, unit: str) -> dict:
    if not current.is_finite() or not prior.is_finite():
        raise ValueError("NON_FINITE_PERIOD_VALUE")
    with localcontext() as context:
        context.prec = 48
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
        elif unit == "ratio":
            result.update(status="calculated", explanation_code=None, change_unit="ratio_points")
        else:
            raise ValueError("UNSUPPORTED_PERIOD_UNIT")
        return result
