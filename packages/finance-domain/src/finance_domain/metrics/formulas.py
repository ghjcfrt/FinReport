from __future__ import annotations

from decimal import Decimal


def ratio(numerator: Decimal | None, denominator: Decimal | None) -> tuple[Decimal | None, str]:
    if numerator is None or denominator is None:
        return None, "MISSING_INPUT"
    if denominator <= 0:
        return None, "NON_POSITIVE_DENOMINATOR"
    return numerator / denominator, ""


def gross_margin(revenue: Decimal | None, cost: Decimal | None) -> tuple[Decimal | None, str]:
    if revenue is None or cost is None:
        return None, "MISSING_INPUT"
    if revenue <= 0:
        return None, "NON_POSITIVE_REVENUE"
    return (revenue - cost) / revenue, ""


def free_cash_flow(cfo: Decimal | None, capex: Decimal | None) -> tuple[Decimal | None, str]:
    if cfo is None or capex is None:
        return None, "MISSING_INPUT"
    if capex < 0:
        return None, "NEGATIVE_CAPEX_CASH"
    return cfo - capex, ""
