# FinReport

当前实现基线：[可实施技术设计 V2](word/可实施技术设计V2.md)。

这是一个可本地演示的、可追溯 A 股财报分析服务。首期默认使用 SQLite 与本地对象目录，生产可通过 `FINREPORT_DATABASE_URL` 切换至 PostgreSQL；PDF 解析只产生候选事实，必须经过两个不同审阅标识的确认、证据定位和资产负债勾稽后，才能进入严格指标计算。当前事实审核尚无个人登录认证，两个标识不构成已验证的两人独立身份。

## 分层目录

```text
apps/api-host       FastAPI 接入层与组合根
apps/worker-host    Celery Worker 入口
apps/desktop        .NET 10 WPF 展示层
packages/finance-domain       纯领域值对象、公式、校验、端口
packages/finance-application  用例编排与任务工作流
packages/finance-adapters     SQLAlchemy、文件、PDF、LLM、导出适配器
contracts/openapi              版本化 API 契约快照
database/migrations             Alembic 迁移目录
```

包依赖由 `uv` workspace 管理；应用 Host 通过组合根注入工作流，桌面端只消费 API。`tools/check-architecture.ps1` 会阻止领域层和应用层直接导入外层框架。

## 运行

```powershell
Copy-Item .env.example .env
uv sync --all-extras
uv run uvicorn finreport_api.main:app --reload
```

后端启动时自动读取仓库根目录的 `.env`，进程已设置的环境变量优先。构建不会复制或删除此文件，因此密钥只需配置一次；更改后重启 API 即可。需要指定其他配置文件时设置 `FINREPORT_ENV_FILE`。桌面设置界面的修改仅在当前后端进程内有效；需要重启后保留的连接请写入 `.env`。

支持新中转站的价格优先路由、思考开关与 SSE 流式正文/工具调用；配置、供应商返回思考的展示和逐项覆盖检查边界见 [LLM 中转站与分析完整性](docs/llm-gateway-and-completeness.md)。

打开 `http://127.0.0.1:8000/docs` 查看由 FastAPI 生成的 API 契约。上传与任务创建均返回 `202`，客户端轮询 `/api/v1/jobs/{job_id}`。

```powershell
uv run pytest
uv run ruff check .
uv run python tools/generate-openapi.py
dotnet build FinReport.sln --nologo
```

也可以直接运行根目录的一键构建脚本。它会依次同步依赖、检查架构、执行 Ruff、运行 Python 测试、生成 OpenAPI 契约、构建 Python 后端分发包、构建 Rust Scanner Supervisor、验证内置 ClamAV clean/infected，并构建桌面端；存在本地验收脚本时，还会验证 Supervisor 数据库更新回滚与 API 门禁及桌面集成行为：

```powershell
.\build.ps1
```

Windows 下也可以双击 `build.cmd`。所有可测试产物会整理到 `dist/FinReport-test/`：其中包含桌面端、Python 后端包、Rust Scanner、官方 ClamAV 运行库、官方签名数据库、更新器与许可证，以及 `run-api.cmd`、`run-desktop.cmd`、`run-all.cmd` 启动脚本。`run-api.cmd` 强制使用 `supervisor` 模式，先启动 Scanner 并等待 READY，再开放 API；双击 `run-all.cmd` 会启动 API 和桌面端。Scanner 或数据库缺失时启动失败，上传不会回退成免扫描。

默认构建使用 `tmp/clamav-runtime-official/` 和 `tmp/clamav-official/staging/current.json`，通过 `FINREPORT_CLAMAV_RUNTIME_DIR`、`FINREPORT_CLAMAV_DATABASE_ROOT`、`FINREPORT_CLAMAV_UPDATER_DIR` 可指定已准备的同一架构官方运行包。MinGW HDB 实验路径只在显式指定 `-AllowExperimentalClamAv` 时构建，不用于正式 bundle 启动。`-SkipScanner`、`-SkipNativeShim` 是开发构建开关，生成的缺失扫描资产目录无法通过正式启动器；其他参数包括 `-SkipSync`、`-SkipTests`、`-SkipOpenApi`、`-SkipPythonPackages`、`-RequireScanner`、`-RequireNativeShim` 和 `-SkipDesktop`。

文件流程固定为：上传 `.uploading` → 发布 `.ready` → Supervisor 领取并复算大小/SHA-256 → 常驻 Worker 调用 `libclamav` → 仅 clean 文件提升为可信对象 → API 创建 `scan_clean` 披露版本 → 创建/重试解析任务与后台 Worker 再检查 clean 扫描记录及可信对象哈希。感染、扫描器不可用、超时、崩溃、协议错误或对象被修改均阻断解析。详见 [Scanner 协议及验收证据](docs/scanner-protocol-v1.1.md)。

## 已实现边界

- 内容寻址的 PDF 保存、SHA-256 去重、任务状态与失败审计；本地文本层解析会保留候选值、页码、行列定位与置信度。
- 主报表按合并范围、年度列及坐标取值，声明的千元/万元统一换算为元；原文金额、单位、比较列重述标记和证据位置保留。
- 候选事实的两次不同标识复核、修改产生新版本、来源完整性、审计意见、期间/范围/单位、资产负债及现金流勾稽校验，以及严格指标准入。
- 通用非金融指标、输入快照哈希、趋势查询、样本不足时拒绝统计对标、受限的 OpenAI 兼容模型结论任务、HTML/CSV/PDF 导出。
- 未登记模型或模型调用/claim 校验失败时，分析任务会失败，不会回退生成模板结论。
- 桌面端提供年报、半年报、一季报和三季报上传、原始 PDF 页码选择、任务轮询、逐项审核、严格指标计算和独立的报告试验分析。严格年报分析与 HTML 导出保留原有审核门禁。
- 模型数值输出使用结构化观察值并绑定公式输入与原文证据；服务端复算和格式化数值、公式。完成、部分结果及等待补件的分析均可按其实际状态导出；原始输入或已使用文本审核快照变化后须继续分析，不能导出为当前结果。PDF 使用本地中文字体分页渲染。

当前 OCR 使用 Baidu Unlimited-OCR provider adapter，项目内已部署官方权重的 Transformers CUDA NF4 服务，通过 `http://127.0.0.1:8765/v1` 接入；安装、启动、停止和样本验证见 [本地 Unlimited-OCR 部署](docs/local-unlimited-ocr.md)。也可另行部署 vLLM，按[官方 vLLM recipe](https://recipes.vllm.ai/baidu/Unlimited-OCR)注册 `NGramPerReqLogitsProcessor`，或配置百度云异步文档解析接口。各协议均保存原始响应和页级调用审计，失败重试保留独立证据；模型名称须与 `FINREPORT_OCR_MODEL` 一致。普通提取请求不安装或启动模型服务。百度云模式设置 `FINREPORT_OCR_PROTOCOL=baidu_cloud`、`FINREPORT_OCR_BASE_URL=https://aip.baidubce.com`，并配置独立的 `FINREPORT_OCR_API_KEY` / `FINREPORT_OCR_SECRET_KEY`，由客户端换取并缓存 token；也支持将已有 access token 填入 API key 并留空 Secret Key。扫描页识别结果可保存为待复核表格，但缺少可信的逐行坐标、单位或合并范围时会阻断严格事实。自然语言的原因解释不能通过数值等式证明，模型推断始终标记待人工复核。

桌面端在开始提取前提供两个方案：**方案 1：直接 PDF 提取**（默认，读取文字与坐标，不自动切换 OCR）；**方案 2：OCR 识别**（将所选页渲染为图片识别，不读取 PDF 文字层）。API 创建任务可提交 `{"extraction_method":"pdf_text","page_numbers":[2,7,9,11]}`；页码按原始 PDF 从 1 开始，解析和文本预处理保留该范围。桌面页码留空时必须显式勾选整份报告；API 省略 `page_numbers` 保留处理全篇的兼容行为。方法和页码随任务保存，重试保持原范围；活动任务不能悄然复用另一方案或页码的结果。

两条路径均为“提取 → 人工审核 → 严格指标计算 → AI 分析”，提取完成只代表进入 `needs_review`。直接 PDF 候选沿用事实确认、订正、拒绝及审核摘要校验。OCR 原表页提供“人工映射”窗口：在本次识别的原图上框选金额、整行、单位和审计意见原文，将表格单元格登记为指定科目、单位、期间、范围的候选事实。单位可引用本次任务另一页的原图和响应证据；原始 OCR 数字和人工修正原数分别保存。接口为 `GET /api/v1/filings/{id}/ocr-mapping-context`、`POST /api/v1/filings/{id}/ocr-fact-mappings` 和 `POST /api/v1/filings/{id}/ocr-audit-confirmations`。审计意见来源须对照原图确认，不能凭确认按钮默认通过。

OCR 映射登记不计入事实审批；建议先核实审计意见来源，再登记映射，最后返回事实页逐项双人复核。仅支持人民币、合并、完整自然年度和标准无保留意见的当前严格通路；其他意见或口径明确阻断。选中列的未经审计、季度、半年或歧义年份不能登记为已审全年数据。旧任务、错页、原图/响应/表格哈希变化、单位或期间冲突会被拒绝；计算、分析和导出会再次检查来源。变更审计来源使旧审批失效，同一来源重复确认保持幂等。OCR 分析上下文只取本次保存的 OCR 响应，不读取 PDF 文字层或文字坐标；未核清的问项边界不能支持定性结论。两个方案均不自动豁免审核；审核不是提取成功或哈希匹配的同义词。

切换提取方案后，可在映射窗口选择“完成映射并归档未采用的旧来源事实”（`POST /api/v1/filings/{id}/ocr-mapping-completion`）。系统校验本次映射齐备及审计来源，再将未采用的旧事实标记为历史并记录其 ID；证据和审批记录保留。完成操作冻结当时的候选集合并使先前审批摘要失效，当前候选须再次双人复核。同一候选集合重复完成不会反复失效审批。

原生 PDF 主报表同时保存本期和上年比较列，比较事实单独复核，不阻断本期指标。`GET /api/v1/filings/{id}/period-comparison` 仅接受同报告、合并、人民币、相邻完整年度及有效证据审核，保留经重述基期；金额基期为零或负值时返回不可比和金额差，不给增长率。利润率变化用百分点，现金流利润比用倍数差。保存 API 响应后可用 `uv run --no-sync python tools/replay-period-comparison.py <结果.json>` 离线复算；哈希与重放匹配仅证明保存输入下的计算一致，不证明原始披露真实。多轮分析把已审核比较结果作为独立计算证据交给模型，涨跌方向由程序输出；模型不能自由心算同比。

事实审核绑定当时的值、期间、单位、范围、修订及原文证据；输入或证据变化后须重新审核。升级前未保存有效摘要的审核不会被自动追认。指标规则版本为 `general-non-financial-v4`，旧版结果重算后保留为历史，禁止混入新分析或导出。现有十九项为营业收入、营业利润、归母净利润、经营现金流、毛利率、归母净利润率、经营现金流与合并净利润比率、自由现金流代理、流动比率、资产负债率、速动比率代理、简单平均总资产净利率、简单平均归母净资产收益率、存货周转率、应收账款周转率代理、总资产周转率、EBIT代理、费用化利息保障倍数和净负债代理。平均余额指标读取同一报告上年比较列的已审核期末余额，保留原期间，绑定计算、分析、导出和离线复算快照；缺少期初数时不以期末数替代。同比规则为 `annual-period-comparison-v3`，缺前年期末数时不提供平均指标的上期比较值。

速动比率代理仅扣存货，未扣预付款；应收周转代理使用营业收入及应收账款净额，不冒称赊销口径；简单平均归母收益率不等于监管披露的加权 ROE。EBIT代理为利润总额加费用化利息，保障倍数以该代理除费用化利息，两者均不含资本化利息，保障倍数不等同现金利息覆盖。净负债代理为短期借款、一年内到期非流动负债、长期借款、应付债券及租赁负债之和减货币资金，货币资金含受限部分；不冒称所有企业通用有息净债务或可自由支配现金。任一明细缺失均不以零补足。单位和分母规则、存量非负约束、科目所属报表和流量/存量类型统一核验；计算采用固定 Decimal 精度，行业分类纳入快照。流动比率是期末流动资产/流动负债，资产负债率是期末负债/资产，不能单独证明资产可变现或债务可偿还。负资本开支支付额拒算自由现金流；此指标采用经营现金流减购建长期资产支付现金的代理定义，不能等同统一 FCFF/FCFE。分析验证采用 v6，正负等可计算命题由服务端验证；跨页或不完整勾选问题禁止独立支持结论，候选省略数量在结果中披露。


“年度披露指标”页另外提取并审核扣非归母净利润、基本/稀释 EPS、公司披露加权 ROE、研发投入及资本化金额/比例。直接 PDF 保留页码、原数、坐标、列名和单位；OCR 可在人工映射窗口登记本次单元格与原图框选。`GET /api/v1/filings/{id}/annual-disclosures` 只读，`POST /annual-disclosures/extract` 显式补提取，`PATCH /annual-disclosures/{candidate_id}/review` 逐项审核，`POST /ocr-annual-disclosure-mappings` 登记 OCR 候选（均以前述 filing 路径为前缀）。两个不同标识确认后显示已审核；原数、期间或来源变化后重新审核。已审核披露及其差额、同比、百分点变化可通过多轮分析的独立计算证据进入报告，保留“公司披露”的保证级别，不冒称严格三表事实或独立复算的 EPS/加权 ROE。未审候选不进入正式数值计算。

## 本地证据库与多轮分析

结构化事实、确定性计算与原文块分别保存。每个原文块关联文件 SHA-256、页码、提取方法、提取版本、原文哈希及相关事实，文本确认独立于数值事实确认。不可量化的会计估计、经营解释、风险与或有事项保留原文，不会被丢弃或强行转成金额。

检索采用官方 Qdrant MCP，SQLite 为权威证据库和服务不可用时的关键词降级路径。选型依据与边界见 [Qdrant/Chroma 比较与部署说明](docs/qdrant-evidence-retrieval.md)。本机已安装并接入独立的 Qdrant/Docling MCP：Qdrant 使用实际 CPU FastEmbed `BAAI/bge-small-zh-v1.5`，Docling 在关闭 OCR/VLM 的本地模式下保存完整 JSON、Markdown 和原始页码映射，仅作为方案 1 的文本预处理。严格金额事实仍走现有坐标解析和人工复核。服务地址、启动/健康检查、权重清单和四报告选页验证见 [本地 MCP 服务](docs/local-mcp-services.md)；普通分析请求不安装模型或启动服务。

分析会先检查非经常性损益、会计口径变化、利润与经营现金流关系，再让模型多轮选择 `search_text`、`read_chunks`、`get_facts`、`get_comparisons`、`request_documents` 或 `finish`。程序执行受控工具，模型不能操作任意路径、SQL 或代码。每轮保存实际请求、响应、工具参数和结果、哈希、拒绝原因与检查点。模型上下文使用有限原文窗口；完整原文与轨迹留在本地，可按证据 ID 继续读取。

每轮只接受一次已注册的工具调用，协议错误会保留原响应并给出有限次数的纠正机会，网络故障单独记录。新执行还持续提供查询历史、重复命中、新增来源、主题覆盖和剩余轮数；连续检索没有取得新原文或达到最后一轮时，要求模型综合已有证据。综合动作仍须通过逐字引用、主题覆盖和金融校验，不会由程序补造模型发现。执行与重放按独立策略版本标记重建请求，历史记录继续使用其原协议。

结果区分程序事实、公司披露、分析推断和证据不足。数据质量通过不会被替换成“没有财务异常”，检索未命中也不能证明全文没有披露。需要假设、原文无法确认的原因及未来结果边界可作为有明确依据的分析判断；没有数字不是删除该判断的理由。

缺材料时列出具体期间、科目、用途及替代材料，返回 `waiting_for_documents`；资料待审核或证据不足时保留 `partial_result`。桌面端可登记年报、半年报、一季报、三季报，按所选 PDF/OCR 路径提取并审核原文，再点击“补件后继续分析”。接口为 `POST /api/v1/analyses/{id}/resume`。证据未变时复用保存结果；严格输入变化时创建关联的新分析版本并保留旧结果与轨迹，客户端使用返回的新任务 ID。

原文接口为 `POST /api/v1/filings/{id}/text-evidence/index`、`GET /text-evidence`、`GET /text-evidence/review` 和 `PATCH /text-evidence/{chunk_id}/review`。审核列表能查看并重新确认已拒绝块，普通检索与 Agent 仍排除拒绝块。审核绑定原文和来源摘要，内容变化后不继承旧确认。原文确认只表示转录核对，不证明公司的解释真实。

严格数值主流程仍以完整合并年报为锚点。另有“报告金额候选与试验分析”通路，支持 Q1、H1、Q3、年度报告选页的公司披露金额、来源复核和服务端 Decimal 计算；待审算术明确标记候选，不升级为严格审计事实。`GET /api/v1/filings/{id}/report-data` 读取金额及计算，`PATCH /report-data/{candidate_id}/review` 记录实际复核人。`POST /api/v1/filings/{id}/report-analyses` 默认独立分析当前报告，只有显式传入 `selected_document_ids` 才联合所选报告；累计拆单季与环比计算要求兼容的期间、来源和配对输入，缺失或冲突时保留问题说明。`GET /api/v1/report-analyses/{run_id}` 读取结果，`GET /replay` 离线核验保存的原始输入、计算及模型轨迹，不重新请求模型。年度严格比较仍使用已审核比较栏或可比的已审核上年年报。

`FINREPORT_ANALYSIS_AGENT_MAX_ROUNDS` 默认 16，限制单次执行的工具动作轮数；它不限制整个测试累计 token。离线重放核对保存输入、工具轨迹和结果，真实模型独立重复运行则另行比较计算值、风险方向、判断类别及材料缺口，允许措辞不同。

开发执行器通过持久任务 owner、可续租 lease 与过期恢复处理丢失的后台回调，取消后旧 worker 不能提交完成；这是本地开发恢复机制。趋势返回单位与期间，拒绝陈旧事实快照、旧公式及同期间不明确的多个版本。同业接口 `peer-gate-v2` 在受信的样本审批和覆盖率门禁下提供群体统计、合格目标值、与中位数的绝对差及数值降序名次；同值共享名次，名次不表示经营优劣。目标没有同口径合格财报或版本有歧义时拒绝；没有受信审批配置时仍拒绝，不能拿任意样本演示成正式行业结论。

尚未声明任何行业模块为 `supported`：这需要技术文档规定的 5 家发行人 gold 样本、双人校对及行业专项自动化测试后再启用。当前开发任务执行器使用 FastAPI 后台任务；部署到 Celery/Redis 前不得将其作为多进程生产队列使用。Worker Host 已提供 Celery 组合根，API 与 Worker 共享 `finance-application`、`finance-domain` 和 `finance-adapters`。桌面端通过 `FinReport.sln` 构建，API 可被 `contracts/openapi/v1.json` 契约消费。

## 表格 OCR 对比工具

运行 `.\tools\run-ocr-comparison.ps1` 可用本地 PaddleOCR / PP-StructureV2 与配置的 OCR HTTP 服务识别同一页，保存原图、完整表格、逐格差异与耗时的离线报告。默认样例为本地比亚迪年报第 126 页；其他图片/PDF 页及历史结果复用方式见 [表格 OCR 对比说明](docs/ocr-table-comparison.md)。工具使用独立 uv 环境，并分别核对数字文字覆盖与表格行列归属。

离线对比工具的远程引擎需使用与 OCR provider 相同的 HTTP 配置；正规电子财报优先读取 PDF 文字坐标的实测、表格恢复限制和局部 OCR 回退建议见 [可行性分析](docs/pdf-text-extraction-feasibility.md)。
