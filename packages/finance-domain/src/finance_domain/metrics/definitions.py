from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, localcontext

from finance_domain.metrics.formulas import free_cash_flow


def is_financial_industry(code: str) -> bool:
    """Known financial sectors; unknown labels are not silently reclassified."""
    return code.strip().lower() in {
        "bank", "insurance", "broker", "finance", "j", "j66", "j67", "j68", "j69",
        "金融业", "货币金融服务", "资本市场服务", "保险业", "其他金融业",
    }


def industry_classification(code: str | None) -> str:
    """Classify declared codes without turning an unknown label into an approval."""
    if not isinstance(code, str):
        return "unknown"
    normalized = code.strip().lower()
    if is_financial_industry(normalized):
        return "financial"
    if (normalized in {"general_non_financial", "制造业", "manufacturing"}
            or re.fullmatch(r"[a-ik-s](?:\d{1,4})?", normalized)):
        return "non_financial"
    return "unknown"


NON_FINANCIAL_METRICS = frozenset({
    "gross_margin", "net_margin", "operating_cashflow_to_net_income", "free_cash_flow",
    "current_ratio", "debt_to_assets",
    "quick_ratio_proxy", "simple_average_roa", "simple_average_parent_roe",
    "inventory_turnover", "receivables_turnover", "asset_turnover",
    "ebit_expensed_interest_proxy", "expensed_interest_coverage_proxy", "net_debt_cash_proxy",
})


@dataclass(frozen=True)
class MetricFormula:
    name: str
    formula: str
    required_concepts: tuple[str, ...]
    unit: str
    operation: str
    beginning_concepts: tuple[str, ...] = ()

    @property
    def input_keys(self) -> tuple[str, ...]:
        return self.required_concepts + tuple("beginning_" + code for code in self.beginning_concepts)

    def evaluate(self, values: dict[str, Decimal]) -> Decimal:
        inputs = [values[code] for code in self.input_keys]
        if any(not value.is_finite() for value in inputs):
            raise ValueError("NON_FINITE_METRIC_INPUT")
        with localcontext() as context:
            context.prec = 48
            if self.operation == "identity":
                return inputs[0]
            if self.operation == "difference":
                return inputs[0] - inputs[1]
            if self.operation in {"ebit_expensed_interest_proxy", "expensed_interest_coverage_proxy"}:
                profit, interest = inputs
                if interest < 0:
                    raise ValueError("NEGATIVE_INTEREST_EXPENSE")
                if self.operation == "expensed_interest_coverage_proxy":
                    if interest <= 0:
                        raise ValueError("NON_POSITIVE_INTEREST_EXPENSE")
                    return (profit + interest) / interest
                return profit + interest
            if self.operation == "net_debt_cash_proxy":
                if any(value < 0 for value in inputs):
                    raise ValueError("NEGATIVE_BALANCE_INPUT")
                return sum(inputs[:-1], Decimal(0)) - inputs[-1]
            if self.operation == "free_cash_flow":
                value, issue = free_cash_flow(inputs[0], inputs[1])
                if issue:
                    raise ValueError(issue)
                return value
            if self.operation == "gross_margin":
                if inputs[0] <= 0:
                    raise ValueError("NON_POSITIVE_REVENUE")
                if inputs[1] < 0:
                    raise ValueError("NEGATIVE_OPERATING_COST")
                return (inputs[0] - inputs[1]) / inputs[0]
            if self.operation == "quick_ratio_proxy":
                assets, inventories, liabilities = inputs
                if min(assets, inventories) < 0 or assets < inventories:
                    raise ValueError("INVALID_QUICK_ASSET_PROXY_INPUT")
                if liabilities <= 0:
                    raise ValueError("NON_POSITIVE_DENOMINATOR")
                return (assets - inventories) / liabilities
            if self.operation in {"average_balance_ratio", "average_balance_turnover"}:
                numerator, closing, opening = inputs
                if min(closing, opening) < 0:
                    raise ValueError("NEGATIVE_BALANCE_INPUT")
                denominator = (closing + opening) / 2
                if denominator <= 0:
                    raise ValueError("NON_POSITIVE_DENOMINATOR")
                if self.operation == "average_balance_turnover" and numerator < 0:
                    raise ValueError("NEGATIVE_TURNOVER_FLOW")
                return numerator / denominator
            if self.operation in {"ratio", "nonnegative_ratio"}:
                if inputs[1] <= 0:
                    raise ValueError("NON_POSITIVE_DENOMINATOR")
                if self.operation == "nonnegative_ratio" and inputs[0] < 0:
                    raise ValueError("NEGATIVE_BALANCE_INPUT")
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
        "自由现金流（购建长期资产现金代理）", "cashflow_operations - capex_cash",
        ("cashflow_operations", "capex_cash"), "currency", "free_cash_flow"
    ),
    "current_ratio": MetricFormula(
        "流动比率", "total_current_assets / total_current_liabilities",
        ("total_current_assets", "total_current_liabilities"), "ratio", "nonnegative_ratio"
    ),
    "debt_to_assets": MetricFormula(
        "资产负债率", "total_liabilities / total_assets",
        ("total_liabilities", "total_assets"), "ratio", "nonnegative_ratio"
    ),
    "quick_ratio_proxy": MetricFormula(
        "速动比率代理（流动资产减存货，未扣预付款）",
        "(total_current_assets - inventories) / total_current_liabilities",
        ("total_current_assets", "inventories", "total_current_liabilities"), "ratio", "quick_ratio_proxy"
    ),
    "simple_average_roa": MetricFormula(
        "总资产净利率（合并净利润/期初末简单平均总资产）",
        "net_income / ((total_assets + beginning_total_assets) / 2)",
        ("net_income", "total_assets"), "ratio", "average_balance_ratio", ("total_assets",)
    ),
    "simple_average_parent_roe": MetricFormula(
        "归母净资产收益率（期初末简单平均，非披露加权ROE）",
        "net_income_attributable / ((equity_attributable + beginning_equity_attributable) / 2)",
        ("net_income_attributable", "equity_attributable"), "ratio", "average_balance_ratio", ("equity_attributable",)
    ),
    "inventory_turnover": MetricFormula(
        "存货周转率（营业成本/期初末简单平均存货）",
        "operating_cost / ((inventories + beginning_inventories) / 2)",
        ("operating_cost", "inventories"), "ratio", "average_balance_turnover", ("inventories",)
    ),
    "receivables_turnover": MetricFormula(
        "应收账款周转率代理（营业收入/期初末平均应收账款净额，非赊销口径）",
        "revenue / ((accounts_receivable + beginning_accounts_receivable) / 2)",
        ("revenue", "accounts_receivable"), "ratio", "average_balance_turnover", ("accounts_receivable",)
    ),
    "asset_turnover": MetricFormula(
        "总资产周转率（营业收入/期初末简单平均总资产）",
        "revenue / ((total_assets + beginning_total_assets) / 2)",
        ("revenue", "total_assets"), "ratio", "average_balance_turnover", ("total_assets",)
    ),
    "ebit_expensed_interest_proxy": MetricFormula(
        "EBIT代理（利润总额加费用化利息，未含资本化利息）",
        "profit_before_tax + interest_expense",
        ("profit_before_tax", "interest_expense"), "currency", "ebit_expensed_interest_proxy"
    ),
    "expensed_interest_coverage_proxy": MetricFormula(
        "费用化利息保障倍数代理（非现金或全部利息覆盖）",
        "(profit_before_tax + interest_expense) / interest_expense",
        ("profit_before_tax", "interest_expense"), "ratio", "expensed_interest_coverage_proxy"
    ),
    "net_debt_cash_proxy": MetricFormula(
        "净负债代理（指定五项债务减货币资金，含受限资金抵减）",
        "short_term_borrowings + current_portion_noncurrent_liabilities + long_term_borrowings + bonds_payable + lease_liabilities - monetary_funds",
        ("short_term_borrowings", "current_portion_noncurrent_liabilities", "long_term_borrowings", "bonds_payable", "lease_liabilities", "monetary_funds"),
        "currency", "net_debt_cash_proxy"
    ),
}
