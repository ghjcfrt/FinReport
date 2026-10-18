"""Bounded semantic checks for high-risk financial statement rewrites.

Passing these rules is not a general semantic validation or an approval of the
underlying financial analysis. Findings retain the exact source span used by
each deterministic check so a reviewer can audit both the trigger and input.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

RULE_VERSION = "financial_semantic_guards_v1"

_FINANCIAL_ASSET_RELATION = re.compile(
    r"非金融企业.{0,80}?持有\s*金融资产和金融负债", re.DOTALL
)
_ASSET_CLASS = re.compile(r"非金融(?:资产|负债)(?:类|类别|项目|属性)?")
_CLASSIFICATION_BEFORE = re.compile(
    r"(?:为|是|属于|归为|划分为|列为|作为|定性为)\s*$"
)
_CLASSIFICATION_AFTER = re.compile(r"(?:构成行|构成项|类别|项目|属性)")
_NEGATION = re.compile(
    r"并非|并不|不是|不属于|不能|不可|不应(?:当)?|不得|未(?:被|能|有|曾)?|没有|不"
)
_SCOPE_BREAK = re.compile(r"[。！？；\n]|但是|然而|不过|但|却|而是")
_SIMPLE_NEGATED_SUFFIX = re.compile(r"^(?:的)?(?:说法|表述)?(?:错误|不正确|不成立)")
_NEGATED_RELATION = re.compile(
    r"(?:不能|不可|不应(?:当)?|未能|不可以)将.{0,18}"
    r"(?:理解为|视为|看作|认定为|归类为|划分为)$"
)

_CASHFLOW_BRIDGE = re.compile(
    r"将净利润调节为经营活动现金流量|现金流量表补充资料|现金流量补充资料|"
    r"经营性(?:应收|应付)项目的变动"
)
_BUSINESS_DRIVER = (
    r"客户回款|销售回款|回款周期|供应商结算|采购付款|销量|售价|销售价格|"
    r"订单需求|销售结构|产品结构|客户需求|产销|市场需求|渠道|账期|信用政策|"
    r"应收账款周转|应付账款周转|采购周期"
)
_CAUSE_PATTERNS = tuple(re.compile(pattern) for pattern in (
    (rf"(?:{_BUSINESS_DRIVER}).{{0,16}}(?:导致|造成|拖累|推动|使得?|致使|带来|解释)"
     r".{0,24}(?:经营活动现金流|经营现金流|现金流)"),
    (rf"(?:经营活动现金流|经营现金流|现金流).{{0,30}}"
     rf"(?:原因是|主要由于|由于|因为|源于|来自|反映|说明|表明|系).{{0,24}}(?:{_BUSINESS_DRIVER})"),
    rf"(?:现金流|回款).{{0,20}}(?:因|由于|因为|源于).{{0,20}}(?:{_BUSINESS_DRIVER})",
    (rf"(?:{_BUSINESS_DRIVER}).{{0,20}}(?:使|导致|造成|拖累|影响).{{0,20}}"
     r"(?:现金流|经营活动现金流|经营现金流)"),
))
_LIMITATION_NEAR_CAUSE = re.compile(
    r"(?:不能|无法|未能|不足以|未必|不一定|尚不能|不能据此|无法据此|"
    r"不能确认|无法确认|尚无法确认|未见证据|没有证据|没有.{0,8}(?:证明|证实|确认)).{0,32}$"
)
_DOUBLE_NEGATION = re.compile(r"不能否认|不可否认|并非不|不是不|不能不|没有不|并不是不")
_OTHER_ASSET_SUBJECT = re.compile(r"固定资产|无形资产|投资性房地产|生物资产|存货|非流动性资产处置")
_CAUSAL_DRIVER_FAMILIES = tuple(re.compile(pattern) for pattern in (
    r"客户回款|销售回款|回款周期", r"供应商结算|采购付款|采购周期",
    r"销量|产销", r"售价|销售价格", r"订单需求|客户需求|市场需求",
    r"销售结构|产品结构", r"渠道", r"账期|信用政策",
    r"应收账款周转", r"应付账款周转",
))


class FinancialSemanticError(ValueError):
    """A bounded financial semantic rule rejected a model candidate."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        super().__init__(issues[0]["code"] if issues else "FINANCIAL_SEMANTIC_INVALID")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {}


def _normalize_sources(sources: Mapping[str, Any] | list[Any]) -> dict[str, dict[str, Any]]:
    if isinstance(sources, Mapping):
        items = sources.items()
    elif isinstance(sources, list):
        items = ((None, item) for item in sources)
    else:
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, raw in items:
        source = _as_dict(raw)
        source_id = source.get("id") or key
        if source_id is not None:
            normalized[str(source_id)] = source
    return normalized


def _candidate_text_fields(candidate: dict[str, Any]):
    for field in ("statement", "rationale"):
        value = candidate.get(field)
        if isinstance(value, str) and value:
            yield field, value
    assessments = candidate.get("evidence_item_assessments") or []
    if isinstance(assessments, list):
        for index, assessment in enumerate(assessments):
            item = _as_dict(assessment)
            reason = item.get("reason")
            if isinstance(reason, str) and reason:
                yield f"evidence_item_assessments[{index}].reason", reason


def _cited_sources(candidate: dict[str, Any], source_map: dict[str, dict[str, Any]]):
    source_ids = candidate.get("source_ids") or []
    if not isinstance(source_ids, list):
        return []
    return [(str(source_id), source_map[str(source_id)])
            for source_id in source_ids if str(source_id) in source_map]


def _source_text(source: dict[str, Any]) -> str:
    value = source.get("text") or source.get("content") or source.get("excerpt")
    return value if isinstance(value, str) else ""


def _source_evidence(source_id: str, source: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    text = _source_text(source)
    return {
        "source_id": source_id,
        "text_sha256": source.get("text_sha256") or hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "page_no": source.get("page_no"),
        "start": start,
        "end": end,
        "text": text[start:end],
    }


def _quoted_source_statement(candidate: dict[str, Any], field: str, value: str,
                             cited: list[tuple[str, dict[str, Any]]]) -> bool:
    if field != "statement" or candidate.get("evidence_kind") != "company_disclosure":
        return False
    statement = value.removeprefix("公司披露：").strip()
    return any(statement and statement in _source_text(source) for _, source in cited)


def _clause_prefix(value: str, position: int) -> str:
    start = 0
    for match in _SCOPE_BREAK.finditer(value, 0, position):
        start = match.end()
    return value[start:position]


def _is_negated_classification(value: str, start: int, end: int) -> bool:
    prefix = _clause_prefix(value, start)
    suffix = value[end:]
    if _SIMPLE_NEGATED_SUFFIX.match(suffix):
        return True
    if _NEGATED_RELATION.search(prefix):
        return len(_NEGATION.findall(prefix)) == 1
    negations = _NEGATION.findall(prefix[-24:])
    return len(negations) == 1 and bool(re.search(
        r"(?:并非|并不|不是|不属于|不能|不可|不应(?:当)?|不得|未(?:被|能|有|曾)?|没有|不)$",
        prefix,
    ))


def _classification_targets_financial_relation(candidate: dict[str, Any], value: str,
                                               position: int) -> bool:
    prefix = _clause_prefix(value, position)
    # Explicitly named other objects remain independent even when their source
    # chunk or finding also contains a financial-asset line.
    last_separator = max(prefix.rfind("，"), prefix.rfind(","))
    local_subject = prefix[last_separator + 1:]
    if _OTHER_ASSET_SUBJECT.search(local_subject):
        return False
    if re.search(r"(?<!非)金融资产|(?<!非)金融负债|非金融企业", prefix):
        return True
    statement = str(candidate.get("statement") or "")
    has_referent = bool(re.search(r"(?<!非)金融资产|(?<!非)金融负债|非金融企业", statement))
    return has_referent and bool(re.search(
        r"该(?:行|项|项目|对象)|本行|上述(?:行|项目)|前述(?:行|项目)|^(?:这|其|是|为|属于|归为)",
        local_subject,
    ))


def _classifications(value: str):
    for match in _ASSET_CLASS.finditer(value):
        before = value[max(0, match.start() - 16):match.start()]
        after = value[match.end():match.end() + 12]
        if _CLASSIFICATION_BEFORE.search(before) or _CLASSIFICATION_AFTER.match(after):
            yield match


def _is_limited_causal_claim(value: str, start: int, end: int) -> bool:
    prefix = _clause_prefix(value, start)
    scope = prefix + value[start:end]
    if _DOUBLE_NEGATION.search(scope):
        return False
    if re.search(r"并非(?:因为|由于|源于)|不是(?:因为|由于|源于)", scope):
        return True
    return bool(_LIMITATION_NEAR_CAUSE.search(prefix[-48:]))


def _business_causal_assertion(value: str):
    clause_start = 0
    breaks = list(_SCOPE_BREAK.finditer(value))
    for separator in [*breaks, None]:
        clause_end = separator.start() if separator else len(value)
        for pattern in _CAUSE_PATTERNS:
            for match in pattern.finditer(value, clause_start, clause_end):
                if not _is_limited_causal_claim(value, match.start(), match.end()):
                    return match
        clause_start = separator.end() if separator else len(value)
    return None


def _make_issue(code: str, field: str, value: str, start: int, end: int,
                rule_note: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    left, right = max(0, start - 80), min(len(value), end + 120)
    return {
        "code": code,
        "field": field,
        "excerpt": value[left:right],
        "source_evidence": evidence,
        "rule_version": RULE_VERSION,
        "rule_note": rule_note,
    }


def semantic_issues(candidate: Any, sources: Mapping[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Return auditable issues for a narrow set of high-risk financial claims."""
    finding = _as_dict(candidate)
    source_map = _normalize_sources(sources)
    cited = _cited_sources(finding, source_map)
    fields = list(_candidate_text_fields(finding))
    issues: list[dict[str, Any]] = []

    financial_relation_sources = []
    bridge_sources = []
    direct_causal_sources = []
    for source_id, source in cited:
        source_text = _source_text(source)
        relation = _FINANCIAL_ASSET_RELATION.search(source_text)
        if relation:
            financial_relation_sources.append((source_id, source, relation))
        bridge = _CASHFLOW_BRIDGE.search(source_text)
        if bridge:
            bridge_sources.append((source_id, source, bridge))
        direct_claim = _business_causal_assertion(source_text)
        if direct_claim and re.search(
            r"经营活动现金流|经营现金流", source_text
        ):
            direct_causal_sources.append(direct_claim.group())

    if financial_relation_sources:
        relation_evidence = [
            _source_evidence(source_id, source, relation.start(), relation.end())
            for source_id, source, relation in financial_relation_sources
        ]
        for field, value in fields:
            if _quoted_source_statement(finding, field, value, cited):
                continue
            for match in _classifications(value):
                if not _classification_targets_financial_relation(finding, value, match.start()):
                    continue
                if _is_negated_classification(value, match.start(), match.end()):
                    continue
                issues.append(_make_issue(
                    "FINANCIAL_OBJECT_TYPE_MISMATCH", field, value, match.start(), match.end(),
                    "来源称非金融企业持有金融资产和金融负债；候选将对象改写为非金融资产或负债。",
                    relation_evidence,
                ))
            for match in re.finditer(r"(?<!非)金融企业.{0,20}持有\s*金融资产和金融负债", value):
                if _is_negated_classification(value, match.start(), match.end()):
                    continue
                issues.append(_make_issue(
                    "FINANCIAL_SUBJECT_TYPE_MISMATCH", field, value, match.start(), match.end(),
                    "来源主体为非金融企业；候选将其改写为金融企业。",
                    relation_evidence,
                ))

    if bridge_sources:
        bridge_evidence = [
            _source_evidence(source_id, source, bridge.start(), bridge.end())
            for source_id, source, bridge in bridge_sources
        ]
        for field, value in fields:
            if _quoted_source_statement(finding, field, value, cited):
                continue
            match = _business_causal_assertion(value)
            if match:
                drivers = [pattern for pattern in _CAUSAL_DRIVER_FAMILIES
                           if pattern.search(match.group())]
                if drivers and any(all(driver.search(disclosure) for driver in drivers)
                                   for disclosure in direct_causal_sources):
                    continue
                issues.append(_make_issue(
                    "CASHFLOW_BRIDGE_AS_BUSINESS_CAUSE", field, value, match.start(), match.end(),
                    "现金流调节表只能支持会计桥接；该候选将其表述为未经直接披露支持的业务原因。",
                    bridge_evidence,
                ))

    return issues


def validate_financial_semantics(candidate: Any, sources: Mapping[str, Any] | list[Any]) -> None:
    """Raise with the complete auditable issue list; never rewrite candidate text."""
    issues = semantic_issues(candidate, sources)
    if issues:
        raise FinancialSemanticError(issues)
