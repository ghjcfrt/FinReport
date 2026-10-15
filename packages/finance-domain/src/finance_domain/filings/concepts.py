"""Canonical accounting metadata; printed indentation does not define these types."""
from __future__ import annotations

CONCEPT_LABELS = {
    "营业收入": ("revenue", "利润表", "flow"),
    "营业成本": ("operating_cost", "利润表", "flow"),
    "营业利润": ("operating_profit", "利润表", "flow"),
    "利润总额": ("profit_before_tax", "利润表", "flow"),
    "利息费用": ("interest_expense", "利润表", "flow"),
    "净利润": ("net_income", "利润表", "flow"),
    "归属于母公司所有者的净利润": ("net_income_attributable", "利润表", "flow"),
    "归属于上市公司股东的净利润": ("net_income_attributable", "利润表", "flow"),
    "资产总计": ("total_assets", "资产负债表", "stock"),
    "流动资产合计": ("total_current_assets", "资产负债表", "stock"),
    "存货": ("inventories", "资产负债表", "stock"),
    "应收账款": ("accounts_receivable", "资产负债表", "stock"),
    "货币资金": ("monetary_funds", "资产负债表", "stock"),
    "短期借款": ("short_term_borrowings", "资产负债表", "stock"),
    "一年内到期的非流动负债": ("current_portion_noncurrent_liabilities", "资产负债表", "stock"),
    "长期借款": ("long_term_borrowings", "资产负债表", "stock"),
    "应付债券": ("bonds_payable", "资产负债表", "stock"),
    "租赁负债": ("lease_liabilities", "资产负债表", "stock"),
    "非流动资产合计": ("total_noncurrent_assets", "资产负债表", "stock"),
    "负债合计": ("total_liabilities", "资产负债表", "stock"),
    "流动负债合计": ("total_current_liabilities", "资产负债表", "stock"),
    "非流动负债合计": ("total_noncurrent_liabilities", "资产负债表", "stock"),
    "所有者权益合计": ("total_equity", "资产负债表", "stock"),
    "股东权益合计": ("total_equity", "资产负债表", "stock"),
    "归属于母公司所有者权益合计": ("equity_attributable", "资产负债表", "stock"),
    "归属于母公司股东权益合计": ("equity_attributable", "资产负债表", "stock"),
    "经营活动产生的现金流量净额": ("cashflow_operations", "现金流量表", "flow"),
    "购建固定资产、无形资产和其他长期资产支付的现金": ("capex_cash", "现金流量表", "flow"),
    "期末现金及现金等价物余额": ("cash_end", "现金流量表", "stock"),
    "年末现金及现金等价物余额": ("cash_end", "现金流量表", "stock"),
    "期初现金及现金等价物余额": ("cash_begin", "现金流量表", "stock"),
    "年初现金及现金等价物余额": ("cash_begin", "现金流量表", "stock"),
    "投资活动产生的现金流量净额": ("cashflow_investing", "现金流量表", "flow"),
    "投资活动使用的现金流量净额": ("cashflow_investing", "现金流量表", "flow"),
    "筹资活动产生的现金流量净额": ("cashflow_financing", "现金流量表", "flow"),
    "筹资活动产生/（使用）的现金流量净额": ("cashflow_financing", "现金流量表", "flow"),
    "筹资活动使用的现金流量净额": ("cashflow_financing", "现金流量表", "flow"),
    "汇率变动对现金及现金等价物的影响": ("cashflow_exchange_effect", "现金流量表", "flow"),
    "现金及现金等价物净增加额": ("cash_net_change", "现金流量表", "flow"),
    "现金及现金等价物净减少额": ("cash_net_change", "现金流量表", "flow"),
}

CONCEPT_METADATA = {code: (statement, kind) for code, statement, kind in CONCEPT_LABELS.values()}
# Explicit attributable wording is required; a generic post-nonrecurring profit
# label does not prove whether minority interests have been excluded.
CONCEPT_METADATA["net_income_attributable_excl_nonrecurring"] = ("利润表", "flow")

STATEMENT_ALIASES = {
    "balance_sheet": "资产负债表", "资产负债表": "资产负债表", "合并资产负债表": "资产负债表",
    "income_statement": "利润表", "利润表": "利润表", "合并利润表": "利润表",
    "cashflow": "现金流量表", "cash_flow_statement": "现金流量表",
    "现金流量表": "现金流量表", "合并现金流量表": "现金流量表",
}


def validate_concept_metadata(code: str, statement_type: str, flow_or_stock: str) -> bool:
    expected = CONCEPT_METADATA.get(code)
    return expected is not None and expected == (STATEMENT_ALIASES.get(statement_type), flow_or_stock)
