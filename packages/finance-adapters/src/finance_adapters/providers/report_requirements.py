"""Exact comparison-input gaps within the user's selected report set.

Missing means unavailable in this selection, never absent from the filing or
from the user's disk. No file is opened and no other filing is authorised here.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal

from finance_domain.analysis.agent import MaterialRequirement

from finance_adapters.parsing.report_measures import CORE
from finance_adapters.persistence.audit import stable_hash

PREFIX = "material-report-"


def _quarter_period(year: int, quarter: int) -> tuple[str, str]:
    month = quarter * 3
    return (date(year, month - 2, 1).isoformat(),
            date(year, month, calendar.monthrange(year, month)[1]).isoformat())


def _ytd_period(year: int, quarter: int) -> tuple[str, str]:
    return date(year, 1, 1).isoformat(), _quarter_period(year, quarter)[1]


def report_comparison_requirements(package: dict, documents: list[dict]) -> list[MaterialRequirement]:
    """Ask for exact missing YoY/QoQ operands, allowing printed comparatives.

    An annual report's directly printed quarterly figures are already local
    operands. Otherwise use calendar YTD subtraction. A rounded growth rate
    is never used to reverse-engineer a missing amount.
    """
    bundle = package.get("report_measure_bundle", {})
    if bundle.get("version") != "report-measures-bundle-v2":
        return []
    selected = set(package["selected_document_ids"])
    if selected != {item["filing_version_id"] for item in bundle["documents"]}:
        raise ValueError("REPORT_REQUIREMENT_DOCUMENT_SELECTION_MISMATCH")
    items = bundle["items"]
    if any(item["filing_version_id"] not in selected for item in items):
        raise ValueError("REPORT_REQUIREMENT_INPUT_OUTSIDE_SELECTION")
    available = {(item["code"], item["period_start"], item["period_end"])
                 for item in items if item.get("review_status") != "rejected"}
    period_sources = defaultdict(set)
    for item in items:
        period_sources[(item["period_start"], item["period_end"])].add(item["filing_version_id"])
    for document in bundle["documents"]:
        period_sources[(document["period_start"], document["period_end"])].add(document["filing_version_id"])
    gaps = defaultdict(lambda: {"codes": set(), "uses": set()})

    def need(code: str, period: tuple[str, str], use: str) -> None:
        if (code, *period) not in available:
            gaps[period]["codes"].add(code)
            gaps[period]["uses"].add(use)

    def quarter_inputs(code: str, year: int, quarter: int) -> None:
        direct = _quarter_period(year, quarter)
        if (code, *direct) in available:
            return
        need(code, _ytd_period(year, quarter), "单季环比")
        if quarter > 1:
            need(code, _ytd_period(year, quarter - 1), "单季环比")

    for document in bundle["documents"]:
        end = date.fromisoformat(document["period_end"])
        current = (document["period_start"], document["period_end"])
        year, quarter = end.year, end.month // 3
        prior = (f"{year - 1}-01-01", f"{year - 1}" + current[1][4:])
        for code in CORE:
            if (code, *current) not in available:
                continue
            need(code, prior, "同比" if code != "net_income_ex_nonrecurring" else "同比及上期非经常性损益归母税后差额")
            quarter_inputs(code, year, quarter)
            quarter_inputs(code, year if quarter > 1 else year - 1, quarter - 1 if quarter > 1 else 4)

    result = []
    for (start, end), gap in sorted(gaps.items()):
        codes = sorted(gap["codes"])
        matches = sorted(period_sources[(start, end)])
        use = "、".join(sorted(gap["uses"]))
        labels = "、".join(CORE[code] for code in codes)
        purpose = (f"本次已选文件及原页尚未取得 {start} 至 {end} 的 {labels} 精确金额，用于{use}。"
                   "先核对已选报告比较栏或直接披露的单季数；不得倒推舍入后的同比百分比。"
                   + ("该期间已有所选文件或比较栏，请补选原页、提取并审核缺失科目。" if matches else
                      "若相应材料已在本地，请显式加入所选集合；否则请上传对应期间报告。")
                   + "缺口仅指本次分析选择，不代表原财报未披露；不会自动读取或联合其他文件。")
        payload = {"topic": "performance_changes", "requirement_kind": "review" if matches else "document",
                   "report_type": None, "period_start": start, "period_end": end,
                   "concept_codes": codes, "purpose": purpose,
                   "acceptable_alternatives": [
                       "同口径报告中的精确比较栏或单季金额及原页审核",
                       "显式选择相应年报、半年报或季报，按累计金额相减并核对会计及合并范围口径",
                   ]}
        result.append(MaterialRequirement(id=PREFIX + stable_hash(payload)[:24], **payload,
                      status="needs_review" if matches else "missing", matching_document_ids=matches))
    return result


def refresh_report_requirement(requirement: MaterialRequirement, documents: list[dict],
                               calculations: list[dict]) -> MaterialRequirement | None:
    """Resolve only reviewed operands from authorised saved calculations."""
    if not requirement.id.startswith(PREFIX):
        return None
    selected = {item["filing_version_id"] for item in documents}
    period = (requirement.period_start, requirement.period_end)
    by_code = defaultdict(list)
    matching = {item["filing_version_id"] for item in documents
                if (item["period_start"], item["period_end"]) == period}
    for calculation in calculations:
        if calculation["kind"] != "report_measure":
            continue
        for item in calculation["result"].get("input_snapshot", []):
            if item["filing_version_id"] not in selected:
                raise ValueError("REPORT_REQUIREMENT_INPUT_OUTSIDE_SELECTION")
            if (item["period_start"], item["period_end"]) == period:
                matching.add(item["filing_version_id"])
                if item["code"] in requirement.concept_codes:
                    by_code[item["code"]].append(item)
    supplied = bool(requirement.concept_codes) and all(
        by_code[code] and len({(Decimal(item["value"]), item.get("arithmetic_sign", 1))
                              for item in by_code[code]}) == 1
        and all(item.get("review_status") == "reviewed" and not item.get("source_conflict")
                for item in by_code[code])
        for code in requirement.concept_codes
    )
    matching.update(set(requirement.matching_document_ids) & selected)
    return requirement.model_copy(update={
        "status": "provided" if supplied else "needs_review" if matching else "missing",
        "matching_document_ids": sorted(matching),
    })
