# 开源分析扩展接入与离线验证

记录日期：2026-10-19。此次接入 Guardrails AI、Outlines、Semantica 和 DSPy，分别处理输出协议检查、结构输出参数构造、来源关系记录和冻结结果评测。四者均使用真实 SDK；本地试验及回放新增 LLM 调用为 **0**，另以 **1 次、49 tokens** 的微型请求验证当前中转站的 JSON Schema 兼容性，未发送财报正文、未执行模型训练，也未重新提取四份 PDF 全文。

## 1. 组件分工和当前边界

| 组件 | 固定版本 / extra | 当前接入 | 当前可支持的判断 |
|---|---|---|---|
| Guardrails AI | `guardrails-ai==0.11.0` / `output-validation` | 本地检查 Agent action envelope，保存输入、Schema 和校验回执的哈希 | 判断协议外壳是否合法；候选的财务跨字段规则、引用和语义仍由既有逻辑独立处理 |
| Outlines | `outlines[openai]==1.3.3` / `structured-output` | 真实 `OpenAITypeAdapter` / `JsonSchema` 构造 OpenAI 兼容接口的 `response_format` | 当前中转站微型兼容请求已通过；不将单个 Schema 的成功推广为所有模型和复杂 Schema 都支持约束 |
| Semantica | `semantica==0.7.0` / `semantic` | 真实 provenance 存储、引用关系和校验和检查；每个发行人、财报版本各建一张图 | 来源和候选的关系可追溯；图连接本身不证明原文能够支持对应财务结论 |
| DSPy | `dspy==3.4.1` / `evaluation` | 真实 `Example`、`Module`、`Evaluate` 评测冻结预测和显式标签 | 比较保存结果与指定参考的类别、数值、来源和接纳状态；尚未开展提示优化或训练 |
| OntoGPT | 暂不加入依赖 | 暂缓接入 | 待明确财务实体、会计事项本体、抽取 Schema、独立标签及调用预算后单独评估 |

本次没有得到“中文会计语义误读已降低”的独立评测证据。上述模块也不替代财务计算、披露范围认证、证据充分性检查或补件判断。Semantica 当前是来源关系侧车图，未作为新的检索器向模型补充上下文；DSPy 当前是离线评测入口。

计划书提出的 `auto_passed / needs_review / rejected / candidate` 正式自动校验状态机，**尚未在本轮迁移完成**。现有运行和历史工件中的 `pending_review` 等状态继续按原含义保留；本轮扩展不会把历史候选升级为自动校验通过的严格事实。

## 2. 安装、启用和关闭

从项目根目录执行：

```powershell
uv sync --frozen --extra output-validation --extra structured-output --extra semantic --extra evaluation
```

本次真实验证环境为 Windows、Python 3.11.15。当前依赖声明对 Guardrails AI 和 Semantica 设置 `python_version < '3.14'`，对 DSPy 设置 `< '3.16'`；使用全部扩展时请选择 Python 3.11–3.13。更高 Python 版本中的条件跳过不代表这些组件已经安装或验证。

扩展默认关闭，安装依赖后仍需显式启用。以下设置只影响当前 PowerShell 启动的后续进程；API 已运行时需要按既有启动方式重新加载配置。

```powershell
$env:FINREPORT_OUTPUT_GUARD_ENABLED = "true"
$env:FINREPORT_SEMANTIC_GRAPH_ENABLED = "true"
$env:FINREPORT_LLM_STRUCTURED_OUTPUT_MODE = "json_schema"
$env:FINREPORT_LLM_SCHEMA_BUILDER = "outlines"
```

也可将同名配置写入既有 `.env`。接口地址、模型和密钥仍使用现有模型配置，本地扩展不要求新增供应商密钥。DSPy 没有服务端常驻开关，仅在运行评测命令时使用。

恢复默认行为：

```powershell
$env:FINREPORT_OUTPUT_GUARD_ENABLED = "false"
$env:FINREPORT_SEMANTIC_GRAPH_ENABLED = "false"
$env:FINREPORT_LLM_STRUCTURED_OUTPUT_MODE = "json_object"
$env:FINREPORT_LLM_SCHEMA_BUILDER = "native"
```

### 2.1 Guardrails AI 的具体准入范围

启用时先检查依赖与本地校验能力，再对每轮原始 action envelope 执行 Schema 校验；使用 `num_reasks=0`、`llm_api=None`，关闭远程 Guard 服务、指标收集及 Guardrails 自身的 tracing，不修改应用的 tracer provider。完整候选继续逐项校验，单条错误不通过完整候选 Schema 扩大为整批拒绝。

SDK 可能在其解析结果中省略值为 `null` 的对象键。本适配器记录省略路径，业务流程始终采用原始输入；出现其他内容改变时拒绝。回执中的 `output_unchanged=false` 配合 `normalization_null_only=true` 表示观察到了上述 SDK 行为，不代表程序已经使用其修改后的输出。依赖缺失或校验后端失败会形成明确错误，不静默标记为校验通过。

### 2.2 Outlines 的实际生效路径

同时设置 `FINREPORT_LLM_STRUCTURED_OUTPUT_MODE=json_schema` 与 `FINREPORT_LLM_SCHEMA_BUILDER=outlines`，且调用方传入响应 Schema、使用 OpenAI 兼容协议时，才经 Outlines 构造结构输出参数。

既有 native function/tool 调用优先使用工具 Schema，**不经 Outlines**；Anthropic 原生工具路径也继续使用现有实现。普通 `json_object` 模式不会因为选择 Outlines 就自动获得严格 Schema。本地测试证明参数构造与接线；另一次微型真实请求返回了符合该 Schema 的对象，仅证明当前中转站接受了该请求，不证明所有复杂 Schema 均受支持，也不证明服务端采用了 token 级约束解码。本机没有进行 token 级约束解码。

### 2.3 Semantica 的隔离与回放

图记录财报、来源页、文本块和引用候选，核对发行人、版本、文档哈希、页面哈希及原文哈希；拒绝跨范围或不完整来源。无文字来源的计算结果和无引用不足处置不进入这张文字引用图，仍保留在原分析结果中。

正式接线将图保存到工作流 trace 的 `semantic_evidence_graphs`，作为侧车证据记录；不改变事实准入、公式结果、模型提示或原财务结果哈希。存在该字段的运行会在离线回放时重建图并核对一致性。`ready` 仅表示图构建完成；缺依赖、版本不支持、后端异常或候选被排除均有独立记录。

## 3. DSPy 数据格式和使用方法

标签数据与预测文件分开保存。评测单位是 `case_id` 所标识的子命题，默认只评测 `test`；也可显式选择 `dev`，不提供训练入口。同一个文档哈希或输入哈希不得跨 `train/dev/test`，避免同一财报的不同题目分别进入训练和测试。缺标签、重复案例、重复预测、未知预测案例、输入哈希不一致及非有限数字均会被拒绝。

查看机器可读 Schema：

```powershell
uv run --no-sync python tools/evaluate-analysis.py --print-schema dataset
uv run --no-sync python tools/evaluate-analysis.py --print-schema predictions
```

以下是一个**合成软件样例**的数据集结构，可保存为 `tmp/demo-dataset.json`。金额 100 不对应实际财报，重复字母哈希仅演示字段格式；真实输入必须根据冻结原件、输入包和标签工件计算相应 SHA-256。

```json
{
  "schema_version": "finreport-analysis-evaluation-v1",
  "dataset_id": "synthetic-demo",
  "cases": [{
    "case_id": "demo-1",
    "obligation_id": "cashflow_reconciliation",
    "document_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "input_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "split": "test",
    "label_basis": "synthetic",
    "label_artifact_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "expected": {
      "assessment": "observation",
      "conclusion_code": "synthetic_bridge_closed",
      "admitted": true,
      "source_ids": ["synthetic:source-1"],
      "numbers": {
        "cfo": {
          "value": "100",
          "unit": "CNY-thousand",
          "period": "2025-H1",
          "basis": "consolidated",
          "absolute_tolerance": "0.01",
          "relative_tolerance": "0"
        }
      }
    }
  }]
}
```

对应预测保存为 `tmp/demo-predictions.json`：

```json
{
  "schema_version": "finreport-frozen-predictions-v1",
  "predictor_id": "saved-demo",
  "predictions": [{
    "case_id": "demo-1",
    "input_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "assessment": "observation",
    "conclusion_code": "synthetic_bridge_closed",
    "admitted": true,
    "source_ids": ["synthetic:source-1"],
    "numbers": {
      "cfo": {
        "value": "100",
        "unit": "CNY-thousand",
        "period": "2025-H1",
        "basis": "consolidated"
      }
    }
  }]
}
```

运行评测：

```powershell
uv run --no-sync python tools/evaluate-analysis.py tmp/demo-dataset.json tmp/demo-predictions.json --split test --output tmp/demo-evaluation.json
```

`admitted` 表示候选处置是否应被系统接纳，可包括正确的证据不足处置，不表示某项会计事项已经发生。`conclusion_code` 是由评测规则显式定义的稳定分类码；分类相同不自动证明两段自由文本的语义等价。`assessment` 必须采用既有合法枚举。

标签须声明来源：`synthetic` 只验证软件行为；`regression_reference` 只验证与历史参考一致；`independent_gold` 用于显式提供的独立测试金标准。SDK 不生成金标准，也不因声明 `independent_gold` 自动认证标签质量。当前真实八项演示全部属于 `regression_reference`。

数值要求单位、期间和口径一致，再以绝对容差和相对容差中较大者比较；来源按 ID 集合精确比较。漏答降低覆盖率，不转换成“证据不足”或“未披露”。结果包括 SDK / Python 版本、评测源码哈希、输入文件及规范化输入哈希、配置、逐项结果和按子命题汇总；不复制原文或密钥。

| 指标 | 分母和含义 |
|---|---|
| `disposition_coverage` | 有预测处置的案例 / 本次所选 split 全部案例 |
| `fully_correct_rate` | 类别、数值、来源、接纳状态均符合标签的案例 / 全部案例 |
| `false_admission_rate` | 预测接纳但任一必检项不符的案例 / 所有预测接纳案例 |
| `false_positive_rate` | 标签应拒收却被接纳的案例 / 标签应拒收案例 |
| `supported_admission_coverage` | 标签应接纳且全部必检项相符的案例 / 标签应接纳案例 |

空分母输出 `null`。完整处置覆盖率不能替代实质分析完成度；回归参考的一致率也不能解释为金融准确率。命令返回成功表示评测已执行，不要求结果必须满分。

## 4. 本次已执行验证

| 验证对象 | 实际结果 | 证据及适用范围 |
|---|---|---|
| Guardrails AI / Outlines 真实 SDK 专项 | **18 passed** | `tests/unit/infrastructure/test_output_guard.py`；包括原值检查、独立候选边界、null 省略记录、其他修改拒绝、依赖缺失、结构输出参数及 tracing 禁用；最终四文件专项共 50 passed，无警告 |
| 历史 action envelope | **2/2 轮通过**，各含 7 / 5 条候选；重复回执一致 | `tmp/open-source-integrations-20261019/guardrails-saved-envelopes.json`；只检查协议外壳，不据此宣称其中 12 条候选全部财务正确 |
| Semantica 专项 | **18 passed** | `tests/unit/infrastructure/test_semantic_graph.py`；覆盖来源范围、哈希、关系、版本隔离及重复构图 |
| 四份已存选页来源图 | 合计 **247** 个来源块，Q1 / H1 / Q3 / 年报分别 **40 / 60 / 33 / 114**；各自两次图哈希一致 | `tmp/opensource-20261019/semantic-selected-pages-receipt.json`；四个版本分开，未混成一个报告 |
| H1 引用候选图 | 提供 30 条保存 finding，13 条有合格文字来源进入图，17 条被排除 | 排除项包括无文字来源的程序事实和不足处置；排除仅表示不进入文字引用图，不是从财务结果删除或认定财务错误 |
| DSPy 专项 | **11 passed** | `tests/unit/infrastructure/test_analysis_evaluation.py`；真实 `Evaluate`，禁止测试中调用 LM 或网络，检查泄漏、漏答、数值、引用、误放及错误脱敏 |
| 八项冻结回归参考 | 8 项类别 / 处置 / 来源与参考相同，现金流 4 个数值检查相同；两次结果字节哈希一致 | `tmp/opensource-20261019/dspy/verification-summary.json`；单发行人、选页、历史程序输出参考，独立金标准不可用 |
| DSPy 合成负例演示 | 5 例中有 4 份预测，1 例完全符合、3 例误放、1 例漏答 | `tmp/opensource-20261019/dspy/synthetic-result.json`；用于证明错误统计能区分数字、引用、接纳和漏答，不是金融表现评测 |
| 三扩展联合工作流 | 开关前后财务结果完全相同；新轨迹离线回放通过；篡改校验回执或来源图被拒绝 | `tests/unit/infrastructure/test_analysis_extensions.py`；采用模拟 HTTP 响应验证应用接线，未把模拟模型当真实金融分析 |
| 当前中转站结构输出兼容 | 1 次请求返回合法对象，通过本地 Guardrails 校验；35 输入 + 14 输出 = 49 tokens，推理 tokens 为 0 | `tmp/opensource-20261019/gateway-compatibility.json`；使用现有 DeepSeek-V4.1-Flash、关闭思考、价格优先，不发送财报正文 |
| 四库冷启动与遥测检查 | 全新 Python 进程导入四库，0 次网络尝试；执行 Guardrails 校验后 OTel span 数为 0 | `tmp/open-source-integrations-20261019/cold-start-validation.json`；导入前拦截网络，不修改应用 tracer provider；此路径未导入 LiteLLM |
| 历史离线回放 | **42/42 passed**，全部原文件字节不变，0 次模型调用 | `tmp/gateway-completeness-20261017/history-replay.json`；H1 冻结输入另行重复回放两次，财务结果哈希相同 |
| 全量软件回归 | **860 passed，11 warnings** | `tmp/opensource-20261019/pytest-final.log`；警告涉及 Starlette/FastAPI 弃用、SQLAlchemy identity map 和重复 OpenAPI Operation ID，不包含 Guardrails tracing 警告 |

四版来源图中只有 H1 使用了历史 finding；其他三版仅检查已存选页来源，没有制造不存在的分析结果。H1 八项回归仍保留原来的 **3 项 `pending_review`、5 项 `insufficient_evidence`**；不扩展为八项财务结论全部证实。

DSPy 两次八项结果文件 SHA-256 均为：

```text
99286f6f8aa2437326def2244055e17592290e72636e8952fc34f6665806895a
```

这些专项数量分别统计，不合并称为整链验收。最终四文件专项为 **50 passed、无警告**，记录在 `tmp/opensource-20261019/pytest-extensions.log`；整体回归以 `tmp/opensource-20261019/pytest-final.log` 为准。软件测试数量不代表财报数据准确率。

本轮涉及的源码、CLI 和专项测试已通过 Ruff；`git diff --check` 通过。GitNexus 刷新索引后，对工作区全部已跟踪变更检测到 36 个变化符号和 21 条受影响流程，风险等级为 `critical`，覆盖共享分析、模型传输和离线回放入口；该结果还包括工作区原有改动，不能全部归因于本轮。检测列出了全部 36 个符号，没有 `partial` 或 `truncated` 标记，但索引器另有流程枚举预算和跨语言解析限制，且未跟踪的新文件不包含在 Git diff 检测中；新增模块另经直接检查和专项测试。因此不将图谱结果解释为完整影响范围或安全认证。

## 5. 复现命令与工件边界

无需财报原件即可运行的软件专项：

```powershell
uv run --no-sync pytest tests/unit/infrastructure/test_output_guard.py tests/unit/infrastructure/test_semantic_graph.py tests/unit/infrastructure/test_analysis_evaluation.py tests/unit/infrastructure/test_analysis_extensions.py -q
```

本次工作区中保留原始冻结工件时，可重建来源图和八项回归投影：

```powershell
uv run --no-sync python tmp/opensource-20261019/probe_semantic.py
uv run --no-sync python tmp/open-source-integrations-20261019/cold_start_validation.py
uv run --no-sync python tmp/opensource-20261019/dspy/build_eight_regression.py
uv run --no-sync python tools/evaluate-analysis.py tmp/opensource-20261019/dspy/eight-dataset.json tmp/opensource-20261019/dspy/eight-predictions.json --output tmp/opensource-20261019/dspy/eight-result-1.json
uv run --no-sync python tools/evaluate-analysis.py tmp/opensource-20261019/dspy/eight-dataset.json tmp/opensource-20261019/dspy/eight-predictions.json --output tmp/opensource-20261019/dspy/eight-result-2.json
Get-FileHash tmp/opensource-20261019/dspy/eight-result-1.json -Algorithm SHA256
Get-FileHash tmp/opensource-20261019/dspy/eight-result-2.json -Algorithm SHA256
uv run --no-sync python tmp/eight-obligations-20261018/verify_saved.py
```

来源 trace / 数据库的路径和哈希保存在 Semantica receipt 及 `dspy/eight-manifest.json`，DSPy 投影生成脚本保留类别映射和数字来源。结果 JSON 不包含日志时间；SDK 日志另存，避免把日志时间差异误判为财务结果变化。

`tmp` 下财报、数据库、原响应和实验工件不属于源码分发内容。干净检出环境需要自行提供对应冻结材料并核对哈希，不能假定这些实验路径存在。没有原实验材料时，可按第 3 节构造显式合成样例，验证安装、CLI 和指标行为；这不能复现真实财报内容。

后续需要用独立标注、未参与规则调整的多发行人样本，比较启用前后语义误放、误拒、必要计算完成度与模型重跑结论稳定性，才能评估框架是否带来实际分析收益。涉及新增 LLM 调用的优化、OntoGPT 抽取或 DSPy 编译训练应另行登记输入、标签、配置和预算。
