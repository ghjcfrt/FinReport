"""Located report disclosures for annual, interim and quarterly calculations.

These records are deliberately separate from audited annual statement facts.
Unrecognised headers, units and signs are not guessed.  A selected-page run
keeps the original file digest/page numbers and never reads unselected pages.
"""
from __future__ import annotations

import calendar
import hashlib
import re
from datetime import date
from decimal import Decimal

from finance_adapters.parsing.statement_extractor import compact, page_rows, parse_number
from finance_adapters.persistence.audit import stable_hash

VERSION = "located-report-measures-v1"
CORE = {
    "revenue": "营业收入", "net_income_parent": "归母净利润",
    "net_income_ex_nonrecurring": "扣非归母净利润", "cashflow_operations": "经营现金流净额",
    "net_income": "合并净利润",
}


def validate_report_period(filing) -> None:
    end, start = filing.period_end, filing.period_start
    ends = {"annual": {(12, 31)}, "semi_annual": {(6, 30)},
            "quarterly": {(3, 31), (6, 30), (9, 30), (12, 31)}}
    if (filing.scope != "consolidated" or filing.report_type != filing.period_basis
            or filing.period_basis not in ends or start != date(end.year, 1, 1)
            or (end.month, end.day) not in ends[filing.period_basis]):
        raise ValueError("REPORT_PERIOD_MUST_BE_EXPLICIT_CALENDAR_YEAR_TO_DATE")


def selected_pages(document, pages: list[int] | None) -> list[int]:
    if pages is None:
        return list(range(1, len(document) + 1))
    if (not pages or len(set(pages)) != len(pages)
            or any(type(p) is not int or not 1 <= p <= len(document) for p in pages)):
        raise ValueError("EXTRACTION_PAGE_NUMBERS_INVALID")
    return sorted(pages)


def _code(label: str) -> str | None:
    label = compact(label)
    if "归属于" in label and "扣除非经常性" in label:
        return "net_income_ex_nonrecurring"
    if "归属于" in label and ("净利润" in label or "股东" in label and "利润" in label):
        return "net_income_parent"
    if "经营活动产生的现金流量净额" in label:
        return "cashflow_operations"
    if re.fullmatch(r"(?:一、)?(?:其中[：:])?营业(?:总)?收入(?:\(元\))?", label):
        return "revenue"
    return None


def _number(value: str | None) -> Decimal | None:
    if value is None or "%" in value or "％" in value:
        return None
    return parse_number(value.replace("（", "(").replace("）", ")"))


def _candidate(filing, page_no, page_hash, label, raw, value, period_start, period_end,
               kind, *, scale=1, bbox=None, header="", unit_source=None, group=None) -> dict:
    result = {
        "version": VERSION, "filing_version_id": filing.id, "source_sha256": filing.file_sha256,
        "company_id": filing.company_id, "scope": filing.scope,
        "accounting_standard": filing.accounting_standard, "currency": "CNY", "unit": "CNY",
        "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
        "code": kind, "label": label, "raw_value": raw, "source_unit_scale": scale,
        "value": format(value * scale, "f"), "page_no": page_no, "page_sha256": page_hash,
        "bbox": bbox, "column_header": header, "unit_source": unit_source,
        "source_kind": "pdf_text", "basis": "report_disclosed_not_audited_fact",
        "flow_or_stock": "flow", "group": group,
    }
    return {**result, "id": stable_hash(result)}


def _annual_quarter_candidates(page, tables, filing, page_no, digest, previous):
    """Read only an explicitly headed four-quarter table and its next-page tail."""
    if filing.period_basis != "annual":
        return [], None
    quarter_labels = ["第一季度", "第二季度", "第三季度", "第四季度"]
    kinds = {
        "营业收入": "revenue",
        "归属于上市公司股东的净利润": "net_income_parent",
        "归属于上市公司股东的扣除非经常性损益的净利润": "net_income_ex_nonrecurring",
        "经营活动产生的现金流量净额": "cashflow_operations",
    }
    # A headerless continuation is allowed only for the immediately adjacent
    # selected page, a matching table span, and the unfinished label itself.
    carried = previous if previous and previous["page_no"] == page_no - 1 else None
    state, output = None, []

    def emit(record, header):
        code = kinds.get(compact(record["label"]))
        if code is None or len(record["numeric"]) != 4:
            return False
        for quarter, (raw, value) in enumerate(record["numeric"], 1):
            month = quarter * 3
            item = _candidate(
                filing, record["page_no"], record["page_sha256"], record["label"],
                raw, value, date(filing.period_end.year, month - 2, 1),
                date(filing.period_end.year, month, calendar.monthrange(filing.period_end.year, month)[1]),
                code, bbox=record["bbox"], header=quarter_labels[quarter - 1],
                unit_source=header["unit_source"], group="single_quarter",
            )
            item.pop("id")
            item.update(quarter_table_policy="annual-exact-four-quarter-v1",
                        quarter_header_source=header, label_sources=record["label_sources"])
            output.append({**item, "id": stable_hash(item)})
        return True

    for table_index, table in enumerate(tables):
        rows = table.extract()
        if not rows:
            continue
        first = [compact(c) for c in rows[0] if c and c.strip()]
        header, start, pending = None, 0, None
        if first == quarter_labels:
            titles = [box for box in page.search_for("分季度主要财务指标")
                      if box.y1 <= table.bbox[1]]
            if not titles:
                continue
            title_box = max(titles, key=lambda box: box.y1)
            units = [box for box in page.search_for("单位：元")
                     if title_box.y1 <= box.y0 and box.y1 <= table.bbox[1]]
            if not units:
                continue
            unit_box = max(units, key=lambda box: box.y1)
            header = {"policy": "annual-exact-four-quarter-v1", "page_no": page_no,
                      "page_sha256": digest, "title": "分季度主要财务指标",
                      "title_bbox": list(title_box), "quarter_labels": quarter_labels,
                      "bbox": list(table.rows[0].bbox),
                      "table_bbox": list(table.bbox),
                      "unit_source": {"text": "单位：元", "page_no": page_no,
                                      "page_sha256": digest, "bbox": list(unit_box), "scale": 1}}
            start = 1
        elif (carried and table_index == 0
              and abs(table.bbox[0] - carried["header"]["table_bbox"][0]) <= 2
              and abs(table.bbox[2] - carried["header"]["table_bbox"][2]) <= 2
              and compact("".join(c or "" for c in rows[0])) == "的净利润"):
            header, pending = carried["header"], carried["pending"]
        if header is None:
            state = None
            continue
        for ri in range(start, len(rows)):
            cells = [c for c in rows[ri] if c and c.strip()]
            numeric = [(c, _number(c)) for c in cells if _number(c) is not None]
            label = "\n".join(c for c in cells if _number(c) is None)
            bbox = list(table.rows[ri].bbox)
            label_source = {"page_no": page_no, "page_sha256": digest,
                            "bbox": bbox, "text": label}
            if not numeric:
                if pending and label:
                    pending["label"] += "\n" + label
                    pending["label_sources"].append(label_source)
                    if emit(pending, header):
                        pending = None
                continue
            # A percent, fifth amount or unrecognized label cannot acquire
            # quarter meaning simply because a previous table had that header.
            pending = None
            if len(numeric) != 4 or any("%" in c or "％" in c for c in cells):
                continue
            record = {"label": label, "numeric": numeric, "bbox": bbox,
                      "page_no": page_no, "page_sha256": digest,
                      "label_sources": [label_source]}
            if emit(record, header):
                continue
            if any(key.startswith(compact(label)) for key in kinds if compact(label)):
                pending = record
        # Do not carry a header through an unrelated table or another page.
        state = ({"header": header, "pending": pending, "page_no": page_no}
                 if pending and header["page_no"] == page_no else None)
    return output, state


def extract_report_measures(document, filing, pages: list[int] | None) -> dict:
    validate_report_period(filing)
    numbers = selected_pages(document, pages)
    items, issues, manifests = [], [], []
    inherited_unit = None
    statement = None
    previous_page = 0
    statement_unit = None
    nonrec_header = None
    nonrec_page = 0
    quarter_table = None
    for page_no in numbers:
        page = document[page_no - 1]
        text = page.get_text("text")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        short = compact(text)
        manifests.append({"page_no": page_no, "page_sha256": digest})
        # A table-specific 元 unit must not overwrite the document-wide 千元
        # declaration used by following unlabelled financial statements.
        declared = re.search(r"(?:财务报表及附注|财务附注中报表)的单位为[:：]?(?:人民币)?(千元|万元|元)", short)
        if declared:
            scale = {"千元": 1000, "万元": 10000, "元": 1}[declared.group(1)]
            inherited_unit = {"page_no": page_no, "text": declared.group(0), "scale": scale,
                              "page_sha256": digest}
        tables = page.find_tables().tables
        quarters, quarter_table = _annual_quarter_candidates(
            page, tables, filing, page_no, digest, quarter_table,
        )
        items.extend(quarters)
        has_nonrec = re.search(r"非经常性损益(?:项目(?:和|及)金额|明细表)", short)
        if has_nonrec:
            unit_match = re.search(r"单位[:：]?(?:人民币)?(元|千元|万元)", short)
            unit_scale = {"元": 1, "千元": 1000, "万元": 10000} if unit_match else {}
            nonrec_header = {"page_no": page_no, "page_sha256": digest,
                             "text": has_nonrec.group(), "qtd_ytd": "年初至报告期期末金额" in short,
                             "years": [filing.period_end.year, filing.period_end.year - 1]
                             if f"{filing.period_end.year - 1}年金额" in short else [filing.period_end.year],
                             "unit_source": ({"text": unit_match.group(0), "page_no": page_no,
                                               "page_sha256": digest, "scale": unit_scale[unit_match.group(1)]}
                                              if unit_match else None)}
            nonrec_page = page_no
        elif nonrec_page != page_no - 1:
            nonrec_header = None
        if nonrec_header and nonrec_header.get("unit_source") is not None \
                and nonrec_page in {page_no, page_no - 1}:
            for table in tables:
                rows = table.extract()
                for ri, row in enumerate(rows):
                    cells = [c for c in row if c and c.strip()]
                    if not cells:
                        continue
                    label = cells[0]
                    for following in rows[ri + 1:]:
                        fc = [c for c in following if c and c.strip()]
                        if len(fc) == 1 and _number(fc[0]) is None:
                            label += "\n" + fc[0]
                        else:
                            break
                    key = compact(label)
                    code = ("asset_disposal" if key.startswith("非流动性资产处置损益") else
                            "government_grants" if key.startswith("计入当期损益的政府补助") else
                            "financial_instruments" if key.startswith("除同") and "套期保值" in key else
                            "impairment_reversal" if key.startswith("单独进行减值测试") else
                            "other" if key.startswith("除上述各项之外") else
                            "tax_effect" if "所得税影响" in key else
                            "minority_effect" if key.startswith("少数股东权益影响") else
                            "total" if key == "合计" else None)
                    numeric = [(c, _number(c)) for c in cells[1:] if _number(c) is not None]
                    if code is None or not numeric:
                        continue
                    expected = 2 if nonrec_header["qtd_ytd"] else len(nonrec_header["years"])
                    if len(numeric) < expected:
                        issues.append(f"NONREC_COLUMNS_INCOMPLETE:p{page_no}:{code}")
                        continue
                    for offset, (raw, value) in enumerate(numeric[:expected]):
                        year = filing.period_end.year if nonrec_header["qtd_ytd"] else nonrec_header["years"][offset]
                        if nonrec_header["qtd_ytd"]:
                            role = "single_quarter" if offset == 0 else "year_to_date"
                            start = date(year, filing.period_end.month - 2, 1) if offset == 0 else date(year, 1, 1)
                        else:
                            role = "current" if offset == 0 else "comparative"
                            start = date(year, 1, 1)
                        candidate = _candidate(filing, page_no, digest, label, raw, value, start,
                                               date(year, filing.period_end.month, filing.period_end.day),
                                               "nonrec_" + code, bbox=list(table.rows[ri].bbox),
                                               scale=nonrec_header["unit_source"]["scale"],
                                               header=str(nonrec_header), unit_source=nonrec_header["unit_source"],
                                               group=role)
                        # Preserve the raw signed amount and the source row
                        # semantics in the candidate digest.
                        candidate.pop("id")
                        # Non-recurring-loss tables print the sign in the amount
                        # cell.  Do not apply a second deduction operator based on
                        # the row label; that would flip already-negative tax or
                        # minority-effect amounts.
                        candidate["arithmetic_sign"] = 1
                        items.append({**candidate, "id": stable_hash(candidate)})
        for table in tables:
            rows = table.extract()
            table_text = "".join(compact(c or "") for row in rows for c in row)
            if "营业收入(元)" not in table_text or "上年" not in table_text and str(filing.period_end.year) not in table_text:
                continue
            header = " | ".join(c or "" for row in rows[:2] for c in row)
            nine_month = "年初至报告期末" in compact(header)
            if not nine_month and not ("上年同期" in compact(header) or str(filing.period_end.year - 1) in compact(header)):
                continue
            for ri, row in enumerate(rows):
                cells = [c for c in row if c and c.strip()]
                if not cells:
                    continue
                label = cells[0]
                # The table detector sometimes splits wrapped labels into
                # rows with no numbers. Retain those continuation words.
                for following in rows[ri + 1:ri + 3]:
                    fc = [c for c in following if c and c.strip()]
                    if fc and all(_number(c) is None and "%" not in c for c in fc) and len(fc) == 1:
                        label += "\n" + fc[0]
                    else:
                        break
                code = _code(label)
                if code is None:
                    continue
                numeric = [(c, _number(c)) for c in cells[1:] if _number(c) is not None]
                if not numeric:
                    continue
                box = list(table.rows[ri].bbox)
                if nine_month:
                    # This layout contains current single-quarter and YTD,
                    # not prior amounts. Never invert rounded YoY rates.
                    expected = 1 if code == "cashflow_operations" else 2
                    if len(numeric) != expected:
                        issues.append(f"AMBIGUOUS_QTD_YTD_COLUMNS:p{page_no}:{code}")
                        continue
                    assignments = [(numeric[-1], filing.period_start, filing.period_end, "year_to_date")]
                    if expected == 2:
                        assignments.append((numeric[0], date(filing.period_end.year, filing.period_end.month - 2, 1),
                                            filing.period_end, "single_quarter"))
                else:
                    if len(numeric) < 2:
                        continue
                    assignments = [(numeric[0], filing.period_start, filing.period_end, "current"),
                                   (numeric[1], date(filing.period_start.year - 1, 1, 1),
                                    date(filing.period_end.year - 1, filing.period_end.month,
                                         calendar.monthrange(filing.period_end.year - 1, filing.period_end.month)[1]), "comparative")]
                for (raw, value), start, end, role in assignments:
                    items.append(_candidate(filing, page_no, digest, label, raw, value, start, end, code,
                                            bbox=box, header=header, unit_source={"text": "（元）", "page_no": page_no}, group=role))
        # Native statement rows and unruled notes retain their actual signs.
        rows = page_rows(page)
        numeric_rows = []
        for row in rows:
            ordered = sorted(row, key=lambda c: c.bbox[0])
            numeric = [(c.text, _number(c.text), c.bbox) for c in ordered if _number(c.text) is not None]
            label = " ".join(c.text for c in ordered if _number(c.text) is None).strip()
            numeric_rows.append((label, numeric, [min(c.bbox[0] for c in row), min(c.bbox[1] for c in row),
                                                 max(c.bbox[2] for c in row), max(c.bbox[3] for c in row)]))
        # A continuation page is admitted only immediately after a statement
        # page; carrying this flag through unrelated notes confuses EPS with
        # a monetary profit row.
        if previous_page != page_no - 1:
            statement, statement_unit = None, None
        unit_source = ({"text": "单位：元", "page_no": page_no, "scale": 1, "page_sha256": digest}
                       if "单位：元" in short else inherited_unit or statement_unit)
        local_scale = unit_source["scale"] if unit_source else None
        bridge = False
        for label, numeric, bbox in numeric_rows:
            key = compact(label)
            if re.search(r"合并(?:年初到报告期末|年初至报告期末)?利润表", key):
                statement, statement_unit = "income", unit_source
            elif re.search(r"合并(?:年初到报告期末|年初至报告期末)?现金流量表", key):
                statement, statement_unit = "cash", unit_source
            elif re.search(r"母公司.*(?:表|附注)|非经常性损益|净资产收益率和每股收益", key):
                statement = None
            if "将净利润调节为经营活动现金流量" in key:
                bridge = True
                continue
            if bridge and "经营活动产生的现金流量净额" in key:
                kind = "bridge_total"
                bridge = False
            elif bridge and len(numeric) >= 2 and key:
                kind = "bridge_row:" + re.sub(r"^加[：:]", "", key)
            elif statement == "income" and re.fullmatch(r"[一二三四五六七八九十]+[、.]?净利润(?:\(.*?\))?", key):
                kind = "net_income"
            elif statement == "income" and re.match(r"(?:1[.、])?归属于母公司",key) and "净利润" in key:
                kind = "net_income_parent"
            elif statement == "cash" and key == "经营活动产生的现金流量净额":
                kind = "cashflow_operations"
            elif statement == "income" and re.fullmatch(r"[一二三四五六七八九十、.]*营业(?:总)?收入", key):
                kind = "revenue"
            else:
                continue
            if local_scale is None or len(numeric) < 2:
                issues.append(f"EXPLICIT_UNIT_OR_TWO_COLUMNS_REQUIRED:p{page_no}:{kind}")
                continue
            for offset, (raw, value, _) in enumerate(numeric[-2:]):
                year = filing.period_end.year - offset
                items.append(_candidate(filing, page_no, digest, label, raw, value,
                                        date(year, 1, 1), date(year, filing.period_end.month, filing.period_end.day),
                                        kind, scale=local_scale, bbox=bbox,
                                        header="current/prior in explicitly headed consolidated statement",
                                        unit_source=unit_source, group="cashflow_bridge" if kind.startswith("bridge_") else "primary_statement"))
        previous_page = page_no
    return {"version": VERSION, "items": items, "issues": sorted(set(issues)),
            "pages": manifests, "processing_scope": "selected_pages" if pages is not None else "whole_document",
            "source_sha256": filing.file_sha256, "selected_page_numbers": numbers}
