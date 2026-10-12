from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class MetricFormula:
    name: str
    formula: str
    required_concepts: tuple[str, ...]
    unit: str
    operation: str

    def evaluate(self, values: dict[str, Decimal]) -> Decimal:
        inputs = [values[code] for code in self.required_concepts]
        if self.operation == "identity":
            return inputs[0]
        if self.operation == "difference":
            return inputs[0] - inputs[1]
        if self.operation == "gross_margin":
            if inputs[0] <= 0:
                raise ValueError("NON_POSITIVE_REVENUE")
            return (inputs[0] - inputs[1]) / inputs[0]
        if self.operation == "ratio":
            if inputs[1] <= 0:
                raise ValueError("NON_POSITIVE_DENOMINATOR")
            return inputs[0] / inputs[1]
        raise ValueError("UNKNOWN_METRIC_OPERATION")


# These definitions are also the contract supplied to the analysis model. In
# particular, cashflow/profit uses consolidated profit, while net margin uses
# profit attributable to the parent; those inputs must never be interchanged.
METRIC_DEFINITIONS = {
    "revenue": MetricFormula("营业收入", "revenue", ("revenue",), "currency", "identity"),
    "operating_profit": MetricFormula(
        "营业利润", "operating_profit", ("operating_profit",), "currency", "identity"
    ),
    "net_income_attributable": MetricFormula(
        "归母净利润", "net_income_attributable", ("net_income_attributable",), "currency", "identity"
    ),
    "cashflow_operations": MetricFormula(
        "经营活动现金流净额", "cashflow_operations", ("cashflow_operations",), "currency", "identity"
    ),
    "gross_margin": MetricFormula(
        "毛利率", "(revenue - operating_cost) / revenue",
        ("revenue", "operating_cost"), "ratio", "gross_margin"
    ),
    "net_margin": MetricFormula(
        "归母净利润率", "net_income_attributable / revenue",
        ("net_income_attributable", "revenue"), "ratio", "ratio"
    ),
    "operating_cashflow_to_net_income": MetricFormula(
        "经营现金流与合并净利润比率", "cashflow_operations / net_income",
        ("cashflow_operations", "net_income"), "ratio", "ratio"
    ),
    "free_cash_flow": MetricFormula(
        "自由现金流", "cashflow_operations - capex_cash",
        ("cashflow_operations", "capex_cash"), "currency", "difference"
    ),
}
