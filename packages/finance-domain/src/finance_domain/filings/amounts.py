"""Convert disclosed monetary amounts without inheriting thread precision."""
from decimal import Decimal, localcontext


def normalize_monetary_value(value: Decimal, unit_scale: int) -> Decimal:
    if not value.is_finite() or unit_scale not in {1, 1000, 10000, 1000000, 100000000}:
        raise ValueError("FACT_UNIT_OR_VALUE_INVALID")
    with localcontext() as context:
        context.prec = 48
        return value * unit_scale
