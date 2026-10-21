# 语义治理推进与逐阶段验证（2026-10-19）

本文承接[结构化候选核查](grounded-analysis-validation-20261019.md)，记录本轮离线修复、主智能体直接核对及独立反例审查。没有新增供应商模型调用，没有把构造预测冒充模型输出；历史原始响应、原 PDF 和既有评测回执保持原样。

## 1. 本轮修复范围

| 问题 | 具体失败方式 | 修复及边界 |
|---|---|---|
| 两段真实引文被错误关联 | 正文逐字引用来源 A，来源 B 真实存在却与正文无关；旧支持门允许 B 作为 rationale | 新运行默认 `claim_support_v2` / `final_claim_support_v2`。完整原文仅作为带来源标签的观察；依据和逐行 reason 仅允许中性边界或服务端认证固定模板。未知解释保留候选，不判为假，也不进入已接纳事实 |
| 标签与预测同时引用错误 span | 二者登记相同错误坐标，旧严格参考匹配可能仍一致 | 新增独立 v3 评测，核对冻结全文、授权来源、文档和文字哈希、实际切片、直接与规则证据；旧 v1/v2 合同和历史成绩不改义 |
| 完成记录在重入时被升级 | 重新进入主分析函数可能按新版本处理旧记录 | 已完成记录先按保存策略校验，只读返回；校验失败不覆盖、不重发模型。新旧策略检查点明确拒绝混用 |
| 不同工作流共用恢复入口 | 通用 resume 只检查状态，能把 report 试验记录续接成严格分析记录 | 恢复接口和严格执行器均校验显式登记的严格工作流版本；异类或未知版本在构建证据包、改库及任务派发前拒绝 |
| 同一严格任务重复投递 | runtime 或 Celery 对相同 ID 的重复投递均能到达模型；后写轨迹会覆盖前一执行 | 执行器只认领 queued，以数据库条件更新原子转为 running；未认领者退出，已失败或待补件记录不能自行重发 |
| 候选表格金额与算术受环境影响 | 括号负数解析被 Decimal 精度舍入，指数范围和 clamp 改变结果或触发 Infinity；总计与层级行的适用条件不足 | 精确变号、完整固定运算上下文及有限值检查；机械结构不适用时拒算，平坦加总另存未认证假设，不认证会计语义 |

这些措施控制可进入结果的新增断言，不是通用中文语义蕴含模型，也不意味着深层财务分析已完成。

版本名分属不同合同：`claim_support_v2` 是工作流准入策略，`final_claim_support_v2` 是综合输出准入策略，评测 v3 是冻结来源计分合同，`grounded-report-candidates-v5` / `grounded_evidence_view_v2` 是实验候选及只读视图合同，不能互换或用版本号推断能力覆盖。

## 2. 通用错链：输入、输出和错误阶段

独立审查采用未写入修复规则的合成反例：

```text
来源 A：营业收入按履约进度确认。
来源 B：本期银行借款采用抵押担保方式。
content：原文披露：营业收入按履约进度确认。
rationale：本期银行借款采用抵押担保方式。
source_assertions：完整引用 A 和 B。
```

两条引用身份均正确，B 不能由此成为 A 的解释依据。历史无策略及 v1 的 finalize、原始回放校验均接受此输入；新 v2 在两入口都以 `CLAIM_SUPPORT_UNVERIFIED` 隔离 `/claims/0/rationale`，并保留两段来源、原始依据和准入策略。问题出在旧程序的主张支持检查，不是引文不存在或金额不一致。

新增回归还覆盖主体、期间、合并/母公司口径与条件错接，以及模型伪造 `verified_text_fields`。认证模板由服务端基于原文行标签生成，模型不能自行授权。该修复不依赖“非金融资产类”等个别词语黑名单。

独立实跑回执：`tmp/semantic-validation-20261019/independent-v2-v3-audit/independent-final-wrong-link.json`。其中来源和预测为构造反例，不是真实公司错误或供应商新输出。

### 恢复入口的类型串线

追加审查发现，`run_report_analysis` 本体要求 queued 并原子认领，不存在已完成记录重入写回问题；但通用 `POST /api/v1/analyses/{id}/resume` 原先只检查状态。对一份同时具备严格年度证据包的财报，将 `report-evidence-analysis-v1` 的 partial 记录送入该接口，旧代码返回 202、把旧记录改为 continued，新建默认 `analysis-v1` 记录并复制 report trace，随后派发严格分析执行器。测试以派发 stub 阻断真实调用，因此没有产生新模型费用。

这是路由和工作流隔离错误，不是公司财务数据问题。修复采用共享版本集合，不以 `analysis-` 前缀猜测合法类型；report、grounded、未知和缺失版本均返回 `ANALYSIS_WORKFLOW_TYPE_MISMATCH`。正确的已登记历史严格版本继续保留既有恢复行为。新增 42 项测试覆盖错误类型不建包、不改库、不派发，以及正确类型单次派发、保存原输入哈希与 trace、已完成旧严格记录只读校验。

复现回执为 `tmp/semantic-validation-20261019/independent-v2-v3-audit/report-analysis-reentry-route-audit.json`；该回执保留修复前行为。修复后独立回执为同目录 `report-analysis-reentry-route-after-fix.json`，并由 `test_analysis_workflow_kind.py` 对照验证，不回写旧回执。

### 严格执行器的重复投递

另一项独立探针分别通过生产 `WorkflowRuntime` 后台任务和 Celery 任务入口，对同一严格分析 ID 各投递两次。旧实现仅对 completed 提前返回，第二次执行在数据库已经 running 时仍进入模型 stub：每个入口记录 2 次调用、2 条生成审计，最终保存的 trace 却只有 1 个 attempt。这不是已复现的普通 API 双击绕过；缺口位于执行端不能防御重复投递。探针阻断外网，不产生供应商调用。

修复后，工作流类型和 completed 只读检查保持在前，其余仅 queued 能尝试数据库 `UPDATE ... WHERE status='queued'`。认领成功与版本更新一并提交后才构建执行轨迹；认领失败 rollback 返回，不覆盖获胜执行的输出、版本或 trace。运行中、失败、待补材料和部分结果记录均不能靠直接重入执行器再次调用模型，恢复仍走显式恢复接口。

主智能体在首个模型 stub 阻塞时，独立投递第二个任务并确认它先返回，再释放首个 stub。两个生产入口均仅 1 次 stub 调用、1 条生成审计、1 个已保存 attempt，源码前后哈希相同，脚本退出码为 0。修复前后回执分别为 `strict-duplicate-dispatch-audit.json` 和 `strict-duplicate-dispatch-main-after-fix.json`，均在上述独立审查目录。新增 16 项软件测试还覆盖两独立数据库 session 同时读到 queued、陈旧 ORM 对象、认领事务失败和提交成功但确认丢失。

这里完成的是原子认领，不是租约、fencing 或崩溃接管。若认领已实际提交但确认丢失，保留 running 和既有记录，不自动夺取所有权或重新收费。真实 worker/Redis 部署及多主机恢复仍需单独验证。

## 3. 原 PDF 与真实模型文字的核对

对象是此前第四次真实调用的比亚迪 2025 年度报告，物理页 39—41；不是半年报。PDF SHA-256：`710d8b2e08e37ba521f1c4071d2c3e2369d3cadabc21a45073215d9f72138b0b`。

主智能体直接使用本机 PyMuPDF 重新读取三页，并查看物理页 40 的渲染图：三页文字与冻结来源逐字一致，三条 raw evidence quote 均唯一命中。原 PDF 与运行 JSON 的字节未变。

| 阶段/字段 | 可检查事实 | 判断 |
|---|---|---|
| 原 PDF 第 40 页 | 问项为“报告期内公司经营活动产生的现金净流量与本年度净利润存在重大差异的原因说明”，选项为 `□适用 不适用` | 不将该问项更名为调节表，也不据此断言调节表存在或不存在 |
| 原始 `/candidates/2/brief_basis` 及 `statement` | 前者写“原文在调节表问题上勾选‘不适用’……”，后者也改称“调节事项” | 原始生成改称问项；错误在模型自由文字，非提取或逐字引用阶段 |
| 原始 selected option quote | 独立选中项字段写“不适用”，漏 ``；完整引文及 options_quote 仍保留勾选符 | 精确选项字段不合合同；不是识别成了相反选项，也不是整段引文都漏符号 |
| 原始 `/candidates/1/relation` | 模型填入年度起止日期、`cumulative` 期间标记及 `scope.amount_unit=单位：元`；旧 source 的 period 及 amount_unit 为 unspecified；合并口径和币种仍为 unspecified | 来源上下文登记合同冲突；不能仅据此认定 PDF 期间或财务原因错误，也不等于原文没有披露原因 |
| 原始政策候选的引文 | 实际引用研发投入占收入比重变化问项 | 字面真实，但不能作为会计政策变更缺失的证据；任务相关性未被认证 |

经营现金流净额为 **59,135,544,000 元**，比较期 **133,453,873,000 元**，原披露同比 **-55.69%**。独立 Decimal 复算为约 **-55.68840179%**，与显示精度相容。原文原因是“主要是购买商品、接受劳务支付的现金增加所致”；这只是公司披露归因，不是独立因果确认。

同页共 10 行同比检查中，7 行正比较基数与显示精度相容，3 行负基数保持未评估，不能写成 10 行均通过。只读 `evidence-view-v6-current.json` 显示 2 条非正式候选登记、1 条被抑制候选；`semantic_support_verified`、`task_relevance_verified`、`formal_admitted` 和 `financial_analysis_complete` 仍全部为 false。

详细回执：`tmp/semantic-validation-20261019/grounded-task-live-v5-primary-pdf-audit.json`；只读视图：`tmp/semantic-validation-20261019/grounded-task-live-v5/evidence-view-v6-current.json`。原始错误不回填，不以新视图替换历史输出。

## 4. 冻结原文绑定评测 v3

v3 在现有严格参考比较之外，新增实际冻结来源合同，见[v3 说明](semantic-evaluation-v3.md)。每个 case 明确文档、授权来源、全文及文字哈希、物理页号；标签非法时拒评，预测非法时逐条记录 `source_binding_errors`。

直接证据和规则自己的 evidence links 都核验。缺失预测降低覆盖率并单列，不伪造成来源错误；有效不足处置可用 `limitation + requires_evidence=false`。沿用 v2 的 `fact + unsupported + admitted=false` 仍会严格失败，这不是新的拒绝语义。

独立反例实跑覆盖越权/未声明引用、未知来源、跨文档来源、缺失预测、错误输入哈希，以及 CLI hardlink、大小写和 `..` 路径别名、已有输出和重复写入保护，未发现可复现的新绕过。已构造的 Pydantic 对象即使被改写嵌套字段，评测入口也重新验证。

真实来源回归参考使用比亚迪 Q1 物理页 2 的营业收入披露，仅 1 条、2 项金额检查，标签明确为 `regression_reference`，预测亦为构造参考。最终两次 `result-final-1.json` / `result-final-2.json` 字节一致，SHA-256 为 `4214a67b3edf33bf4474a8f497e9bdaad2b80dac93be0c6ff952d00533559320`。正确参考 strict 为 100%，错位 span 和缺失预测为 0%；这些分数检验评测器，没有验证该样本同比、模型或金融准确率。

v3 不打开 PDF，不独立认证文档及页号真实性；冻结引用身份正确仍不能证明主张蕴含、规则正确性和经济事实真实。

## 5. 当前语料和可复现性的实际范围

57 条冻结语料在当前 v1、v2 门级策略下各重跑两次：均为正例 19/32 准入、负例 0/25 误放，13 条有效改写仍被隔离。两遍回执字节一致，SHA-256：`16a1da74e47b9cdfd1e92326c631e81a4ac04f2b14a70b02bc6957f45e37c6d9`。旧结果及语料文件未变。

这 57 条没有覆盖新发现的真实引文错链，故 v2 计数不变不能说明新修复无效。全为 synthetic 或 regression_reference，independent_gold 为 0；也没有恢复被隔离自由改写的可靠验证能力。

历史回放的当前实跑结果：

| 对象 | 结果 | 具体限制 |
|---|---|---|
| 52 份旧工作流 | 49 通过、3 兼容失败；49 个成功结果哈希与旧基线相同 | 3 项仍缺旧 payload profile，不臆造缺失提示；新错误码与后续 prompt-profile 基线一致 |
| 11 份固定 grounded 导出 | 两遍共 22 次正式 CLI 通过，完整回执相同并与 v6 基线一致 | 原任务若失败仍记录 failed；回放成功不代表任务成功 |
| 另 1 份真实 live-v5 导出 | 另行双回放 2 次通过，完整回执一致 | 不混入原 11 份分母，不等于重新调用模型结论一致 |

最终严格执行器源码冻结后，历史审计进程退出码为 0，全部原始文件与旧基线字节未变。52 份中的三项错误码由早期 `REPLAY_AGENT_REQUEST_MISMATCH` 细化为 `REPLAY_AGENT_LEGACY_PAYLOAD_PROFILE_UNAVAILABLE`，失败状态未变；不能宣称 52 项结果与最早基线全部完全相同。逐项退出码、输入/源码哈希和差异保存于 `tmp/semantic-validation-20261019/final-claim-source-history-audit/summary.json`，独立退出回执为同级 `final-claim-source-history-audit-exit.json`；更早轮次的回执原样保留。

### 新增原页表格区域适配

独立 `composition_regions_v1` 通路进一步补充已发现的构成表识别缺口，当前算法标记为 `composition_region_flat_arithmetic_v2`。它用统一 `lines_strict` 几何策略识别选页有线表，先核对原生字符流与完整原页文字全等，再为每个 cell 保存原页 span 列表、物理 bbox、表头、单位和逐列表头明示的期间身份；不按报告名特判，也不接入旧 v2—v5 或正式事实准入。横杠、空值、非法数字和未绑定单元格不当作零。

| 独立报告 | 提取结果 | 条件算术/未解决项 |
|---|---|---|
| Q1 p2 | 8 行、1 金额列 | 登记扣减惯例下合计 983,358,000.00 元 |
| H1 p7—8 | 5+3 行、2 个区域 | p7 无合计；p8 无重复表头或明确续表锚点，期间和单位不继承，不跨页闭合 |
| Q3 p2 | 8 行、2 金额列 | 本报告期 931,889,000.00 元，年初至期末 2,842,681,000.00 元；两列不合并为一个期间 |
| 年报 p12 | 8 行、3 金额列 | 2025/2024/2023 年分别 3,173,319,000.00、3,271,459,000.00、1,578,379,000.00 元 |

合计计算显式保存少数股东影响额在减项区内扣减的惯例假设：本行未明示减号，不能把程序假设冒充原文字面事实。每项算术还保存“各行互不重叠、可按平坦明细相加”的未认证假设。6 列条件算术相符只描述给定规则下的金额关系，全部 `flatness_verified=false`、`semantic_support_verified=false`、`table_closure_certified=false`、`formal_admitted=false`。`structure_applicable` 仅表示本版本机械条件满足，不证明实际项目没有隐含父子关系。

主智能体亲自查看 Q3 p2、年报 p12、H1 p7/p8 渲染原图。算法修复后，再独立通过正式 CLI 各提取一次并从原 PDF 核验一次，共 8 次命令退出码均为 0；四份输出与当前算法实施冻结产物字节一致，239 处嵌套原文 span/quote 哈希核对相符，代码及 PDF 前后哈希不变。该计数包含重复出现的锚点，不是 239 个独立财务事实。

当前主核查回执：`tmp/composition-regions-main-review-20261019/cli-audit-v2/summary.json`；实现方四份双次提取与 40 个篡改反例回执：`tmp/composition-regions-20261019-v2/summary.json`，80 次篡改重建核验均拒收。旧 `cli-audit/` 和 `composition-regions-20261019-v1/` 保留为修复前快照；旧算法工件缺少当前算法字段时，当前核验器明确拒绝，不冒称跨算法字节兼容。

独立审查曾复现三类程序问题，均发生在提取或条件算术阶段，没有模型参与：

| 原始输入或环境 | 修复前输出 | 当前处理 |
|---|---|---|
| `(123456789012345678901234567890.12)` | 默认精度下括号变号已舍入，原本相等的表被报不一致 | 不受上下文影响的精确变号，原金额完整保留 |
| 原表 `100.00 - 20.00 - 5.00 = 75.00`，调用方缩小指数范围或启用 clamp | 可能抛 Overflow、输出字符串 Infinity，或只改了数值格式却改变产物哈希 | 固定完整 Decimal 上下文、有限值检查及固定十进制格式；不继承调用方陷阱与格式设置 |
| 总计在首行/中间、总计后仍有成分、小计与其中项被重复相加、列身份不明或重复 | 可以出现无 issue 的条件一致 | 不符合机械结构语法则 `unresolved`，computed/equal 保持 null；明显层级需另建结构，未知隐含层级仍不认证 |

最终独立复核覆盖 27 张程序构造 PDF 表、60 次 Decimal Context 提取、21 项解析观测、6 次 CLI 保护、8 个重算外层哈希的篡改和四份原页重建，34 项独立断言通过。60 次 Context 下各对应输入的数值和产物哈希一致，且外部 Context 未被改变。专项测试为 **43 passed**。这些计数属于不同检查层，不能相加解释为财报准确率。最终独立回执为 `tmp/composition-regions-independent-audit-v3/acceptance-verdict.json`；修复前证据另存于 `tmp/composition-regions-independent-audit-v1/`。

合同、命令与未支持格式见[区域适配说明](composition-regions-v1.md)。仍为单发行人选页实验，隐含父子关系、多栏版面锚点范围、跨页自动连接、OCR 表格及全文覆盖均未因此获得认证。

## 6. 软件验证

生产门禁组合专项为 **94 passed**，其中新增 v2 测试 **23 项**；评测三代组合专项为 **51 passed、1 skipped**，其中 v3 为 28 passed、1 skipped。跳过项是本机账户无权创建真实 symlink，其余路径别名和独占写入用例通过。两个组合有共同依赖，不能简单相加当作全量测试数。

最终工作区完整回归命令为 `uv run --no-sync pytest -q`：**1558 passed、1 skipped、11 warnings，476.58 秒**，独立进程退出码 **0**；外层含启动与记录的耗时为 481.687 秒。日志和命令、逐文件程序/测试 SHA-256、前后字节不变检查保存在 `tmp/semantic-validation-20261019/final-workspace-regression/output.txt` 与 `receipt.json`。本次包含上述全部冻结增量，验证期间程序和测试源码未变。`uv run --no-sync ruff check .` 退出码 0，回执另存 `final-workspace-ruff/`。之前 1457 passed 是门禁与评测阶段基线，1406 passed 是证据视图阶段基线；专项数量不与全量相加。

跳过项仍是本机无真实符号链接创建权限。11 条警告包含弃用 API、测试故障注入产生的 SQLAlchemy identity-map 警告，以及全量顺序下的 5 条 OpenAPI 重复操作 ID 警告。后者此前在 fresh/仅收集/API 前序/单元前序的分开核查中未复现，最终全量仍出现，不能称已解决；也没有据警告就推断生产 router 一定被重复注册。既有定位记录为 `tmp/semantic-validation-20261019/router-openapi-audit-summary.json` 与 `router-openapi-sequence-audit.json`。软件测试数不表示财报准确率，离线回放不表示外部模型重新生成一致。

### GitNexus 结构与数据流检查

最终使用本机已有 GitNexus 1.6.12 执行 `node .gitnexus/run.cjs analyze --index-only --pdg`，退出码 0，持久化 19,007 节点、61,525 关系、136 个 cluster 和 299 条流程；程序和测试源码前后不变。`status --json` 返回 `up-to-date`、runner identity schema 4/current、`contentDrift=current`，索引与 HEAD 均为 `0cd2bee6ed89ee1bfea9cb8e8ad2948869688215`。

`detect_changes(scope=all)` 的本次结果列出完整的 90 个已映射变更符号和 48 条受影响流程，涉及 21 个 Git diff 文件，风险为 `critical`；没有返回 partial/truncated 标记，列出数量与 changed_count 相同。主要覆盖最终综合/支持门禁、回放、严格执行、恢复、模型客户端及报告实验链路。该风险评级表示整个未提交工作区变更范围较大，不是 90 个缺陷；本轮不据此提交或声明全库无回归。

该检查仍有明确盲区：未跟踪新文件不保证由 Git diff 自动纳入；本轮新增模块已另行阅读、专项测试和独立反例核验。索引日志还报告 C# namespace 扫描截断、16 处跨语言属性未链接，以及 371 个候选入口未排名、651 个 callee 分支跳过、25 条遍历预算截断。成功刷新和 provenance 的 incompleteReasons 为空，不能消除这些分析覆盖限制，空调用集合也不表示代码无影响。

对严格执行器、来源评测和构成表三个文件的 `explain` 未返回持久化 taint findings；闭包、属性和隐式流等仍不在其能力内，不能把零结果作安全证明。`run_analysis` 的 controls 查询在工具上限下显示 200/216 条边并标记截断，只作守卫顺序的辅助材料；本轮对 queued/认领失败出口的结论以直接源码及两个真实入口的并发 stub 实测为依据，不称完成了该函数的全量 PDG 证明。

全部图回执位于 `tmp/semantic-validation-20261019/final-workspace-graph/`，其中 `status-observed.json` 是命令工具输出的观测摘要，不冒充 CLI 原始捕获。静态图检查、软件回归和离线复现共同约束本轮变更，但不替代全文金融评测。

## 7. 当前边界和下一步的证据要求

1. **自由解释的通用语义支持尚未证明。** 门禁能隔离未认证文字；无法因此认定模型正确。放宽前需独立、多发行人、保留条件/否定/口径的测试金标准，登记有效改写与拒绝类标签，盲测误放、误拒及任务覆盖。
2. **已有来源不等于任务充分利用。** 当前政策候选使用研发问项，不能当政策证据；完整引文、引用身份和算术相容都不能替代任务相关性及分析完成判定。
3. **全文和跨页构成表仍有工程缺口。** 需保留原页 offset、表格区域、多列期间和跨页接续，不能只改“和/及”等词就宣布通用表格已支持。
4. **WP4 自动事实准入、跨进程崩溃恢复和桌面入口仍有工作。** 它们是可继续开发的工程任务，不能包装成客观无法实现；本轮完成的是支持门禁和评测基础，不代表这些工作包通过。
5. **历史三份缺失 payload profile 不能猜补。** 需要原始请求或可认证的旧版本材料；保留明确失败比生成一个可通过的替代请求更可追溯。

本轮没有提交 Git，没有将测试财报和 tmp 工件强制加入跟踪。正式运行仍以自动校验为目标，测试金标准只用于评测，不恢复人工审核作为产品准入。
