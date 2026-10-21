# FinReport

面向“上市公司财务报告分析”赛题的中国 A 股本地研发原型。系统围绕年度、半年度和季度报告，已有 PDF/OCR 提取、财务指标计算、证据检索和多轮模型分析路径；正式版目标以自动校验作为计算准入，测试复查仅用于评测规则，重点是让数据、计算与结论能够回到原文检查。

> **项目状态**：已有可运行的端到端原型和自动化测试，但尚未完成整份财报、多发行人、测试复查样本和竞赛级准确率评测。这里列出的实现能力不等于对所有公司或报告格式均已验证，也不构成“已满足全部赛题要求”的结论。

**事实准入**：正式版以自动校验和来源追溯为门槛，使用 `auto_passed`、`needs_review`、`rejected` 和 `candidate` 状态。整份原生 PDF 事实已有显式自动校验入口；新章节流程只查询真实校验通过的事实，OCR 和选页候选尚未获得同等自动准入。旧流程兼容保留，尚未完成全项目状态机迁移。测试金标准只用于评测和误差归因，不属于正式运行流程。

## 赛题映射

| 赛题关注点 | 研发方案与已有基础 | 验证边界 |
| --- | --- | --- |
| 财报数据提取 | 已支持原生 PDF 文本/坐标或选页 Unlimited-OCR；正式版对两条路径的候选统一执行格式、单位、期间、口径、勾稽、来源定位和置信度校验 | PDF 文本可读不代表表格结构正确；疑点保留为 `needs_review`，通过补证、重提取或规则重算处理；测试复查仅用于评测 |
| 同比、环比计算 | 服务端已有期间、单位和合并范围校验及季度累计数拆分等试验通路；新章节流程严格计算只读取自动校验通过且来源可追溯的事实 | 整份原生事实可显式自动校验；OCR/选页候选准入尚待补齐，比较期间、基数或来源不兼容时拒算并请求材料 |
| 非经常性损益、口径变化 | 已有分项分析与留痕路径；正式版按原文和自动校验通过的事实组织结论、待核项或补件请求 | 逐项处置不证明模型发现全部事项；表格续页、扣减项和合计仍需完整来源 |
| 利润与现金流背离、异常信号 | 程序提供可复算的指标和比较结果；模型可检索原文、调用受控分析工具并区分披露、推断和证据不足 | 数据质量通过不代表没有财务异常；现金流勾稽不证明经营因果，检索未命中也不等于全文未披露 |
| 过程透明与复现 | 保存文件/原文哈希、证据位置、计算输入、模型请求与响应、工具轨迹和结果状态；支持对已保存运行离线回放 | 离线回放验证保存输入和响应下的程序确定性，不保证重新调用外部模型会生成完全相同的措辞或判断 |

## 当前样本试验

2026-10-18 的一次固定输入试验使用比亚迪 2025 年半年度报告的 7 个选定页（共 60 个原文块），逐项处置了 8 个分析子命题：3 项候选按本计划术语记为“待校验候选”，5 项因选页证据不足而要求补充材料，运行状态为 `waiting_for_documents`。该次试验尚无测试复查记录，也没有覆盖完整报告；本次未按新校验状态机重跑，不能据此认为自动准入已通过评测。

## 正式版目标流程

```text
上传 PDF 并通过安全扫描
        ↓
选择提取方案与页码
        ↓
提取候选事实和原文证据 → 自动校验与候选标记
        ↓
严格年度指标 / 报告金额试验计算
        ↓
多轮证据检索与分析 → 完成、部分结果或请求补件
        ↓
查看来源、计算和分析轨迹并导出
```

提取方案由用户显式选择，不会在 PDF 文本提取失败时自动改走 OCR：

1. **直接 PDF 提取**：读取 PDF 文字层和坐标。可选择页码，另可使用关闭 OCR/VLM 的 Docling 做文本预处理；目标是将坐标解析候选统一送入自动校验。
2. **OCR 识别**：把所选 PDF 页渲染成图像后调用 Unlimited-OCR，不读取该任务的 PDF 文字层或文字坐标。OCR 表格先进行科目、行列和口径候选映射，再进入自动校验。

正式版中，自动校验是两条路径的共同门槛。严格计算只读取 `auto_passed` 且来源可追溯的事实；`needs_review` 保留为未解决候选，不设置人员处理的正式准入要求。测试复查用于评测误放、误拒和规则覆盖，不属于正式运行流程。

## 架构

```text
apps/desktop                 .NET 10 WPF 桌面端
apps/api-host                FastAPI API 与组合根
apps/worker-host             Celery Worker 组合根
packages/finance-domain      领域模型、口径和公式
packages/finance-application 用例与工作流编排
packages/finance-adapters    数据库、文件、PDF/OCR、LLM 与导出适配器
apps/scanner-supervisor      Rust 扫描监督程序
```

Python 3.11+ 的包和开发依赖由根目录 `uv` workspace 管理；桌面端通过版本化 API 与后端交互。默认开发配置使用 SQLite 和本地文件目录，数据库可配置 PostgreSQL。当前 API 开发执行器使用 FastAPI 后台任务；虽提供 Celery Worker 组合根，仍未验收 Celery/Redis 多进程生产部署。

本地可选服务包括项目内 Unlimited-OCR、Qdrant MCP 和 Docling MCP。SQLite 保存权威证据；Qdrant 用于检索，服务不可用时可走关键词降级；Docling 仅作为原生 PDF 文本预处理，不承担严格金额确认。启动这些服务不是普通 API 请求的隐式副作用。

### 可选分析扩展

已接入 Guardrails AI 进行本地动作外壳校验、Outlines 构造远程 JSON Schema 参数、Semantica 记录按发行人和财报版本隔离的候选来源图，以及 DSPy 评测冻结结果。依赖按 extra 固定版本，源码默认不开启；安装、开关、离线评测文件格式及实际验证范围见 [开源分析扩展](docs/open-source-analysis-extensions.md)。

这些组件分别检查协议、引用关系和回归一致性，不能据此推定会计语义错误已消除。OntoGPT 等待财务本体和独立标注基础；DSPy 尚未进行提示训练或在线优化。

旧版证据分析流程的新运行默认启用独立于上述可选 SDK 的 `claim_support_v2` 支持性准入，最终综合报告另记录 `final_claim_support_v2`。完整已保存父来源上下文仅作为带来源标签的原文观察保留；`rationale` 和逐行 `reason` 只接纳中性边界或程序认证的固定模板，两段引文各自真实不证明它们之间存在支持关系。局部原文单元和无法确认支持的自由改写保留原始候选与准入回执，不进入已接纳结论；程序计算继续独立验证。该策略也会隔离正确但尚未能验证的改写，仍需独立测试金标准评估误放、误拒及分析覆盖，不能称为通用语义正确性保证。已完成历史运行按保存策略只读校验，不静默升级或重发请求；新旧策略检查点不混用。下文新章节流程采用独立版本及 `claim_evidence_verify_v2`，不继承上述固定模板准入。旧流程修复与逐阶段证据见[语义治理推进报告](docs/semantic-progress-validation-20261019.md)。

DSPy 已增加带主体、对象、否定、条件、期间、口径、原文 span 和简要依据的 v2 评测及 Signature 合同，尚未接入生产生成或优化器。旧记录按冻结策略回放，不自动升级为通过新门禁；设计与限制见 [结构化主张语义评测 v2](docs/semantic-evaluation-v2.md)。

新增独立[冻结来源评测 v3](docs/semantic-evaluation-v3.md)，在参考比较外检查授权来源、冻结全文哈希和真实切片，避免标签与预测共用错误坐标仍获得严格匹配。来源绑定仍不证明中文语义蕴含或财务事实；当前真实来源样例只有构造回归参考，没有独立测试金标准。

另有显式离线[原页构成表区域探针](docs/composition-regions-v1.md)，保存有线表区域、多列期间及每个 cell 的原页 span，条件算术使用隔离的 Decimal 环境。金额相等依赖明示的平坦加总及扣减假设，语义与正式准入均未认证；H1 无明确续表锚点的页不继承期间、单位或跨页闭合。该探针未接入正式事实准入。

严格分析恢复入口与执行器已隔离工作流类型，并以数据库原子认领防御同 ID 重复投递；已完成记录保持只读，失败及待补件状态不自行重发。这里未实现租约、fencing 或崩溃后的自动接管；生产 runtime/Celery 入口的并发测试使用离线模型 stub，不代表真实多机部署验收。

前一阶段语义准入修复的全量软件回归为 **932 passed，11 warnings**；该阶段的原始错误、16项保留输出与误拒边界见 [语义修复与实际结果核查](docs/semantic-remediation-validation-20261019.md)。软件测试数量不代表财报准确率。

后续新增持久化的结构化候选实验入口：显式选择单份财报的页码与子命题，局部补读来源并一次批量生成。模型按版本化 Schema 返回结构化候选（包括 `statement`、`relation`、`claim_kind`、`brief_basis` 和 `task_evidence`）；引用字段使用来源 ID 与逐字引文，坐标由程序定位，原始响应会保存。v5 区分值、变动、原因、调节等证据维度，并只在已支持的原文语法范围内重建观察和核对相邻勾选状态；未覆盖语法不能据此判断原文没有披露。固定文案只描述候选状态，不复制未认证的自由解释。程序可重建受支持表格中的公司披露归因，保留科目、原因、比较金额、两列表头及单位锚点，但不证明独立因果关系。实验候选不进入严格金额事实或正式结论，也不会覆盖正式报告导出。新增只读 `grounded_evidence_view_v2` 和 `disclosed_arithmetic_v1`，从已保存运行重建原文观察与披露同比检查；其结果不认证来源真实性、语义支持、任务覆盖或正式准入。四份 PDF 独立链路、47 条受限披露归因、4 次真实批量调用及原始错误的逐阶段归因见 [结构化候选与实际生成核查](docs/grounded-analysis-validation-20261019.md)。

截至 2026-10-19，包含 v2 支持门禁、来源评测 v3、工作流隔离、原子认领和原页构成表增量的工作区全量软件回归为 **1558 passed、1 skipped、11 warnings**，退出码 0；日志与源码不变回执位于 `tmp/semantic-validation-20261019/final-workspace-regression/`，Ruff 通过。跳过项为本机无真实符号链接创建权限；5 条 OpenAPI 重复操作 ID 警告仍在定位。此前 **1406 passed** 是证据视图阶段历史基线。最终源码下 52 份旧流程为 49 份通过、3 份缺少历史 payload profile；11 份固定候选导出双回放 22 次，加上另一份真实 v5 导出双回放 2 次，完整回执一致。本增量没有新增供应商模型调用；这些结果不代表财报准确率、独立测试金标准通过或新模型复跑一致。逐阶段证据见[最新验证报告](docs/semantic-progress-validation-20261019.md)。

通过`GET /api/v1/grounded-analyses/{id}/export`下载的`run.json`可使用`uv run --no-sync python tools/replay-grounded-analysis.py --input run.json --output new-receipt.json`离线重建，无需数据库、PDF或模型连接。`GET /api/v1/grounded-analyses/{id}/recovery`只检查保存状态，不接管worker或重新收费。离线回放一致不代表重新调用模型结论一致；最新真实调用的经营现金流原因符合原文，但自由文字仍将“重大差异原因说明”改称“调节表”，精确合同也不能认证这类语义改写。

## 章节分析、跨章取证与运行日志

新增独立版本 `chapter-evidence-analysis-v1`：章节首轮分析 → 财务专题跨章检查 → 未处理材料中性补查 → 程序校验 → 逐条支持性核验 → 受控汇总。六类角色使用预写且带哈希的提示词；判断修订、原文切片、计算结果和核验回执分别保存。反证、限定或汇总冲突会使相关回执失效，新增判断必须重新核验。

四份报告默认各自运行，联合分析必须显式选择报告集合，保留各自身份。工具只读取冻结的授权本地材料，支持目录、词法检索、原文及上下文读取、事实查询和确定性计算。此版本尚未接入 Qdrant 语义检索；章节边界来自标题候选，跨页续表与脚注完整性仍需补齐。材料读过、处置过、被引用分别计数，不等于模型充分理解；报告同时展示选页与未选页范围。

桌面端默认选择新章节流程，可查看分级日志、供应商实际 reasoning、取消请求、离线回放及通过门禁的 Markdown。旧分析入口单独保留。日志支持 `TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL`，SQLite 保存权威事件，JSONL、可读日志、manifest、检查点和不可变输入输出工件用于排查。响应不完整、结果未知、工件损坏或关键日志写入失败时，不自动重发付费请求。

主要接口：

- `POST /api/v1/filings/{id}/chapter-analyses`：启动；`selected_document_ids` 显式联合，`limits` 设置调用与时间预算。
- `GET /api/v1/chapter-analyses/{id}`：状态、判断、核验与缺口；`/events` 按序号分页，支持级别、阶段和任务过滤。
- `POST /api/v1/chapter-analyses/{id}/cancel`、`/resume`：请求取消或创建显式续跑。已派发请求可能继续返回；续跑不会重发结果未知的请求。
- `GET /api/v1/chapter-analyses/{id}/replay`：离线检查已保存工件与发布门禁；`/report.md` 只导出通过门禁的报告。

已有已完成提取的报告 ID 时，可复用当前环境运行：

```powershell
uv run --no-sync python tools/run-chapter-analysis.py run 123 --max-model-calls 12
uv run --no-sync python tools/run-chapter-analysis.py events 456 --level DEBUG
uv run --no-sync python tools/run-chapter-analysis.py replay 456
```

示例 ID 需替换为本地真实 ID；`run` 会调用配置的供应商。离线回放通过只证明保存结果符合当前可检查合同，不能证明模型重跑结论一致或所有语义正确。2026-10-21 的两次真实格式兼容测试已发现判定类别与理由越界，尚未通过 G6 语义验收，未扩大到全文。实现、输入输出证据、测试结果和剩余工作见[本轮实施验证报告](docs/chapter-evidence-workflow-validation-20261021.md)。

本轮冻结源码全量回归为 **1900 passed、1 skipped、11 warnings**，退出码 0；Ruff 通过，WPF 构建 0 警告、0 错误。真实调用只核验了冻结候选，尚未真实完整执行新章节工作流，也未完成原流程与新增核验的增益对照。软件行为回归、离线回放与模型语义效果分别验收。

## 财务口径与边界

- 严格数值主流程优先面向人民币、合并口径、完整自然年度和高保证的标准无保留意见样本；非标准审计意见保留原文和审计意见状态并标记为受限，后续研发将其作为异常/风险披露纳入分析，相关计算不静默丢弃。
- Q1、H1、Q3 和年度报告另有“金额候选与试验分析”通路。候选金额和计算会保留待校验/披露属性，不自动升级为严格事实；默认单份报告独立分析，只有显式选择报告后才联合分析。
- 同比、环比依赖期间配对、可比口径和有效来源。累计季报拆单季需要相邻累计期间兼容；材料不足时系统可列出具体缺口，不能把缺失输入当作零。
- 部分通用比率是明示的代理指标，不等同监管或行业统一口径。例如速动比率代理只扣存货；简单平均 ROE 不等于公司披露加权 ROE；自由现金流使用经营现金流减购建长期资产支付现金的代理定义。
- 程序事实、公司披露、分析推断和证据不足分别呈现。对经营原因的解释不能由数值等式单独证明；模型输出的结论仍需按证据和校验状态理解。
- 研发过程同时保存结构化结果、表格或报告，以及文件、工具、计算和模型生成日志；源码、依赖、配置和演示环境版本需要冻结并按研发 Gate 检查。
- 当前没有行业模块声明为 `supported`。行业能力启用前需要符合设计要求的多发行人 gold 样本、测试复查抽样和专项自动化测试；对标样本也须通过覆盖门禁。

## 快速开始

在 Windows PowerShell 中：

```powershell
Copy-Item .env.example .env
uv sync --all-extras
uv run uvicorn finreport_api.main:app --reload
```

API 文档地址为 <http://127.0.0.1:8000/docs>。`.env` 中的模型和服务连接仅为配置入口；接入模型分析或 OCR 前，需按实际环境配置各自的服务地址、模型及密钥。不要把真实密钥写入仓库。

上传解析依赖扫描器和病毒库。默认配置使用 Scanner Supervisor；扫描器缺失、扫描异常或对象哈希变化时，文件不会绕过扫描进入解析。先按 [Scanner 协议与验收说明](docs/scanner-protocol-v1.1.md)准备运行环境。项目内 Unlimited-OCR、Qdrant MCP 和 Docling MCP 的安装与启动说明分别见 [OCR 部署](docs/local-unlimited-ocr.md)和[本地 MCP 服务](docs/local-mcp-services.md)。

运行 API 与测试：

```powershell
uv run pytest
uv run ruff check .
dotnet build FinReport.sln --nologo
```

完整 Windows 测试构建也可使用 `.\build.ps1`。构建脚本还会检查架构、生成 OpenAPI、构建 Rust Scanner、运行 ClamAV 验证并构建桌面端；它需要本机准备好相应的 .NET、Rust、Python 和 ClamAV 工具链。自动化测试通过不等于对原始 PDF 提取准确率或模型财务结论正确率的验收。

## 主要配置

配置项在仓库根目录 `.env.example` 中。进程环境变量优先于 `.env`；修改配置后需重启 API。

| 用途 | 配置项 |
| --- | --- |
| 数据库与文件存储 | `FINREPORT_DATABASE_URL`、`FINREPORT_STORAGE_DIR` |
| 病毒扫描 | `FINREPORT_AV_MODE`、扫描器运行目录和数据库配置 |
| LLM 分析 | `FINREPORT_LLM_PROVIDER`、`FINREPORT_LLM_BASE_URL`、`FINREPORT_LLM_API_KEY`、`FINREPORT_LLM_MODEL` |
| LLM 流式/路由选项 | `FINREPORT_LLM_STREAM`、`FINREPORT_LLM_ENABLE_THINKING`、`FINREPORT_LLM_PROVIDER_SORT` |
| OCR | `FINREPORT_OCR_PROVIDER`、`FINREPORT_OCR_BASE_URL`、`FINREPORT_OCR_API_KEY`、`FINREPORT_OCR_MODEL`、`FINREPORT_OCR_PROTOCOL` |
| 本地检索与预处理 | `FINREPORT_QDRANT_ENABLED`、`FINREPORT_QDRANT_MCP_URL`、`FINREPORT_DOCLING_ENABLED`、`FINREPORT_DOCLING_MCP_URL` |

思考内容只展示供应商实际返回的字段，不代表可验证的内部思维过程。价格优先路由选项也不能证明中转站实际选用了最低价供应商。更多配置和分析完整性说明见 [LLM 中转站与分析完整性](docs/llm-gateway-and-completeness.md)。

## 文档导航

- [章节分析、证据核验与分级日志方案](docs/chapter-evidence-workflow-plan-20261021.md)及[实施验证报告](docs/chapter-evidence-workflow-validation-20261021.md)：研发目标、实际实现、真实模型问题与分阶段验收状态。
- [上市公司财务报告分析研发项目计划书](docs/competition/FinReport上市公司财务报告分析项目计划书.pdf)（[Word](docs/competition/FinReport上市公司财务报告分析项目计划书.docx) / [Markdown](docs/competition/FinReport上市公司财务报告分析项目计划书.md)）：研发目标、工作包、Gate、技术路线、验证计划、风险与研发实施计划。
- [LLM 中转站与分析完整性](docs/llm-gateway-and-completeness.md)：模型接入、流式响应、思考字段和分析覆盖的边界。
- [本地 Unlimited-OCR 部署](docs/local-unlimited-ocr.md)：项目内 OCR 服务的安装、启动与验证。
- [本地 MCP 服务](docs/local-mcp-services.md)：Qdrant 与 Docling 的本地配置和健康检查。
- [Qdrant 证据检索说明](docs/qdrant-evidence-retrieval.md)：检索架构、SQLite 降级与适用边界。
- [Scanner 协议与验收说明](docs/scanner-protocol-v1.1.md)：上传文件扫描、可信对象发布与失败阻断。
- [可实施技术设计 V2（历史设计文档）](word/可实施技术设计V2.md)：包含部分目标方案和较早的设计描述；实现状态以当前代码、配置及上述 `docs/` 文档为准。
