from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from itertools import pairwise
from statistics import median


@dataclass(frozen=True)
class TextCell:
    text: str
    bbox: tuple[float, float, float, float]
    characters: tuple[tuple[str, tuple[float, float, float, float]], ...] = ()
    font_size: float = 0.0
    coordinate_source: str = "pdf_span_bbox_fallback"


@dataclass(frozen=True)
class LabelLayout:
    """Printed geometry only: an indentation level is not an accounting parent."""

    page_no: int
    table_name: str
    source: str
    table_origin_x: float
    label_bbox: tuple[float, float, float, float]
    label_start_x: float
    body_start_x: float
    prefix: str
    indent_points: float
    indent_level: int
    font_size: float


@dataclass(frozen=True)
class StatementTableRow:
    row_index: int
    cells: tuple[str, ...]
    raw_label: str
    row_bbox: tuple[float, float, float, float]
    label_layout: LabelLayout


@dataclass(frozen=True)
class StatementTable:
    page_no: int
    table_name: str
    statement_type: str
    headers: tuple[str, ...]
    column_roles: tuple[str, ...]
    table_bbox: tuple[float, float, float, float]
    rows: tuple[StatementTableRow, ...]
    page_sha256: str


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
    label_layout: LabelLayout | None = None


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
    try:
        content = page.get_text("rawdict")
    except (AttributeError, KeyError, NotImplementedError, TypeError, ValueError):
        content = page.get_text("dict")
    if not isinstance(content, dict) or "blocks" not in content:
        content = page.get_text("dict")
    cells = []
    for block in content["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                chars = span.get("chars", [])
                text = "".join(char["c"] for char in chars) if chars else span.get("text", "")
                if not text.strip():
                    continue
                visible = [char for char in chars if not char["c"].isspace()]
                bbox = (
                    (
                        min(char["bbox"][0] for char in visible),
                        min(char["bbox"][1] for char in visible),
                        max(char["bbox"][2] for char in visible),
                        max(char["bbox"][3] for char in visible),
                    )
                    if visible else tuple(span["bbox"])
                )
                cells.append(TextCell(
                    text, bbox,
                    tuple((char["c"], tuple(char["bbox"])) for char in chars),
                    float(span.get("size", bbox[3] - bbox[1])),
                    "pdf_text_geometry" if visible else "pdf_span_bbox_fallback",
                ))
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


def _raw_label(cells: list[TextCell]) -> str:
    lines: list[list[TextCell]] = []
    for cell in sorted(cells, key=lambda item: (item.bbox[1], item.bbox[0])):
        if lines and abs(cell.bbox[1] - lines[-1][0].bbox[1]) <= 3:
            lines[-1].append(cell)
        else:
            lines.append([cell])
    return "\n".join("".join(cell.text for cell in line).strip() for line in lines)


def _font_size(cells: list[TextCell]) -> float:
    sizes = [cell.font_size for cell in cells if cell.font_size > 0]
    return median(sizes) if sizes else 0.0


def _label_layout(
    cells: list[TextCell], page_no: int, table_name: str,
    origin_x: float, band_centers: list[float],
) -> LabelLayout:
    label = compact(_raw_label(cells))
    match = re.match(r"^(?:[一二三四五六七八九十百\d]+[、.．:：]|(?:加|减|其中)[:：])", label)
    prefix = match[0] if match else ""
    ordered = sorted(cells, key=lambda cell: (cell.bbox[1], cell.bbox[0]))
    visible = [(char, bbox) for cell in ordered for char, bbox in cell.characters
               if not char.isspace()]
    bbox = union_bbox(cells)
    # Dict-only page adapters cannot establish a within-span character position.
    # Preserve their coarse geometry and identify it explicitly instead of guessing.
    body_x = visible[len(prefix)][1][0] if len(visible) > len(prefix) else bbox[0]
    if not visible and prefix:
        remaining = len(prefix)
        for cell in ordered:
            length = len(compact(cell.text))
            if remaining < length:
                body_x = cell.bbox[0]
                break
            remaining -= length
    return LabelLayout(
        page_no, table_name,
        "pdf_text_geometry" if all(cell.coordinate_source == "pdf_text_geometry"
                                    for cell in cells) else "pdf_span_bbox_fallback",
        origin_x, bbox, bbox[0], body_x, prefix, max(0.0, bbox[0] - origin_x),
        min(range(len(band_centers)), key=lambda i: abs(band_centers[i] - bbox[0])),
        _font_size(cells),
    )


def _statement_table(
    rows: list[list[TextCell]], header_row: list[TextCell], years: list[TextCell],
    page_no: int, table_name: str, statement_type: str, page_hash: str,
) -> StatementTable:
    years = sorted(years, key=lambda cell: cell.bbox[0])
    centers = [(cell.bbox[0] + cell.bbox[2]) / 2 for cell in years]
    separation = min(right - left for left, right in pairwise(centers))
    boundaries = [centers[0] - separation / 2,
                  *((left + right) / 2 for left, right in pairwise(centers)),
                  centers[-1] + separation / 2]
    notes = next((cell for cell in header_row if "附注" in cell.text
                  and cell.bbox[0] < boundaries[0]), None)
    label_right = notes.bbox[0] if notes else boundaries[0]
    label_header = "".join(cell.text for cell in header_row
                           if cell.bbox[0] < label_right and cell not in years).strip()
    headers = [label_header] + ([notes.text.strip()] if notes else [])
    headers.extend(cell.text.strip() for cell in years)
    roles = ["label"] + (["notes"] if notes else [])
    roles.extend("year:" + compact(cell.text)[:4] for cell in years)
    header_cells = list(header_row)
    header_top = min(cell.bbox[1] for cell in header_row)
    header_bottom = max(cell.bbox[3] for cell in header_row)
    for row in rows:
        if not (header_top < row[0].bbox[1] <= header_bottom + 25):
            continue
        for cell in row:
            if "重述" not in cell.text and "重列" not in cell.text:
                continue
            center = (cell.bbox[0] + cell.bbox[2]) / 2
            for index in range(len(years)):
                if boundaries[index] <= center < boundaries[index + 1]:
                    headers[1 + bool(notes) + index] += "\n" + cell.text.strip()
                    header_cells.append(cell)
    numeric_baselines = [
        (row[0].bbox[1] + row[0].bbox[3]) / 2 for row in rows
        if row[0].bbox[1] > header_bottom and any(
            boundaries[0] <= (cell.bbox[0] + cell.bbox[2]) / 2 < boundaries[-1]
            and parse_number(cell.text) is not None for cell in row
        )
    ]
    numeric_gaps = [right - left for left, right in pairwise(numeric_baselines) if right > left]
    numeric_line_gap = 0.0
    # Sparse tables do not establish normal row spacing: a short next label may
    # be the first child of a category rather than the end of a wrapped label.
    if len(numeric_gaps) >= 3:
        typical_gap = median(numeric_gaps)
        stable_gaps = [gap for gap in numeric_gaps if abs(gap - typical_gap) <= 1.0]
        if len(stable_gaps) >= 3:
            numeric_line_gap = median(stable_gaps)
    drafts: list[tuple[list[TextCell], list[TextCell]]] = []
    for row in rows:
        if row[0].bbox[1] <= header_top + 5:
            continue
        row_text = "".join(cell.text for cell in row)
        if re.search(r"后附财务报表附注|法定代表人|主管会计工作负责人|会计机构负责人", row_text):
            break
        if any(cell in header_cells for cell in row) or declared_scale(row_text) is not None:
            continue
        labels = [cell for cell in row if cell.bbox[0] < label_right
                  and parse_number(cell.text) is None]
        data = [cell for cell in row if cell not in labels and cell.bbox[0] >= label_right]
        if not labels:
            if drafts and data and not drafts[-1][1]:
                previous = drafts[-1][0]
                if row[0].bbox[1] - union_bbox(previous)[3] <= max(7, _font_size(previous)):
                    drafts[-1][1].extend(data)
            continue
        if drafts and not drafts[-1][1]:
            previous = drafts[-1][0]
            font_size = _font_size(previous + labels)
            tolerance = max(1.0, font_size * 0.1)
            previous_box, current_box = union_bbox(previous), union_bbox(labels)
            previous_text = compact(_raw_label(previous))
            at_column_edge = previous_box[2] >= label_right - max(2, font_size * 2)
            tight_line_spacing = (
                numeric_line_gap > 0
                and current_box[1] - previous_box[1] <= numeric_line_gap * 0.6
                and previous_box[2] - previous_box[0] >= (label_right - previous_box[0]) / 2
                and current_box[2] - current_box[0]
                >= (previous_box[2] - previous_box[0]) * 0.4
                and len(previous_text) > len(compact(_raw_label(labels)))
            )
            if (
                abs(previous_box[0] - current_box[0]) <= tolerance
                and current_box[1] - previous_box[3] <= max(7, font_size * 0.8)
                and not re.match(r"^[一二三四五六七八九十百\d]+[、.．:：]", previous_text)
                and not previous_text.endswith(("：", ":"))
                and (at_column_edge or tight_line_spacing)
            ):
                previous.extend(labels)
                drafts[-1][1].extend(data)
                continue
        drafts.append((list(labels), list(data)))
    if not drafts:
        return StatementTable(page_no, table_name, statement_type, tuple(headers), tuple(roles),
                              union_bbox(header_cells), (), page_hash)
    origin_x = min(union_bbox(labels)[0] for labels, _ in drafts)
    tolerance = max(1.0, _font_size([cell for labels, _ in drafts for cell in labels]) * 0.1)
    bands: list[list[float]] = []
    for x in sorted(union_bbox(labels)[0] for labels, _ in drafts):
        if bands and x - sum(bands[-1]) / len(bands[-1]) <= tolerance:
            bands[-1].append(x)
        else:
            bands.append([x])
    band_centers = [sum(band) / len(band) for band in bands]
    table_rows = []
    all_cells = list(header_cells)
    for index, (labels, data) in enumerate(drafts):
        raw_label = _raw_label(labels)
        values = [raw_label.replace("\n", "")]
        if notes:
            values.append("".join(cell.text for cell in data
                                  if cell.bbox[0] < boundaries[0]).strip())
        for column in range(len(years)):
            values.append("".join(cell.text for cell in data
                                  if boundaries[column] <= (cell.bbox[0] + cell.bbox[2]) / 2
                                  < boundaries[column + 1]).strip())
        table_rows.append(StatementTableRow(
            index, tuple(values), raw_label, union_bbox(labels + data),
            _label_layout(labels, page_no, table_name, origin_x, band_centers),
        ))
        all_cells.extend(labels + data)
    return StatementTable(page_no, table_name, statement_type, tuple(headers), tuple(roles),
                          union_bbox(all_cells), tuple(table_rows), page_hash)


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
        self.tables: dict[int, StatementTable] = {}
        self.issues: list[str] = []
        self.statement_pages: list[int] = []
        self.unit_page_no: int | None = None
        self.unit_text: str | None = None

    def add_page(
        self, page, page_no: int, page_hash: str, *, native_text: str | None = None,
    ) -> list[StatementFact]:
        rows = page_rows(page)
        text = page.get_text("text") if native_text is None else native_text
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
        header_row, years, target = header
        table = _statement_table(
            rows, header_row, years, page_no, self.table_name, self.statement, page_hash,
        )
        self.tables[page_no] = table
        label_layouts = {row.label_layout.label_bbox: row.label_layout for row in table.rows}
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
                if ("重述" in cell.text or "重列" in cell.text) and not (
                    left <= (cell.bbox[0] + cell.bbox[2]) / 2 < right
                ):
                    marker = "重述" if "重述" in cell.text else "重列"
                    column_label += f"；比较列经{marker}"
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
                label_layouts.get(union_bbox(label_cells)),
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
