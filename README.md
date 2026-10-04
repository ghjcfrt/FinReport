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

打开 `http://127.0.0.1:8000/docs` 查看由 FastAPI 生成的 API 契约。上传与任务创建均返回 `202`，客户端轮询 `/api/v1/jobs/{job_id}`。

```powershell
uv run pytest
uv run ruff check .
uv run python tools/generate-openapi.py
dotnet build FinReport.sln --nologo
```

也可以直接运行根目录的一键构建脚本。它会依次同步依赖、检查架构、执行 Ruff、运行测试、生成 OpenAPI 契约、构建 Python 后端分发包、构建 Rust Scanner Supervisor（检测到 Rust 时）并构建桌面端：

```powershell
.\build.ps1
```

Windows 下也可以双击 `build.cmd`。所有可测试产物会整理到 `dist/FinReport-test/`：其中包含桌面端、Python 后端包、Rust Scanner、ClamAV 运行库和 `run-api.cmd`、`run-desktop.cmd`、`run-all.cmd` 启动脚本；双击 `run-all.cmd` 会启动 API 和桌面端，构建结束后窗口会等待按键再关闭。单独的 Python 包仍输出到 `dist/`，Rust Supervisor 输出到 `apps/scanner-supervisor/target/release/`，ClamAV shim 输出到 `tmp/clamav-experiment/`。调试时可跳过不相关阶段，例如 ` .\build.ps1 -SkipDesktop -SkipScanner -SkipNativeShim`；完整参数包括 `-SkipSync`、`-SkipTests`、`-SkipOpenApi`、`-SkipPythonPackages`、`-SkipScanner`、`-RequireScanner`、`-SkipNativeShim`、`-RequireNativeShim` 和 `-SkipDesktop`。

## 已实现边界

- 内容寻址的 PDF 保存、SHA-256 去重、任务状态与失败审计；本地文本层解析会保留候选值、页码、行列定位与置信度。
- 候选事实的双人独立复核、修改产生新版本、资产负债平衡校验，以及严格指标准入。
- 通用非金融指标、输入快照哈希、趋势查询、样本不足时拒绝统计对标、受限的 OpenAI 兼容模型结论任务、HTML/CSV/PDF 导出。
- 未登记模型或模型调用/claim 校验失败时，分析任务会失败，不会回退生成模板结论。
- `apps/desktop/FinReport.Desktop` 提供 .NET 10 WPF/MVVM 上传、取消超时和任务轮询入口；它仅通过 API 工作，不访问文件存储、数据库或模型服务。

尚未声明任何行业模块为 `supported`：这需要技术文档规定的 5 家发行人 gold 样本、双人校对及行业专项自动化测试后再启用。当前开发任务执行器使用 FastAPI 后台任务；部署到 Celery/Redis 前不得将其作为多进程生产队列使用。Worker Host 已提供 Celery 组合根，API 与 Worker 共享 `finance-application`、`finance-domain` 和 `finance-adapters`。桌面端通过 `FinReport.sln` 构建，API 可被 `contracts/openapi/v1.json` 契约消费。
