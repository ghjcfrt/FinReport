# Qdrant MCP 证据检索

本项目选择 Qdrant MCP，依据是服务端与 Python/REST/gRPC 生态的适配范围、官方 MCP 的维护与部署方式，以及混合检索扩展空间。这是根据部署能力作出的工程判断，尚未进行双方生产长稳实测。轻量化不是选择依据。SQLite 保留为证据权威库与确定性关键词降级路径；Qdrant 返回内容不会直接变为财务事实。

Docling MCP 负责把 PDF 转成带章节、表格和版面信息的 `DoclingDocument`，Qdrant/Chroma 负责对已经切分的证据块做向量存储和召回。本项目已将本地 Docling 接入方案 1 的文本预处理，方案 2 仍使用 Unlimited-OCR；严格财务事实继续使用 PyMuPDF 坐标解析或 OCR 人工映射及原有审核门禁。Docling 转换和向量命中均不自动确认金额或文本。

| 维度 | Qdrant MCP | Chroma MCP | 本项目取舍 |
| --- | --- | --- | --- |
| 数据库与部署适配 | Qdrant 服务端、Cloud、本地客户端模式；Python、REST、gRPC生态 | Chroma 本地/客户端服务及 Chroma Cloud；Python/JS生态 | 选择 Qdrant 的服务端部署和后续跨应用适配路径 |
| MCP 使用便利性 | 官方工具主要为 store/find，模型参数和元数据过滤需配好 | 官方 MCP 暴露集合创建/删除/查看、文档增删改查、查询等更多数据库操作 | Chroma 对直接管理数据库更方便；FinReport 只向 LLM 开放固定业务检索动作，工具数量不是胜负依据 |
| Embedding 适配 | 当前官方 MCP 仅 FastEmbed；模型由服务端固定 | 官方 MCP README 明列 `default`、`cohere`、`openai`、`jina`、`voyageai`、`roboflow`，集合配置保存选定的 embedding function | Chroma 更易接入已有 embedding 服务，这是它的明确优势；本项目先固定可复现的 FastEmbed 模型 |
| 检索扩展 | Qdrant 服务端支持稠密/稀疏、混合检索、多向量及过滤等能力 | Chroma 提供向量/元数据/文档检索能力，业务开发较直接 | Qdrant 扩展空间更符合后续大批财报检索；这些高级能力不会自动由当前 store/find 全部暴露 |
| 稳定性与维护 | 成熟向量服务与官方 MCP；仍要固定版本并验证实际运行 | 官方 MCP 和主数据库生态完善，同样需版本固定、持久化及并发验证 | 已完成 Qdrant 本地持久索引和实际中文 embedding 的四报告选页冒烟测试；尚未做双方生产压力或长稳对照 |

## Docling 的组合边界

官方 [docling-mcp](https://github.com/docling-project/docling-mcp) 提供 PDF 转换、结构化文档缓存和 MCP 工具。本机已在独立 uv 环境安装并运行本地 native 服务，按以下路径接入：

1. 保留原始 PDF 及 SHA-256，调用 Docling 生成完整 `DoclingDocument` JSON，记录 Docling 版本、配置和返回哈希；Markdown 仅作阅读副本。
2. 按章节、表格和连续段落生成文本证据块，写入 SQLite；页码、表格范围和原文哈希必须保留。
3. 将已物化的证据块交给 Qdrant MCP 做向量索引，使用 `filing_version_id` 过滤；Qdrant 不保存唯一权威副本。
4. Agent 先用 Qdrant/SQLite 定位，再读取 SQLite 中的完整证据；财务事实仍由现有坐标解析或 OCR 人工映射产生。

Docling 的结构识别不能保证财报数字、单位、合并范围或列关系正确。当前已核对四报告选页的完整 JSON/Markdown、原始页码映射和 bbox，尚未完成整份中文报告的页码、表格单元格和单位恢复率对照。因此 Docling 保持可选文本预处理器，不替换方案 1 的严格事实路径。转换清单记录服务端实际配置、版本和模型权重清单哈希；客户端要求本地、非 OCR、非 VLM 的服务回执。

源码核实固定为官方提交 `5e1551a9b984f9a26349de6d23d02eccf5a69db2`。其转换工具主要接收 `source`，方案 1 要在服务端设置 `DOCLING_MCP_DO_OCR=false`，不能只靠调用参数声明“直接提取”；旧变量 `DOCLING_DO_OCR` 已被忽略。方案 2 继续显式调用 Unlimited-OCR。

JSON 保存父子引用、阅读顺序、表格行列跨度和存在时的页码/bbox；Markdown 仅为阅读副本。文本入库已保留 `heading_path`、`parent_anchor`、表格标识和 provenance。原始 Docling 子集页码与原 PDF 页码映射分别保留，bbox 统一为 TOPLEFT。通用标题层级不等于财务科目的“减/其中”层级，仍需专项评测。

`save_docling_document` 返回的是 MCP 服务端缓存目录中的 JSON/MD 路径。远程部署必须约定共享输出目录或读取 Docling Serve 的 JSON 响应，不能假定客户端能直接打开服务端路径。官方锚点搜索主要是文本匹配，可辅助定位，但不能替代 Qdrant 的语义召回。

比较依据为 [Qdrant MCP 官方文档](https://qdrant.tech/documentation/qdrant-mcp-server/)、[Qdrant 官方 MCP 仓库](https://github.com/qdrant/mcp-server-qdrant) 和 [Chroma 官方 MCP README 固定提交](https://github.com/chroma-core/chroma-mcp/blob/98ff67589bdcc31b730a5415ff9529433f949077/README.md)。选择 Qdrant 是项目对部署生态、可审计过滤和长期检索扩展的取舍，不能表述成“Chroma 不稳定”或“Qdrant MCP 功能更多”。

## 官方协议与版本

对接官方 `mcp-server-qdrant==0.8.1` 的 Streamable HTTP MCP 协议，执行 `initialize`、`notifications/initialized`、`tools/list`、`tools/call`。本地包装保留官方 embedding 和 `qdrant-find`，增加稳定索引身份工具与 `finreport-store` 确定性 upsert；连接其他官方服务时兼容 `qdrant-store`。客户端依据真实 `inputSchema` 选择参数，不向工具发送模型任意生成的参数。

协议核对来源固定为官方仓库提交 `c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41`：

- [官方设置](https://github.com/qdrant/mcp-server-qdrant/blob/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/settings.py) 定义 `QDRANT_ALLOW_ARBITRARY_FILTER`。
- [官方工具](https://github.com/qdrant/mcp-server-qdrant/blob/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/mcp_server.py) 定义 `qdrant-store`、`qdrant-find`、运行时参数裁剪和 `<entry>` 返回格式。
- [官方连接器](https://github.com/qdrant/mcp-server-qdrant/blob/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/qdrant.py) 定义内容与 `metadata` 的存储、命名向量及检索。

本项目要求 `qdrant-find` 暴露 `query_filter`，以 `metadata.filing_version_id` 隔离财报。如果服务端未开启过滤，客户端明确降级为 SQLite，避免跨报告 top-k 召回挤占当前报告。

## 服务配置

优先复用已配置的 Qdrant 与 MCP 服务。本机已安装官方 0.8.1 包，固定 CPU FastEmbed `BAAI/bge-small-zh-v1.5`（512 维）及实际 ONNX 权重。启动、健康检查、固定依赖与清单见 [本地 MCP 服务](local-mcp-services.md)。模型和虚拟环境保存在忽略的 `data/local-services`，不进入 Git；普通分析请求不安装依赖或下载模型。四报告选页冒烟测试不等于中文语义检索准确率评测。

MCP 服务端环境示例：

```text
QDRANT_URL=http://127.0.0.1:6333
COLLECTION_NAME=finreport_document_chunks
QDRANT_ALLOW_ARBITRARY_FILTER=true
QDRANT_SEARCH_LIMIT=30
EMBEDDING_MODEL=<已安装并经过中文财报评测的 FastEmbed 模型>
```

启动已安装的官方命令：`mcp-server-qdrant --transport streamable-http`。若需要隔离环境安装，再由用户运维流程选择固定版本；普通分析请求不会启动进程或安装依赖。

FinReport 配置：

```text
FINREPORT_QDRANT_ENABLED=true
FINREPORT_QDRANT_MCP_URL=http://127.0.0.1:18768/mcp/
FINREPORT_QDRANT_COLLECTION=finreport_document_chunks
FINREPORT_QDRANT_TIMEOUT_SECONDS=120
FINREPORT_DOCLING_ENABLED=true
FINREPORT_DOCLING_MCP_URL=http://127.0.0.1:18769/mcp
FINREPORT_DOCLING_MCP_TIMEOUT_SECONDS=300
```

网关需要 Bearer 鉴权时使用 `FINREPORT_QDRANT_MCP_TOKEN`。未配置、网络失败、缺失工具或缺失文档过滤能力时，返回 `backend=sqlite`、`degraded=true`，查询仍可进行。

另有独立的可选 Qdrant REST 适配器，使用本地固定维度的词法哈希向量。它明确标识为 `qdrant_rest_lexical`，不等同 MCP，也不等同语义 embedding。MCP 配置存在时优先使用 MCP。

## 证据与审核

`ensure_text_chunks` 按当前选择的 PDF 原生文字/OCR 路径及任务 `page_numbers` 保存文本块，不受年度报告限制。启用 Docling 时，PDF 路径先按所选原页物化其结构化文本，转换失败明确记录状态并回退 PyMuPDF 文本；OCR 路径只读当前任务保存且哈希匹配的逐页响应。所有生产路径先检查文件扫描与可信对象。每块保存财报、原始页码、页哈希、原文哈希、源文件哈希、提取版本及坐标；向量结果必须回查 SQLite 并匹配来源哈希。文本快照变化会使依赖的分析失效。

文本审核独立于财务事实审核。`review_text_chunk` 将确认/拒绝绑定至当前文本、文件、页码、提取方式与坐标摘要；任一绑定内容变化，审核重新变为 `unreviewed`。拒绝块不进入检索结果。文本确认只说明原文核对，不会把其中金额自动升级为严格财务事实。

原生 PDF 的有限阅读摘录使用 `native-context-v5`，遵守所选原页范围：规范摘录最末端的 ASCII 空白，并将已识别为勾选选项的私用字体字形 `U+F052` / `U+F0FE` 转为可见的 `☑`；保留 `raw_text_sha256`、逐字符替换位置、规范化标记、页哈希和原始行定位。无关字形、内部换行及缩进不变。模型引用仍须与提供的阅读摘录逐字一致，不能以规范化为由改写金额、选项或正文。历史证据包保留其原提取版本和引用校验，不回写为新摘录。

人工审核列表单独使用 `GET /api/v1/filings/{id}/text-evidence/review`，支持按页、审核状态筛选，默认保留拒绝块，供查看原文和再次确认。`include_rejected` 仅属于这个审核接口；普通原文读取、关键词/向量检索和 Agent 工具仍排除拒绝块。桌面审核页使用专用列表，拒绝操作后不会丢失重新审核入口。

受控读取只支持资料目录、覆盖范围、关键词/语义检索、指定块/页/章节及事实列表。模型不能指定文件路径、执行任意 SQL 或运行命令。每次操作保留参数、结果快照、命中 ID、后端、降级状态及输入输出哈希；分析 Agent 还保存实际读取的文本和每轮调用记录。

## 已验证与边界

早期 8 维确定性测试 embedding 协议探针仍仅证明协议，不计作语义验证。另使用真实 CPU `BAAI/bge-small-zh-v1.5` 与持久本地 Qdrant，对 2025 比亚迪 Q1、H1、Q3、年度报告原始第 2、7、9、11 页分别执行 Docling、索引和带 filing 过滤的查询；现有完整证据为 `data/local-services/mcp/verification/four-reports.json`，运行命令为 `uv run --no-sync python tools/local-mcp/verify_qdrant_four.py`。

四份独立报告均返回 `backend=qdrant_mcp_hybrid` 和有效向量候选，重复索引新增为零，重复查询结果一致，显式联合模式仅使用选择的四个 ID。另验证坐标变化使文本确认失效、启用开关但缺少 URL 时明确降级 SQLite。本地 `finreport-store` 用稳定 UUID5 upsert，服务端重试点数不增加；缓存绑定索引代次、模型和向量身份，重建后不能错用旧索引记录。

这证明所选页的真实 embedding 检索、受控过滤、审核绑定和重复执行可运行，不能证明全报告召回率、生产集群长稳或模型新结论逐字一致。完整召回、重排、并发和长期性能仍需专项评估。`rebuild-qdrant.ps1` 保留旧索引目录并创建新代次；权重清单、服务配置和转换快照保存在本地运行证据中。
