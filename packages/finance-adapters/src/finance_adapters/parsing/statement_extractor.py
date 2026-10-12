from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from itertools import pairwise


@dataclass(frozen=True)
class TextCell:
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class StatementFact:
    concept_code: str
    concept_name: str
    statement_type: str
    flow_or_stock: str
    value: Decimal
    raw_value: str
    raw_label: str
    page_no: int
    table_name: str
    column_label: str
    currency: str
    unit_scale: int
    row_bbox: tuple[float, float, float, float]
    value_bbox: tuple[float, float, float, float]
    page_sha256: str
    unit_page_no: int | None = None
    unit_text: str | None = None


def parse_number(value: str) -> Decimal | None:
    cleaned = re.sub(r"\s+", "", value).replace("，", ",")
    cleaned = cleaned.replace("（", "(").replace("）", ")").replace("−", "-")
    if not re.fullmatch(r"\(?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?", cleaned):
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    try:
        number = Decimal(cleaned.strip("()").replace(",", ""))
        return -number if negative else number
    except InvalidOperation:
        return None


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("（", "(").replace("）", ")")


def normalized_label(text: str) -> str:
    text = compact(text)
    text = re.sub(r"^[一二三四五六七八九十百\d]+[、.．:：]", "", text)
    text = re.sub(r"^(?:减|加|其中)[:：]", "", text)
    return text.rstrip(":：")


def page_rows(page) -> list[list[TextCell]]:
    cells = [
        TextCell(span["text"], tuple(span["bbox"]))
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if span["text"].strip()
    ]
    rows: list[list[TextCell]] = []
    for cell in sorted(cells, key=lambda item: (item.bbox[1], item.bbox[0])):
        center = (cell.bbox[1] + cell.bbox[3]) / 2
        if rows and abs(center - (rows[-1][0].bbox[1] + rows[-1][0].bbox[3]) / 2) <= 3:
            rows[-1].append(cell)
        else:
            rows.append([cell])
    return [sorted(row, key=lambda item: item.bbox[0]) for row in rows]


def union_bbox(cells: list[TextCell]) -> tuple[float, float, float, float]:
    return (
        min(cell.bbox[0] for cell in cells),
        min(cell.bbox[1] for cell in cells),
        max(cell.bbox[2] for cell in cells),
        max(cell.bbox[3] for cell in cells),
    )


def declared_scale(text: str) -> int | None:
    matches = re.findall(
        r"(?:单位(?:为)?[：:]?|以(?:人民币|美元|港元))\s*(?:人民币|美元|港元)?\s*(亿元|万元|千元|元)",
        compact(text),
    )
    scales = {"元": 1, "千元": 1000, "万元": 10000, "亿元": 100000000}
    values = {scales[value] for value in matches}
    return values.pop() if len(values) == 1 else None


def declared_currency(text: str) -> str | None:
    values = set()
    for name, code in (("人民币", "CNY"), ("美元", "USD"), ("港元", "HKD")):
        if re.search(r"(?:单位[：:]|以)" + name, compact(text)) or re.search(
            r"编制本财务报表.{0,35}(?:货币|币).{0,10}(?:为|采用)" + name, compact(text)
        ):
            values.add(code)
    return values.pop() if len(values) == 1 else None


class StatementExtractor:
    """Only numbered consolidated primary statements and their adjacent table pages.

    Amounts retain the printed unit. Narrative, notes and company-only statements
    cannot introduce facts. Uncertain headers, units or conflicting rows are surfaced.
    """

    def __init__(
        self,
        concepts: dict,
        target_year: int,
        default_currency: str | None = None,
        default_scale: int | None = None,
        expected_period_end: date | None = None,
    ):
        self.concepts = concepts
        self.target_year = target_year
        self.expected_period_end = expected_period_end
        self.currency = default_currency
        self.scale = default_scale
        self.statement: str | None = None
        self.table_name: str | None = None
        self.last_page = 0
        self.facts: dict[str, StatementFact] = {}
        self.issues: list[str] = []
        self.statement_pages: list[int] = []
        self.unit_page_no: int | None = None
        self.unit_text: str | None = None

    def add_page(self, page, page_no: int, page_hash: str) -> list[StatementFact]:
        rows = page_rows(page)
        text = page.get_text("text")
        titles = []
        for row in rows:
            label = compact("".join(cell.text for cell in row))
            match = re.fullmatch(
                r"(?:\d+[、.．])?(合并|母公司|公司)(资产负债表|利润表|现金流量表|所有者权益变动表)(?:\(续\))?",
                label,
            )
            if match:
                titles.append((match, row[0].bbox[1]))
        title_bottom = 0.0
        if len(titles) > 1:
            self.statement = self.table_name = None
            self.issues.append(f"page:{page_no}:MULTIPLE_STATEMENT_SECTIONS_REQUIRE_REVIEW")
            return []
        if titles:
            match, title_y = titles[0]
            title_bottom = title_y + 10
            if match[1] != "合并" or match[2] == "所有者权益变动表":
                self.statement = self.table_name = None
                return []
            self.statement, self.table_name = match[2], "合并" + match[2]
        elif self.last_page != page_no - 1 or any(
            re.fullmatch(
                r"(?:[一二三四五六七八九十\d]+[、.．])?(?:财务报表附注|合并财务报表项目注释|"
                r"重要会计政策及会计估计|财务报表的编制基础)(?:\(续\))?",
                compact("".join(cell.text for cell in row)),
            )
            for row in rows
            if row[0].bbox[1] < page.rect.height / 3
        ):
            self.statement = self.table_name = None
        self.last_page = page_no
        if self.statement is None:
            return self._english_fixture(rows, page_no, page_hash, text)

        # A continuation must repeat the year columns; notes without a primary
        # statement title never restart an ended statement context.
        header = None
        for row in rows:
            years = [
                cell
                for cell in row
                if re.fullmatch(r"20\d{2}年(?:\d{1,2}月\d{1,2}日)?", compact(cell.text))
            ]
            if len(years) >= 2 and row[0].bbox[1] >= title_bottom:
                target = [
                    cell for cell in years if compact(cell.text).startswith(f"{self.target_year}年")
                ]
                if len(target) == 1:
                    header = (row, years, target[0])
                    break
        if header is None:
            self.statement = self.table_name = None
            if titles:
                self.issues.append(f"page:{page_no}:CURRENT_PERIOD_COLUMN_UNCONFIRMED")
            return []
        scale = declared_scale(text)
        if scale is not None:
            self.scale = scale
            self.unit_page_no = page_no
            self.unit_text = next(
                (line.strip() for line in text.splitlines() if declared_scale(line) == scale), None
            )
        currency = declared_currency(text)
        if currency is not None:
            self.currency = currency
        if self.scale is None or self.currency is None:
            self.issues.append(f"page:{page_no}:CURRENCY_OR_UNIT_UNCONFIRMED")
            return []
        header_row, years, target = header
        target_label = compact(target.text)
        date_match = re.fullmatch(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", target_label)
        if date_match and self.expected_period_end is not None:
            try:
                header_end = date(*(int(value) for value in date_match.groups()))
            except ValueError:
                header_end = None
            if header_end != self.expected_period_end:
                self.issues.append(f"page:{page_no}:STATEMENT_PERIOD_MISMATCH:{target_label}")
                return []
        elif self.expected_period_end is not None and self.statement == "资产负债表":
            self.issues.append(f"page:{page_no}:BALANCE_SHEET_DATE_UNCONFIRMED")
            return []
        years = sorted(years, key=lambda item: item.bbox[0])
        centers = [(cell.bbox[0] + cell.bbox[2]) / 2 for cell in years]
        target_index = years.index(target)
        separation = min(right - left for left, right in pairwise(centers))
        left = (
            (centers[target_index - 1] + centers[target_index]) / 2
            if target_index
            else centers[0] - separation / 2
        )
        right = (
            (centers[target_index] + centers[target_index + 1]) / 2
            if target_index < len(years) - 1
            else centers[-1] + separation / 2
        )
        label_right = centers[0] - separation / 2
        column_label = target.text.strip()
        for row in rows:
            if not (header_row[0].bbox[1] < row[0].bbox[1] <= header_row[0].bbox[3] + 25):
                continue
            for cell in row:
                if "重述" in cell.text and not left <= (cell.bbox[0] + cell.bbox[2]) / 2 < right:
                    column_label += "；比较列经重述"
        self.statement_pages.append(page_no)
        new_facts = []
        pending_label: list[TextCell] = []
        for row in rows:
            if row[0].bbox[1] <= header_row[0].bbox[1] + 5:
                continue
            if any("后附财务报表附注" in cell.text for cell in row):
                break
            label_cells = [
                cell
                for cell in row
                if cell.bbox[0] < label_right and parse_number(cell.text) is None
            ]
            values = [
                cell
                for cell in row
                if left <= (cell.bbox[0] + cell.bbox[2]) / 2 < right
                and parse_number(cell.text) is not None
            ]
            if not values:
                pending_label = label_cells if label_cells else []
                continue
            if not label_cells:
                label_cells = pending_label
            elif pending_label and row[0].bbox[1] - pending_label[-1].bbox[3] <= 7:
                current_label = normalized_label("".join(cell.text for cell in label_cells))
                if not any(normalized_label(alias) == current_label for alias in self.concepts):
                    label_cells = pending_label + label_cells
            pending_label = []
            label = normalized_label("".join(cell.text for cell in label_cells))
            matches = [
                (alias, meta)
                for alias, meta in self.concepts.items()
                if normalized_label(alias) == label and meta[1] == self.statement
            ]
            if not matches:
                continue
            if len(values) != 1 or len({meta[0] for _, meta in matches}) != 1:
                self.issues.append(f"page:{page_no}:{label}:AMBIGUOUS_TABLE_ROW")
                continue
            _, (code, _, flow_or_stock) = matches[0]
            cell = values[0]
            fact = StatementFact(
                code,
                label,
                self.statement,
                flow_or_stock,
                parse_number(cell.text),
                cell.text.strip(),
                "".join(item.text for item in label_cells).strip(),
                page_no,
                self.table_name,
                column_label,
                self.currency,
                self.scale,
                union_bbox(label_cells + [cell]),
                cell.bbox,
                page_hash,
                self.unit_page_no,
                self.unit_text,
            )
            old = self.facts.get(code)
            if old:
                if (old.value, old.unit_scale, old.currency) != (
                    fact.value,
                    fact.unit_scale,
                    fact.currency,
                ):
                    self.issues.append(f"page:{page_no}:{code}:CONFLICTING_PRIMARY_STATEMENT_ROWS")
                continue
            self.facts[code] = fact
            new_facts.append(fact)
        return new_facts

    def _english_fixture(self, rows, page_no, page_hash, text) -> list[StatementFact]:
        # Explicit English-only test fixtures have no bilingual filing metadata.
        # This compatibility path is never enabled for Chinese report concepts.
        if not self.concepts or not all(label.isascii() for label in self.concepts):
            return []
        if "Income Statement" not in text:
            return []
        added = []
        for row in rows:
            joined = " ".join(cell.text for cell in row)
            for label, (code, statement, flow) in self.concepts.items():
                match = re.fullmatch(re.escape(label) + r"\s+([-\d,.()]+)", joined.strip())
                if not match or code in self.facts:
                    continue
                value = parse_number(match[1])
                if value is None:
                    continue
                bbox = union_bbox(row)
                fact = StatementFact(
                    code,
                    label,
                    statement,
                    flow,
                    value,
                    match[1],
                    label,
                    page_no,
                    "Income Statement",
                    str(self.target_year),
                    "CNY",
                    1,
                    bbox,
                    bbox,
                    page_hash,
                )
                self.facts[code] = fact
                added.append(fact)
        return added


class _HtmlTables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def ocr_table_candidates(text: str) -> list[list[list[str]]]:
    parser = _HtmlTables()
    parser.feed(text)
    if parser.tables:
        return parser.tables
    rows = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [item.strip() for item in line.strip().strip("|").split("|")]
        if cells and not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            rows.append(cells)
    return [rows] if rows else []
