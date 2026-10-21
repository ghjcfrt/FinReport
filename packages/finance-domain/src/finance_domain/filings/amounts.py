"""Convert disclosed monetary amounts exactly, independent of thread context."""
from decimal import Decimal

from finance_domain.decimal_policy import FINANCIAL_MAX_EXPONENT, FINANCIAL_MIN_EXPONENT


def normalize_monetary_value(value: Decimal, unit_scale: int) -> Decimal:
    shifts = {1: 0, 1000: 3, 10000: 4, 1000000: 6, 100000000: 8}
    if (not isinstance(value, Decimal) or not value.is_finite()
            or type(unit_scale) is not int or unit_scale not in shifts):
        raise ValueError("FACT_UNIT_OR_VALUE_INVALID")
    sign, digits, exponent = value.as_tuple()
    normalized_exponent = exponent + shifts[unit_scale]
    adjusted_exponent = normalized_exponent + len(digits) - 1
    if (not FINANCIAL_MIN_EXPONENT <= normalized_exponent <= FINANCIAL_MAX_EXPONENT
            or adjusted_exponent > FINANCIAL_MAX_EXPONENT):
        raise ValueError("FACT_UNIT_OR_VALUE_INVALID")
    # Multiplication would round amounts longer than the ratio precision. A
    # power-of-ten unit conversion only shifts the exact coefficient exponent.
    return Decimal((sign, digits, normalized_exponent))
