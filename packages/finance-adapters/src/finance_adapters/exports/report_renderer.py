from __future__ import annotations

import csv
import html
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import fitz
from finance_domain.analysis.contracts import AnalysisOutput
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.exports.reasoning import provider_reasoning
from finance_adapters.persistence.audit import stable_hash
from finance_adapters.persistence.metric_gateway import (
    FORMULA_VERSION,
    _snapshot,
    metric_facts,
)
from finance_adapters.persistence.models import (
    AnalysisRun,
    AnomalyObservation,
    AuditEvent,
    Company,
    EvidenceFragment,
    FilingVersion,
    FinancialFact,
    MetricResult,
)
from finance_adapters.providers.analysis_agent import current_workflow_snapshot
from finance_adapters.providers.llm_analysis import (
    _hash,
    build_evidence_package,
    split_claim_text,
    validate_analysis_output,
)
from finance_adapters.providers.reproduction import validate_saved_analysis_run

METRIC_NAMES = {
    "revenue": "营业收入",
    "operating_profit": "营业利润",
    "net_income_attributable": "归母净利润",
    "cashflow_operations": "经营活动现金流净额",
    "gross_margin": "毛利率",
    "net_margin": "归母净利率",
    "operating_cashflow_to_net_income": "经营现金流／合并净利润",
    "free_cash_flow": "自由现金流（购建长期资产现金代理）",
    "current_ratio": "流动比率",
    "debt_to_assets": "资产负债率",
    "simple_average_roa": "总资产净利率（合并净利润/期初末简单平均总资产）",
    "simple_average_parent_roe": "归母净资产收益率（期初末简单平均，非披露加权ROE）",
    "inventory_turnover": "存货周转率（营业成本/期初末简单平均存货）",
    "receivables_turnover": "应收账款周转率代理（营业收入/期初末平均应收账款净额，非赊销口径）",
    "asset_turnover": "总资产周转率（营业收入/期初末简单平均总资产）",
    "ebit_expensed_interest_proxy": "EBIT代理（利润总额加费用化利息，未含资本化利息）",
    "expensed_interest_coverage_proxy": "费用化利息保障倍数（EBIT代理/费用化利息，不含资本化利息）",
    "net_debt_cash_proxy": "净负债代理（指定五项债务减货币资金，含受限资金）",
    "quick_ratio_proxy": "速动比率代理（流动资产减存货，未扣预付款）",
}
CSS = """
body {font-family:Arial,'Microsoft YaHei',sans-serif;font-size:10pt;line-height:1.5;color:#202939}
h1 {font-size:22pt;color:#16355a;margin-bottom:12pt}
h2 {font-size:15pt;color:#16355a;margin-top:20pt;margin-bottom:8pt}
h3 {font-size:11pt;margin-bottom:5pt}
p,li {margin-top:4pt;margin-bottom:7pt}
table {border-collapse:collapse;width:100%;font-size:8pt;margin-top:8pt}
th,td {border:1px solid #bdc9d6;padding:6pt;text-align:left;vertical-align:top}
th {font-weight:bold}
.meta,.limitations {color:#4b5563;font-size:9pt}
.claim {margin-bottom:14pt}
.evidence {font-size:9pt}
footer {margin-top:22pt;font-size:9pt;color:#4b5563}
details {margin-top:8pt;margin-bottom:8pt}
pre.reasoning {white-space:pre-wrap;overflow-wrap:anywhere;font:9pt Arial,'Microsoft YaHei',sans-serif}
"""


def _escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _number(value: Decimal | None) -> str:
    if value is None:
        return "不可用"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _metric_unit(metric: MetricResult, facts: dict[int, FinancialFact]) -> str:
    if metric.metric_code in {"inventory_turnover", "receivables_turnover", "asset_turnover"}:
        return "次/年"
    if metric.metric_code in {"current_ratio", "quick_ratio_proxy", "operating_cashflow_to_net_income", "expensed_interest_coverage_proxy"}:
        return "倍"
    if metric.unit != "currency":
        return "比率（1 = 100%）" if metric.unit == "ratio" else metric.unit
    currencies = {facts[key].currency for key in metric.input_fact_ids if key in facts}
    currency = next(iter(currencies)) if len(currencies) == 1 else "CNY"
    return "人民币元（CNY）" if currency == "CNY" else f"{currency} 元"


def _source_unit(fact: FinancialFact) -> str:
    scale = {1: "元", 1000: "千元", 10000: "万元", 100000000: "亿元"}.get(
        fact.unit_scale, f"元 × {fact.unit_scale}"
    )
    return f"{'人民币' if fact.currency == 'CNY' else fact.currency}{scale}"


def _validated_inputs(session: Session, filing_id: int):
    filing = session.get(FilingVersion, filing_id)
    if filing is None:
        raise ValueError("EXPORT_FILING_NOT_FOUND")
    if filing.status not in {"facts_verified", "metrics_ready", "analysis_ready"}:
        raise ValueError("EXPORT_STRICT_INPUT_REQUIRED")
    metrics = list(
        session.scalars(
            select(MetricResult)
            .where(
                MetricResult.filing_version_id == filing_id,
                MetricResult.computation_mode == "strict_audited",
                MetricResult.applicability_status != "superseded",
                MetricResult.quality_status != "superseded",
            )
            .order_by(MetricResult.id)
        )
    )
    usable = [
        metric
        for metric in metrics
        if metric.applicability_status == "calculated" and metric.value is not None
    ]
    if any(metric.formula_version != FORMULA_VERSION for metric in metrics):
        raise ValueError("EXPORT_UNSUPPORTED_METRIC_FORMULA_VERSION")
    if not usable:
        raise ValueError("EXPORT_STRICT_METRICS_REQUIRED")
    analysis = session.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.filing_version_id == filing_id,
            AnalysisRun.status.in_(["completed", "partial_result", "waiting_for_documents"]),
            AnalysisRun.invalidated_at.is_(None),
        )
        .order_by(AnalysisRun.id.desc())
    )
    if analysis is None:
        raise ValueError("EXPORT_COMPLETED_ANALYSIS_REQUIRED")
    try:
        output = AnalysisOutput.model_validate(analysis.output)
    except ValueError as exc:
        raise ValueError("EXPORT_ANALYSIS_INVALID") from exc
    if not output.claims or any(not claim.content.strip() for claim in output.claims):
        raise ValueError("EXPORT_ANALYSIS_CLAIMS_REQUIRED")
    try:
        current_facts = metric_facts(session, filing)
    except ValueError as exc:
        raise ValueError(f"EXPORT_STRICT_INPUT_INVALID:{exc}") from exc
    if not current_facts:
        raise ValueError("EXPORT_STRICT_FACTS_REQUIRED")
    facts = {item.id: item for item in current_facts.values()}
    snapshot = _snapshot(current_facts)
    if any(
        metric.input_snapshot_hash != snapshot or not set(metric.input_fact_ids).issubset(facts)
        for metric in usable
    ):
        raise ValueError("EXPORT_INPUT_SNAPSHOT_CHANGED")
    if any(
        metric.quality_status != "strict_audited"
        or metric.scope != filing.scope
        or metric.period_basis != filing.period_basis
        for metric in usable
    ):
        raise ValueError("EXPORT_STRICT_METRICS_REQUIRED")
    try:
        package = build_evidence_package(session, filing_id)
    except ValueError as exc:
        if str(exc) in {"METRIC_INPUT_SNAPSHOT_CHANGED", "METRIC_RECOMPUTATION_MISMATCH"}:
            raise ValueError("EXPORT_INPUT_SNAPSHOT_CHANGED") from exc
        raise ValueError(f"EXPORT_EVIDENCE_PACKAGE_INVALID:{exc}") from exc
    if _hash(package) != analysis.input_snapshot_hash:
        raise ValueError("EXPORT_INPUT_SNAPSHOT_CHANGED")
    if output.agent_workflow is not None and stable_hash(
        current_workflow_snapshot(session, filing)
    ) != output.agent_workflow.document_snapshot_hash:
        raise ValueError("EXPORT_DOCUMENT_SNAPSHOT_CHANGED")
    try:
        validate_analysis_output(analysis.output, package)
    except ValueError as exc:
        raise ValueError(f"EXPORT_ANALYSIS_INVALID:{exc}") from exc
    try:
        validate_saved_analysis_run(analysis, package)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"EXPORT_ANALYSIS_PROVENANCE_INVALID:{exc}") from exc
    metric_ids = {item["id"] for item in package["metrics"]}
    evidence_ids = {item["id"] for item in package["evidence"]}
    if not evidence_ids or any(
        not set(claim.metric_result_ids).issubset(metric_ids)
        or not set(claim.evidence_fragment_ids).issubset(evidence_ids)
        for claim in output.claims
    ):
        raise ValueError("EXPORT_ANALYSIS_EVIDENCE_UNAVAILABLE")
    return filing, metrics, analysis, output, facts, evidence_ids, package


def _render_workflow_html(workflow) -> str:
    if workflow is None:
        return ""
    topics = {"non_recurring_items": "非经常性损益", "accounting_basis_changes": "会计口径变化",
              "profit_cashflow_relationship": "利润与经营现金流关系", "performance_changes": "业绩变化"}
    kinds = {"program_fact": "程序确认的事实", "company_disclosure": "公司原文披露",
             "analysis_inference": "分析推断", "evidence_insufficient": "证据不足的判断"}
    statuses = {"completed": "完成", "partial_result": "部分结果", "waiting_for_documents": "等待补充材料"}
    source_map = {item["id"]: item for item in workflow.sources}
    checks = "".join(
        f"<tr><td>{_escape(topics.get(item.topic, item.topic))}</td><td>{_escape(item.status)}</td>"
        f"<td>{_escape(item.coverage_note)}"
        f"{('<ul>' + ''.join('<li>' + _escape(obligation.get('label', obligation.get('id'))) + '：' + _escape(obligation.get('status')) + '；' + _escape(obligation.get('reason')) + '</li>' for obligation in item.obligations) + '</ul>') if item.obligations else ''}"
        f"</td></tr>" for item in workflow.mandatory_checks
    )
    findings = []
    for item in workflow.findings:
        references = []
        for source_id in item.source_ids:
            source = source_map.get(source_id, {})
            references.append(
                f"<li>来源 {_escape(source_id)}；文件 {_escape(source.get('filing_version_id'))}，"
                f"第 {_escape(source.get('page_no'))} 页；校验状态 {_escape(source.get('review_status', 'unknown'))}。"
                f"<br>{_escape(source.get('text', '')).replace(chr(10), '<br>')}"
                f"<br>原文 SHA-256：{_escape(source.get('text_sha256'))}</li>"
            )
        obligation_label = getattr(item, "obligation_id", None)
        obligation_text = f" · 子命题 {_escape(obligation_label)}" if obligation_label else ""
        semantic_review_label = None
        has_current_semantic_policy = bool(getattr(workflow, "semantic_policy", None))
        if (has_current_semantic_policy and item.evidence_kind == "company_disclosure"
                and obligation_label):
            for check in workflow.mandatory_checks:
                if check.topic != item.topic:
                    continue
                matching_obligation = next((obligation for obligation in check.obligations
                                            if obligation.get("id") == obligation_label
                                            and obligation.get("status") == "needs_review"
                                            and item.id in obligation.get("finding_ids", [])), None)
                if matching_obligation is not None:
                    semantic_review_label = matching_obligation.get("label", obligation_label)
                    break
        if semantic_review_label:
            semantic_review_notice = (
                f"<p class='meta'>该原文引用是否支持子命题“{_escape(semantic_review_label)}”尚未核验；"
                "本项仅为待核验候选，不构成该子命题结论。</p>"
            )
        elif has_current_semantic_policy and item.evidence_kind == "company_disclosure" and obligation_label:
            semantic_review_notice = (
                "<p class='meta'>系统未保存独立的子命题关联核验结果；该原文仅作为候选引用，"
                "不构成该子命题结论。</p>"
            )
        else:
            semantic_review_notice = ""
        item_assessments = []
        for assessment in getattr(item, "evidence_item_assessments", None) or []:
            value = assessment.model_dump() if hasattr(assessment, "model_dump") else assessment
            item_assessments.append(
                f"<li>清单项 {_escape(value.get('item_id'))}；处置 {_escape(value.get('status'))}："
                f"{_escape(value.get('reason'))}</li>"
            )
        item_assessment_html = ("<h4>逐项处置依据</h4><ul>" + "".join(item_assessments) + "</ul>"
                                if item_assessments else "")
        findings.append(
            f"<div class='claim'><h3>{_escape(topics.get(item.topic, item.topic))}{obligation_text} · "
            f"{_escape(kinds.get(item.evidence_kind, item.evidence_kind))}</h3>"
            f"<p>{_escape(item.statement)}</p>{semantic_review_notice}"
            f"<p>依据与判断边界：{_escape(item.rationale)}</p>"
            f"<p class='meta'>判断类别：{_escape(item.assessment)}；"
            f"计算引用：{_escape(item.calculation_ids)}；指标引用：{_escape(item.metric_result_ids)}</p>"
            f"<ul>{''.join(references)}</ul>{item_assessment_html}</div>"
        )
    requirements = "".join(
        f"<li>{_escape(topics.get(item.topic, item.topic))}：{_escape(item.purpose)}；"
        f"期间 {_escape(item.period_start)} 至 {_escape(item.period_end)}；"
        f"所需科目：{_escape(item.concept_codes)}；状态：{_escape(item.status)}；"
        f"可接受替代材料：{_escape('；'.join(item.acceptable_alternatives))}</li>"
        for item in workflow.material_requirements
    ) or "<li>当前没有登记待补材料。</li>"
    calculations = "".join(
        f"<li>计算 {_escape(item.get('id'))}：{_escape(item)}</li>" for item in workflow.calculations
    ) or "<li>本轮没有新增比较计算；不能据此推定没有财务异常。</li>"
    return (
        f"<h2>多轮财务分析：{_escape(statuses.get(workflow.status, workflow.status))}</h2>"
        f"<p>停止原因：{_escape(workflow.stop_reason)}；已执行 {_escape(workflow.completed_rounds)} 轮。</p>"
        f"<table><tr><th>必查主题</th><th>状态</th><th>证据覆盖与限制</th></tr>{checks}</table>"
        + _render_evidence_coverage_html(getattr(workflow, "evidence_coverage", None))
        + "".join(findings)
        + f"<h3>比较计算与输入依据</h3><ul>{calculations}</ul>"
        + f"<h3>补充材料与待校验事项</h3><ul>{requirements}</ul>"
        + f"<p class='meta'>检查点：{_escape(workflow.checkpoint_id)}；材料快照 SHA-256："
        f"{_escape(workflow.document_snapshot_hash)}。仅对已执行检查和已读取证据声明分析范围。</p>"
    )


def _render_evidence_coverage_html(coverage: dict | None) -> str:
    if not isinstance(coverage, dict):
        return ""
    manifest = coverage.get("manifest") or {}
    states = {item.get("item_id"): item for item in coverage.get("item_states", [])}
    pending = set(coverage.get("pending_item_ids", []))
    rows = []
    for item in manifest.get("items", []):
        state = states.get(item.get("id"), {})
        period = item.get("period") or {}
        period_label = f"{period.get('period_start') or 'unknown'} 至 {period.get('period_end') or 'unknown'}"
        quoted_ids = state.get("presented_finding_ids", [])
        disposition_ids = state.get("assessment_finding_ids", [])
        disposition = "、".join(state.get("disposition_statuses", [])) or "pending（未逐项处置）"
        if item.get("id") in pending:
            disposition += "；pending（原文呈现或逐项处置待补）"
        references = "；".join(
            f"{ref.get('source_id')} · 第 {ref.get('page_no')} 页" for ref in item.get("source_refs", [])
        )
        rows.append(
            f"<tr><td>{_escape(item.get('raw_label'))}<br><span class='meta'>{_escape(item.get('id'))}</span></td>"
            f"<td>{_escape(item.get('raw_amount') if item.get('raw_amount') is not None else 'unknown')}</td>"
            f"<td>{_escape(period_label)}<br>{_escape(item.get('column_label') or 'unknown')}</td>"
            f"<td>{_escape(item.get('unit') or 'unknown（原单位未定位）')}</td>"
            f"<td>{'是' if state.get('ever_sent') else '否'}；当前可见：{'是' if state.get('currently_visible') else '否'}</td>"
            f"<td>{'是' if quoted_ids else '否'}<br>{_escape('、'.join(quoted_ids))}</td>"
            f"<td>{_escape(disposition)}<br>{_escape('、'.join(disposition_ids))}</td>"
            f"<td>{_escape(state.get('review_status', item.get('review_status', 'unknown')))}"
            f"<br>文件 {_escape(item.get('filing_version_id'))}；{_escape(references)}</td></tr>"
        )
    issues = []
    for group in manifest.get("groups", []):
        problems = "；".join(group.get("issues", [])) or "当前取证表格范围未登记闭合问题"
        issues.append(
            f"<li>文件 {_escape(group.get('filing_version_id'))}；清单状态 {_escape(group.get('status'))}；"
            f"原单位状态 {_escape(group.get('unit_status', 'unknown'))}；{_escape(problems)}"
            f"<br>表内核对：{_escape(group.get('reconciliation', []))}</li>"
        )
    verified = "通过" if coverage.get("manifest_verified") else "未通过（分母尚不可靠）"
    ratio = coverage.get("coverage_ratio")
    ratio_label = (f"{ratio:.1%}" if coverage.get("manifest_verified")
                   and isinstance(ratio, (float, int)) else "不可计算")
    return (
        "<details><summary>逐项证据呈现与处置清单</summary>"
        f"<p>清单 {_escape(coverage.get('required_count', 0))} 项；原文呈现 {_escape(coverage.get('presented_count', 0))} 项；"
        f"显式处置 {_escape(coverage.get('explicitly_addressed_count', 0))} 项；待补 {len(pending)} 项。"
        f"表格闭合检查：{verified}；呈现及处置覆盖率：{_escape(ratio_label)}。</p>"
        f"<p>逐项讨论 {_escape(coverage.get('discussed_count', 0))} 项；"
        f"明确证据不足 {_escape(coverage.get('insufficient_evidence_count', 0))} 项；"
        f"语义核验状态：{_escape(coverage.get('semantic_analysis_status', 'requires_semantic_review'))}（仍须复核）。</p>"
        "<p class='meta'>覆盖率仅度量可观察的原文呈现和逐项处置，不代表模型内部理解或事实已通过正式校验。"
        "逐项原文引用按完整原文行呈现统计；来源页的泛引用不算逐项引用。原单位 unknown 时不得自动换算。</p>"
        "<table><tr><th>原文项目</th><th>原金额</th><th>期间与原列名</th><th>原单位</th>"
        "<th>已发给模型</th><th>逐项原文引用</th><th>逐项处置</th><th>校验状态与来源</th></tr>"
        + "".join(rows) + "</table><h4>清单闭合问题与表内核对</h4><ul>"
        + "".join(issues) + "</ul>"
        f"<p class='meta'>清单 SHA-256：{_escape(coverage.get('manifest_hash'))}；"
        f"{_escape(coverage.get('boundary', '仅覆盖已取证范围，未完成项仍为待校验。'))}</p></details>"
    )


def _render_reasoning_html(trace: dict) -> str:
    projection = provider_reasoning(trace)
    if not projection["calls"]:
        return "<h3>供应商返回的思考文本</h3><p>尚无已保存的模型调用。</p>"
    blocks = []
    labels = {"returned": "已返回思考文本", "disabled": "本次请求已关闭思考；未返回思考正文",
              "not_returned": "供应商未返回思考正文"}
    for call in projection["calls"]:
        fields = "".join(
            f"<p class='meta'>字段：{_escape(field['field'])}</p>"
            f"<pre class='reasoning'>{_escape(field['content'])}</pre>" for field in call["fields"]
        )
        blocks.append(
            f"<details><summary>{_escape(call['stage'])} · 第 {call['call_index']} 次 · "
            f"{labels[call['status']]}</summary>{fields}</details>"
        )
    return "<h3>供应商返回的思考文本</h3>" + "".join(blocks)


def _render_html(
    filing, company, metrics, analysis, output, facts, evidence, anomalies, package,
    *, test_waiver: bool = False,
) -> str:
    metric_rows = "".join(
        f"<tr><td>{_escape(METRIC_NAMES.get(item.metric_code, item.metric_code))}"
        f"<br>{_escape(item.metric_code)}</td><td>{_escape(_number(item.value))}</td>"
        f"<td>{_escape(_metric_unit(item, facts))}</td>"
        f"<td>{_escape(item.applicability_status)}"
        f"{('<br>' + _escape(item.explanation_code)) if item.explanation_code else ''}</td>"
        f"<td>{_escape(item.formula_version)}</td></tr>"
        for item in metrics
    )
    separated = [(claim, split_claim_text(claim, package)) for claim in output.claims]
    claim_rows = "".join(
        f'<div class="claim"><h3>指标观察 {index}</h3>'
        f"<p>{_escape(text['numeric_content'])}</p>"
        f"<p>依据：{_escape(text['numeric_rationale'])}</p>"
        f'<p class="meta">口径：{_escape(claim.scope)}；'
        f"结论类型：{_escape(claim.claim_class)}；"
        f"{'需要进一步校验；' if claim.review_required else ''}"
        f"指标编号：{_escape(', '.join(map(str, claim.metric_result_ids)) or '无')}；"
        f"证据编号：{_escape(', '.join(map(str, claim.evidence_fragment_ids)) or '无')}"
        f"</p></div>"
        for index, (claim, text) in enumerate(separated, 1)
    )
    review_rows = (
        "".join(
            f'<div class="claim"><h3>待语义校验：观点 {index}</h3>'
            f'<p class="meta">模型定性文字未做语义核验。</p>'
            f"<p>{_escape(text['qualitative_content'])}</p>"
            f"<p>{_escape(text['qualitative_rationale'])}</p></div>"
            for index, (claim, text) in enumerate(separated, 1)
            if text["qualitative_content"] or text["qualitative_rationale"]
        )
        or "<p>没有模型定性观点。</p>"
    )
    evidence_rows = "".join(
        f'<li id="evidence-{item.id}">证据 {item.id}：PDF 第 {item.page_no} 页；'
        f"表名：{_escape(item.table_name)}；科目：{_escape(item.raw_label)}；"
        f"列：{_escape(item.column_locator)}；原始金额：{_escape(item.raw_value)}；"
        f"原文单位：{_escape(_source_unit(facts[item.fact_id]))}。</li>"
        for item in evidence
    )
    anomaly_rows = (
        "".join(
            f"<li>{_escape(item.rule_code)}：{_escape(item.status)}；"
            f"{_escape(item.explanation)}</li>"
            for item in anomalies
        )
        or "<li>无异常观察记录。</li>"
    )
    limitations = "".join(f"<li>{_escape(item)}</li>" for item in output.limitations)
    trace = analysis.tool_trace or {}
    reasoning_html = _render_reasoning_html(trace)
    attempts = trace.get("attempts", [])
    execution_rows = "".join(
        f"<tr><td>{_escape(item.get('attempt'))}</td>"
        f"<td>{_escape(item.get('provider_response_id'))}</td>"
        f"<td>{_escape(item.get('status'))}</td>"
        f"<td>{_escape(item.get('provider_metadata', {}).get('duration_seconds'))}</td>"
        f"<td>{_escape((item.get('provider_metadata', {}).get('usage') or {}).get('total_tokens'))}</td>"
        f"<td>{'有供应商暴露文本' if item.get('provider_metadata', {}).get('reasoning_output') else '未返回思考正文'}</td></tr>"
        for item in attempts
    )
    context = package.get("document_context", {})
    context_label = "OCR上下文" if str(context.get("extractor_version", "")).startswith("ocr") else "原生文字上下文"
    selected_sources = {
        assertion.source_id for claim in output.claims for assertion in claim.source_assertions
    }
    context_rows = "".join(
        f"<li>{context_label} {_escape(item['id'])}，PDF第{item['page_no']}页，"
        f"上下文校验状态：{_escape(item.get('selection_status') or 'unknown')}；"
        f"选项：{_escape(item.get('selected_answer') or item.get('selection_status'))}。"
        f"<br>{_escape(item['text']).replace(chr(10), '<br>')}</li>"
        for item in context.get("excerpts", []) if item["id"] in selected_sources
    ) or "<li>本次模型没有引用文字上下文。</li>"
    review_summary = _escape(output.qualitative_summary) or "模型未提供定性总结。"
    review_mode = ("用户授权的测试豁免：未完成测试金标准评估，不构成正式校验通过。"
                   if test_waiver else "按当前自动校验状态生成；具体校验记录见审计轨迹。")
    scope = {"consolidated": "合并口径", "parent": "母公司口径"}.get(filing.scope, filing.scope)
    generated = datetime.now(UTC).isoformat()
    workflow_html = _render_workflow_html(output.agent_workflow)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>FinReport 财报分析报告</title>
<style>{CSS}</style></head><body>
<h1>财报分析报告</h1>
<p><strong>{review_mode}</strong></p>
<p class="meta">自动校验仅覆盖指标数值、公式与引用。模型定性文字未做语义核验，列于待语义校验区。</p>
<p>公司：{_escape(company.name if company else filing.company_id)}；股票代码：{_escape(company.stock_code if company else "未知")}</p>
<p>报告期间：{filing.period_start.isoformat()} 至 {filing.period_end.isoformat()}；{_escape(scope)}；报告类型：{_escape(filing.report_type)}</p>
<p class="meta">披露版本：{filing.id}；分析运行：{analysis.id}；生成时间（UTC）：{generated}</p>
<p class="meta">模型：{_escape(analysis.model)}；模型版本：{_escape(analysis.model_version)}；Prompt：{_escape(analysis.prompt_version)}；编排：{_escape(analysis.orchestration_version)}；验证器：{_escape(analysis.validator_version)}</p>
<h2>总体指标结论</h2><p>{_escape(output.overall_conclusion)}</p>
<h2>结构化指标</h2><p class="meta">金额指标已按原文单位倍率换算为元；比率 1 对应 100%。不可用指标保留原因。</p>
<table><tr><th>指标</th><th>值</th><th>单位</th><th>状态</th><th>公式版本</th></tr>{metric_rows}</table>
<h2>已核验指标观察与依据</h2>{claim_rows}
{workflow_html}
<h2>异常观察</h2><ul>{anomaly_rows}</ul>
<h2>证据索引</h2><ul class="evidence">{evidence_rows}</ul>
<h2>模型定性观点：待语义校验</h2>
<p class="meta">模型自由总结未做语义核验，未作为报告总体结论输出；定性观点可能与指标矛盾。</p>
<h3>模型定性总结</h3><p>{review_summary}</p>
{review_rows}
<h2>过程透明与复现</h2>
<p>输入快照 SHA-256：{_escape(analysis.input_snapshot_hash)}<br>工具轨迹 SHA-256：{_escape(analysis.tool_trace_hash)}</p>
<p>程序扫描页数：{_escape(context.get('total_pages', '不可用'))}；实际模型摘录页码：{_escape(context.get('selected_page_numbers', []))}；摘录行数：{_escape(context.get('model_input_line_count', 0))}。只对实际摘录声明模型输入覆盖。</p>
<p>候选摘录：{_escape(context.get('candidate_excerpt_count', 0))}；因预算省略：{_escape(context.get('omitted_excerpt_count', 0))}；不完整问项：{_escape(context.get('incomplete_question_count', 0))}。不完整问项禁止支撑独立定性断言；跨页问项仍需补充证据校验。</p>
<p class="meta">省略原因：{_escape(context.get('omission_reasons', {}))}</p>
<table><tr><th>调用</th><th>供应商响应ID</th><th>校验结果</th><th>耗时秒</th><th>总tokens</th><th>思考字段</th></tr>{execution_rows}</table>
{reasoning_html}
<p class="meta">供应商暴露的思考文本不等于完整内部推理，也不证明财务结论正确。原始请求、响应、失败校验和渲染前后对照保存在复现包中。</p>
<p>离线复现包接口：/api/v1/analyses/{analysis.id}/reproduction。离线重放校验原有输入输出；再次联网调用模型不保证逐字相同。哈希不能防止数据库管理员同时改写数据与哈希。</p>
<h3>模型引用的{context_label}</h3><ul class="evidence">{context_rows}</ul>
<h2>分析边界与限制</h2><ul class="limitations">{limitations}</ul>
<footer>仅供研究与教学，不构成投资建议。分析状态：{_escape(analysis.status)}；导出与当前输入快照核对一致，未完成事项保留在材料清单中。</footer>
</body></html>"""


def _write_pdf(page: str, target: Path) -> None:
    # Reuse local fonts; MuPDF's bundled CJK fallback needs no runtime download.
    font_folder = Path(os.getenv("WINDIR", r"C:\Windows")) / "Fonts"
    font = next(
        (
            font_folder / name
            for name in ("msyh.ttc", "simsun.ttc")
            if (font_folder / name).is_file()
        ),
        None,
    )
    archive = fitz.Archive(str(font.parent)) if font else None
    font_css = (
        f"@font-face {{font-family:ReportCJK;src:url({font.name});}}"
        "body {font-family:ReportCJK,sans-serif;}"
        if font
        else "body {font-family:sans-serif;}"
    )
    story = fitz.Story(page, user_css=font_css, archive=archive)
    media = fitz.paper_rect("a4")
    content = media + (36, 36, -36, -42)
    document = story.write_with_links(lambda _number, _filled: (media, content, None))
    try:
        if not document.page_count:
            raise ValueError("EXPORT_PDF_EMPTY")
        for index, pdf_page in enumerate(document):
            pdf_page.insert_text(
                (36, 20),
                "定性内容待语义校验；自动校验仅覆盖指标、公式及引用。",
                fontname="ReportReview" if font else "china-s",
                fontfile=str(font) if font else None,
                fontsize=7.5,
                color=(0.35, 0.4, 0.45),
            )
            pdf_page.insert_text(
                (36, media.height - 23),
                f"FinReport | {index + 1} / {document.page_count}",
                fontsize=8,
                color=(0.35, 0.4, 0.45),
            )
        document.subset_fonts()
        document.save(target, garbage=4, deflate=True)
    finally:
        document.close()


def generate_export(session: Session, filing_id: int, output_format: str) -> Path:
    if output_format not in {"html", "pdf", "csv"}:
        raise ValueError("EXPORT_FORMAT_UNSUPPORTED")
    filing, metrics, analysis, output, facts, evidence_ids, package = _validated_inputs(
        session, filing_id
    )
    waiver = session.scalar(select(AuditEvent.id).where(
        AuditEvent.filing_version_id == filing_id,
        AuditEvent.action == "user_authorized_test_review_waiver",
    ))
    company = session.get(Company, filing.company_id)
    evidence = list(
        session.scalars(
            select(EvidenceFragment)
            .where(EvidenceFragment.id.in_(evidence_ids))
            .order_by(EvidenceFragment.page_no, EvidenceFragment.id)
        )
    )
    anomalies = list(
        session.scalars(
            select(AnomalyObservation)
            .where(AnomalyObservation.filing_version_id == filing_id)
            .order_by(AnomalyObservation.id)
        )
    )
    folder = settings.storage_dir / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    base = folder / f"filing-{filing_id}-{timestamp}-{uuid4().hex[:8]}"
    target = base.with_suffix(f".{output_format}")
    if output_format == "csv":
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "company_name",
                    "stock_code",
                    "period_start",
                    "period_end",
                    "scope",
                    "filing_version_id",
                    "analysis_run_id",
                    "analysis_snapshot_hash",
                    "metric_code",
                    "value",
                    "unit",
                    "status",
                    "formula_version",
                    "input_snapshot_hash",
                    "review_mode",
                ]
            )
            writer.writerows(
                [
                    [
                        company.name if company else filing.company_id,
                        company.stock_code if company else "",
                        filing.period_start,
                        filing.period_end,
                        filing.scope,
                        filing.id,
                        analysis.id,
                        analysis.input_snapshot_hash,
                        item.metric_code,
                        _number(item.value),
                        _metric_unit(item, facts),
                        item.applicability_status,
                        item.formula_version,
                        item.input_snapshot_hash,
                        "user_authorized_test_only" if waiver else "standard_review_state",
                    ]
                    for item in metrics
                ]
            )
        return target
    page = _render_html(
        filing, company, metrics, analysis, output, facts, evidence, anomalies, package,
        test_waiver=waiver is not None,
    )
    if output_format == "html":
        target.write_text(page, encoding="utf-8")
    else:
        _write_pdf(page, target)
    return target
