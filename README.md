# FinReport

当前实现基线：[可实施技术设计 V2](word/可实施技术设计V2.md)。

这是一个可本地演示的、可追溯 A 股财报分析服务。首期默认使用 SQLite 与本地对象目录，生产可通过 `FINREPORT_DATABASE_URL` 切换至 PostgreSQL；PDF 解析只产生候选事实，必须经过两位不同审阅者确认、证据定位和资产负债勾稽后，才能进入严格指标计算。

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
- 候选事实的双人独立复核、修改产生新版本、来源完整性、审计意见、期间/范围/单位、资产负债及现金流勾稽校验，以及严格指标准入。
- 通用非金融指标、输入快照哈希、趋势查询、样本不足时拒绝统计对标、受限的 OpenAI 兼容模型结论任务、HTML/CSV/PDF 导出。
- 未登记模型或模型调用/claim 校验失败时，分析任务会失败，不会回退生成模板结论。
- 桌面端提供年度上传、任务轮询、逐项确认/订正/拒绝、校验展示、严格指标计算、分析结果轮询与 HTML 保存。解析完成后先复核，再计算指标和启动分析。
- 模型数值输出使用结构化观察值并绑定公式输入与原文证据；服务端复算和格式化数值、公式。未完成分析或输入已变更时禁止导出；PDF 使用本地中文字体分页渲染。

当前 OCR 已适配 DeepSeek-OCR 的图像顺序和文档提示。扫描页识别结果可保存为待复核表格，但缺少可信的逐行坐标、单位或合并范围时会阻断严格事实；不会仅凭 OCR 文字宣称核验完成。自然语言的原因解释不能通过数值等式证明，模型推断始终标记待人工复核。

原生 PDF 主报表同时保存本期和上年比较列，比较事实单独复核，不阻断本期指标。`GET /api/v1/filings/{id}/period-comparison` 仅接受同报告、合并、人民币、相邻完整年度及有效证据审核，保留经重述基期；金额基期为零或负值时返回不可比和金额差，不给增长率。利润率变化用百分点，现金流利润比用倍数差。保存 API 响应后可用 `uv run --no-sync python tools/replay-period-comparison.py <结果.json>` 离线复算；哈希与重放匹配仅证明保存输入下的计算一致，不证明原始披露真实。该比较接口独立于当前 LLM 证据包，未向模型开放自由同比推断。

事实审核绑定当时的值、期间、单位、范围、修订及原文证据；输入或证据变化后须重新审核。升级前未保存有效摘要的审核不会被自动追认。指标规则版本为 `general-non-financial-v2`，旧版结果重算后保留为历史，禁止混入新分析或导出。负资本开支支付额拒算自由现金流；此指标采用经营现金流减购建长期资产支付现金的代理定义，不能等同统一 FCFF/FCFE。分析验证采用 v5，正负等可计算命题由服务端验证；跨页或不完整勾选问题禁止独立支持结论，候选省略数量在结果中披露。

开发执行器通过持久任务 owner、可续租 lease 与过期恢复处理丢失的后台回调，取消后旧 worker 不能提交完成；这是本地开发恢复机制。趋势返回单位与期间，拒绝陈旧事实快照、旧公式及同期间不明确的多个版本；同行接口只提供合格样本群体统计，尚不提供目标公司排名或偏离度。

尚未声明任何行业模块为 `supported`：这需要技术文档规定的 5 家发行人 gold 样本、双人校对及行业专项自动化测试后再启用。当前开发任务执行器使用 FastAPI 后台任务；部署到 Celery/Redis 前不得将其作为多进程生产队列使用。Worker Host 已提供 Celery 组合根，API 与 Worker 共享 `finance-application`、`finance-domain` 和 `finance-adapters`。桌面端通过 `FinReport.sln` 构建，API 可被 `contracts/openapi/v1.json` 契约消费。
