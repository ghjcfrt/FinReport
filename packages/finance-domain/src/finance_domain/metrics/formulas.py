from __future__ import annotations

from decimal import Decimal, localcontext


def ratio(numerator: Decimal | None, denominator: Decimal | None) -> tuple[Decimal | None, str]:
    if numerator is None or denominator is None:
        return None, "MISSING_INPUT"
    if not numerator.is_finite() or not denominator.is_finite():
        return None, "NON_FINITE_METRIC_INPUT"
    if denominator <= 0:
        return None, "NON_POSITIVE_DENOMINATOR"
    with localcontext() as context:
        context.prec = 48
        return numerator / denominator, ""


def gross_margin(revenue: Decimal | None, cost: Decimal | None) -> tuple[Decimal | None, str]:
    if revenue is None or cost is None:
        return None, "MISSING_INPUT"
    if not revenue.is_finite() or not cost.is_finite():
        return None, "NON_FINITE_METRIC_INPUT"
    if revenue <= 0:
        return None, "NON_POSITIVE_REVENUE"
    if cost < 0:
        return None, "NEGATIVE_OPERATING_COST"
    with localcontext() as context:
        context.prec = 48
        return (revenue - cost) / revenue, ""


def free_cash_flow(cfo: Decimal | None, capex: Decimal | None) -> tuple[Decimal | None, str]:
    if cfo is None or capex is None:
        return None, "MISSING_INPUT"
    if not cfo.is_finite() or not capex.is_finite():
        return None, "NON_FINITE_METRIC_INPUT"
    if capex < 0:
        return None, "NEGATIVE_CAPEX_CASH"
    with localcontext() as context:
        context.prec = 48
        return cfo - capex, ""
