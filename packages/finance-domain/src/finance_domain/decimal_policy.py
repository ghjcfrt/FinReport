"""An explicit, fresh Decimal context for reproducible financial calculations."""
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    Underflow,
)

FINANCIAL_MIN_EXPONENT = -999999
FINANCIAL_MAX_EXPONENT = 999999


def financial_context(precision: int = 48) -> Context:
    """Keep all policy fields independent of thread and mutable default contexts.

    Repeating ratios retain the registered 48-significant-digit policy. Invalid,
    overflowing and underflowing arithmetic must fail rather than become a
    non-finite result or an inexact zero.
    """
    if type(precision) is not int or precision < 1:
        raise ValueError("INVALID_FINANCIAL_PRECISION")
    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=FINANCIAL_MIN_EXPONENT,
        Emax=FINANCIAL_MAX_EXPONENT,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow, Underflow],
    )
