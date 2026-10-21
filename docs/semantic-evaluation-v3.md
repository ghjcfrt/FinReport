# 冻结原文绑定语义评测 v3

v3 补充“标签和预测引用同一错误 span 仍可能同时匹配”的评测缺口。独立模块 `analysis_evaluation_sources.py` 和入口 `tools/evaluate-analysis-v3.py` 复用 v2 字段比较，再核验实际冻结原文；旧 v1/v2 文件格式、实现和历史结果保持原义，不自动升级。

## 输入合同

数据集版本为 `finreport-analysis-evaluation-v3`，预测版本为 `finreport-frozen-predictions-v3`，结果版本为 `finreport-analysis-evaluation-result-v3`。每个 case 除 v2 字段外还包含：

- `authorized_source_ids`：本 case 明确授权的来源集合。来源存在不自动等于获得授权。
- `frozen_sources`：每条包含 `source_id`、`document_sha256`、`text_sha256`、完整冻结 `text` 和从 1 开始的 `physical_page_no`。
- 每个 case 只允许一个 `document_sha256`。需跨文档比较时先拆成不同 case；不把四个报告混作一个期间。

所有来源文本哈希须与实际 UTF-8 文本一致；来源声明的文档哈希须与 case 文档相同。标签中的直接证据和规则证据都必须使用授权且已声明的来源，哈希一致，`[start,end)` 不越界，且 `source.text[start:end] == quote`。不合格标签在数据集构造时直接拒评，避免标签和预测一起错仍得到满分。

CLI 在 Schema 前拒绝任意层级的重复 JSON 键及 `NaN/Infinity/-Infinity`，避免冲突字段被解析器静默覆盖。物理页号和字符偏移采用严格整数类型，不把布尔值、字符串或小数强转为位置。

预测采用同样核查，但错误逐条记录为 `source_binding_errors`，包含 claim、来源、位置和错误码，不输出原文。每条规则自己的 `evidence_links` 也核验，不能通过移入 rule evidence 绕过。缺失预测降低覆盖率并单列统计，不生成虚假的“来源错误”或证据不足结论。明确拒绝或不足处置仍可使用 `limitation + requires_evidence=false`，无需伪造引用；事实与推断不能关闭证据要求。

## 计分与能力边界

`dspy_reference_score_percent` 保留真实 DSPy v2 比较得分；`strict_score_percent` 是 v3 的最终严格分数，要求 v2 完整匹配且本预测全部来源绑定有效。结果不保留可能与新严格口径混淆的 `dspy_strict_score_percent` 名称。`reference_v2_fully_correct`、来源核查状态和 v3 `fully_correct` 分别保存，能够定位失败层。

`metrics` 同时报告来源错误总数、含错误预测数、已检查/有效/缺失预测数；逐类语义字段指标仍保持 v2 的比较含义。正文和依据沿用 v2 空白归一后的严格参考匹配，尚未增加多个有效表达登记，也不做自动同义推断。

拒绝处置采用上述 `limitation` 表达。若把未获支持的候选仍登记为 `fact + requires_evidence=true + grounding=unsupported`，继承的 v2 严格指标会因该事实未获支持而失败，即使 `admitted=false` 且与标签相同；v3 没有把这种“候选事实分类”任务重新解释成有效最终事实输出。

来源绑定证明引用身份与冻结完整性，**不证明原文蕴含主张、业务因果、规则正确性或财务结论真实**。评测器不打开 PDF，因此文档哈希和物理页号仍是来源声明；外部原 PDF 与页内容的认证需在语料构建时另行保存证据。`grounding=supported` 仍是待与标签比较的字段，不是独立正确性证据。

## 离线使用

```powershell
uv run --no-sync python tools/evaluate-analysis-v3.py --print-schema dataset
uv run --no-sync python tools/evaluate-analysis-v3.py --print-schema predictions
uv run --no-sync python tools/evaluate-analysis-v3.py DATASET.json PREDICTIONS.json --output NEW_RESULT.json
uv run --no-sync pytest tests/unit/infrastructure/test_analysis_evaluation.py tests/unit/infrastructure/test_analysis_evaluation_v2.py tests/unit/infrastructure/test_analysis_evaluation_sources.py -q
```

`--output` 只创建新文件，不覆盖输入或已有回执。路径别名、解析后与输入相同的路径以及已有 symlink 均拒绝；最后写入使用 exclusive create，发生并发占位时失败。输入或输出错误只报告固定错误码，不回显原文、密钥或内部异常文字。

## 真实来源回归样例

本机保留 `tmp/semantic-evaluation-v3-20261019/`，包含构建脚本、数据集、构造的正确/错位 span/缺失预测及来源清单。脚本核对本地比亚迪 2025 一季报 PDF 文件哈希、物理第 2 页原生文字和已冻结来源文本，之后生成一条营业收入披露参考；标签明确为 `regression_reference`，预测也是构造回归参考，**不是真实模型输出或 independent_gold**。来源、测试财报及生成结果遵循项目忽略规则，不强制纳入 Git。

```powershell
uv run --no-sync python tmp/semantic-evaluation-v3-20261019/build_reference.py
uv run --no-sync python tools/evaluate-analysis-v3.py tmp/semantic-evaluation-v3-20261019/dataset.json tmp/semantic-evaluation-v3-20261019/predictions.json --output tmp/semantic-evaluation-v3-20261019/result-1.json
uv run --no-sync python tools/evaluate-analysis-v3.py tmp/semantic-evaluation-v3-20261019/dataset.json tmp/semantic-evaluation-v3-20261019/predictions.json --output tmp/semantic-evaluation-v3-20261019/result-2.json
Get-FileHash tmp/semantic-evaluation-v3-20261019/result-1.json,tmp/semantic-evaluation-v3-20261019/result-2.json -Algorithm SHA256
```

复跑需选择新的结果文件名，原回执保留。相同输入、源码、依赖和参数下的两次字节一致只证明离线确定性，不证明外部模型复跑结论一致；单条同发行人选页参考也不能证明泛化语义准确率。缺少本地 PDF 或旧冻结来源时，构建脚本失败，不从网络下载或静默造替代样本。

## 2026-10-19 专项验证记录

上述三代评测专项共 `51 passed, 1 skipped`，其中 v3 新增 `28 passed, 1 skipped`；跳过项为 Windows 当前账户未获创建真实 symlink 权限，普通路径别名、已有回执保护及独占创建测试均通过。专项测试在 socket 与 `dspy.LM` 入口阻断网络和模型；Ruff 检查通过。已校验的对象若在调用前被修改，v3 入口会重跑完整来源/标签校验，不能用已构造模型绕过门禁。

真实来源回归参考的最终源码回执为 `result-final-1.json` 和 `result-final-2.json`，两份字节一致，SHA-256 为 `4214a67b3edf33bf4474a8f497e9bdaad2b80dac93be0c6ff952d00533559320`；对应评测模块源码 SHA-256 为 `1e69e417b5329526dcc95130da8eba8414730a36711e4171983ff0c85e481cab`。正确参考的 v3 strict 为 100%；主智能体另按同一最终源码重跑并保存 `result-final-bad-span.json` 和 `result-final-missing.json`：构造错位 span 为 0% 且报告 `EVALUATION_EVIDENCE_QUOTE_MISMATCH`，缺失预测的 strict 和处置覆盖率为 0%，两次命令退出码均为 0。这里退出码 0 表示评测成功完成，不表示预测正确。这些分数描述构造回归对照，不能作为模型性能或金融准确率。
