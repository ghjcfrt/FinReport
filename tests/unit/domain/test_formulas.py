from decimal import Decimal

from finance_domain.metrics.formulas import free_cash_flow, gross_margin, ratio


def test_domain_formulas_are_framework_free():
    assert gross_margin(Decimal(100), Decimal(60))[0] == Decimal("0.4")
    assert ratio(Decimal(20), Decimal(0)) == (None, "NON_POSITIVE_DENOMINATOR")
    assert free_cash_flow(Decimal(80), Decimal(30)) == (Decimal(50), "")
