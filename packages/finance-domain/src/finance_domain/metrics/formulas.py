from __future__ import annotations

from decimal import Decimal, DecimalException, localcontext

from finance_domain.decimal_policy import financial_context


def ratio(numerator: Decimal | None, denominator: Decimal | None) -> tuple[Decimal | None, str]:
    if numerator is None or denominator is None:
        return None, "MISSING_INPUT"
    if not isinstance(numerator, Decimal) or not isinstance(denominator, Decimal):
        return None, "INVALID_METRIC_INPUT"
    if not numerator.is_finite() or not denominator.is_finite():
        return None, "NON_FINITE_METRIC_INPUT"
    if denominator <= 0:
        return None, "NON_POSITIVE_DENOMINATOR"
    try:
        with localcontext(financial_context()):
            return numerator / denominator, ""
    except DecimalException:
        return None, "METRIC_ARITHMETIC_OUT_OF_RANGE"


def gross_margin(revenue: Decimal | None, cost: Decimal | None) -> tuple[Decimal | None, str]:
    if revenue is None or cost is None:
        return None, "MISSING_INPUT"
    if not isinstance(revenue, Decimal) or not isinstance(cost, Decimal):
        return None, "INVALID_METRIC_INPUT"
    if not revenue.is_finite() or not cost.is_finite():
        return None, "NON_FINITE_METRIC_INPUT"
    if revenue <= 0:
        return None, "NON_POSITIVE_REVENUE"
    if cost < 0:
        return None, "NEGATIVE_OPERATING_COST"
    try:
        with localcontext(financial_context()):
            return (revenue - cost) / revenue, ""
    except DecimalException:
        return None, "METRIC_ARITHMETIC_OUT_OF_RANGE"


def free_cash_flow(cfo: Decimal | None, capex: Decimal | None) -> tuple[Decimal | None, str]:
    if cfo is None or capex is None:
        return None, "MISSING_INPUT"
    if not isinstance(cfo, Decimal) or not isinstance(capex, Decimal):
        return None, "INVALID_METRIC_INPUT"
    if not cfo.is_finite() or not capex.is_finite():
        return None, "NON_FINITE_METRIC_INPUT"
    if capex < 0:
        return None, "NEGATIVE_CAPEX_CASH"
    try:
        with localcontext(financial_context()):
            return cfo - capex, ""
    except DecimalException:
        return None, "METRIC_ARITHMETIC_OUT_OF_RANGE"
