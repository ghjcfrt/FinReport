# FinReport

面向“上市公司财务报告分析”赛题的中国 A 股本地研发原型。系统围绕年度、半年度和季度报告，已有 PDF/OCR 提取、财务指标计算、证据检索和多轮模型分析路径；正式版目标以自动校验作为计算准入，测试复查仅用于评测规则，重点是让数据、计算与结论能够回到原文检查。

> **项目状态**：已有可运行的端到端原型和自动化测试，但尚未完成整份财报、多发行人、测试复查样本和竞赛级准确率评测。这里列出的实现能力不等于对所有公司或报告格式均已验证，也不构成“已满足全部赛题要求”的结论。

**正式版研发目标**：将事实准入统一为自动校验和来源追溯，状态为 `auto_passed`、`needs_review`、`rejected` 和 `candidate`。该改造属于计划书 WP4，以下目标流程与当前原型需区分；本次文档更新不代表代码已经完成状态机迁移。测试复查仅用于评测和误差归因，不属于正式运行流程。

## 赛题映射

| 赛题关注点 | 研发方案与已有基础 | 验证边界 |
| --- | --- | --- |
| 财报数据提取 | 已支持原生 PDF 文本/坐标或选页 Unlimited-OCR；正式版对两条路径的候选统一执行格式、单位、期间、口径、勾稽、来源定位和置信度校验 | PDF 文本可读不代表表格结构正确；疑点保留为 `needs_review`，通过补证、重提取或规则重算处理；测试复查仅用于评测 |
| 同比、环比计算 | 服务端已有期间、单位和合并范围校验及季度累计数拆分等试验通路；正式版严格计算只读取自动校验通过且来源可追溯的事实 | 当前准入改造归入 WP4；比较期间、基数或来源不兼容时不强算，并可能要求补交材料 |
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

- [上市公司财务报告分析研发项目计划书](docs/competition/FinReport上市公司财务报告分析项目计划书.pdf)（[Word](docs/competition/FinReport上市公司财务报告分析项目计划书.docx) / [Markdown](docs/competition/FinReport上市公司财务报告分析项目计划书.md)）：研发目标、工作包、Gate、技术路线、验证计划、风险与研发实施计划。
- [LLM 中转站与分析完整性](docs/llm-gateway-and-completeness.md)：模型接入、流式响应、思考字段和分析覆盖的边界。
- [本地 Unlimited-OCR 部署](docs/local-unlimited-ocr.md)：项目内 OCR 服务的安装、启动与验证。
- [本地 MCP 服务](docs/local-mcp-services.md)：Qdrant 与 Docling 的本地配置和健康检查。
- [Qdrant 证据检索说明](docs/qdrant-evidence-retrieval.md)：检索架构、SQLite 降级与适用边界。
- [Scanner 协议与验收说明](docs/scanner-protocol-v1.1.md)：上传文件扫描、可信对象发布与失败阻断。
- [可实施技术设计 V2（历史设计文档）](word/可实施技术设计V2.md)：包含部分目标方案和较早的设计描述；实现状态以当前代码、配置及上述 `docs/` 文档为准。
