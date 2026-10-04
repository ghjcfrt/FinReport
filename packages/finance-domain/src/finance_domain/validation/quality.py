from __future__ import annotations

from collections.abc import Iterable


def has_independent_double_review(reviewer_ids: Iterable[str]) -> bool:
    ids = {item for item in reviewer_ids if item}
    return len(ids) >= 2


def balance_sheet_balances(assets, liabilities, equity) -> bool:
    return (
        assets is not None
        and liabilities is not None
        and equity is not None
        and assets == liabilities + equity
    )
