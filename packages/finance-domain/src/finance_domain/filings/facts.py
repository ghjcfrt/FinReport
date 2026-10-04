from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class FactInput:
    """ORM-free input used by formulas and validation rules."""

    concept_code: str
    value: Decimal
    period_start: date
    period_end: date
    scope: str = "consolidated"
    currency: str = "CNY"
    quality_status: str = "candidate"
    flow_or_stock: str = "flow"

    @property
    def is_strict(self) -> bool:
        return self.quality_status == "facts_verified"
