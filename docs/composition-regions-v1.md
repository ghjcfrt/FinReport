# 原页区域绑定的非经常性损益候选观察 v1

本工具是独立、显式选择的离线通路，不改变现有 v2—v5 请求、生成、回放合同，不进入正式事实或金融结论准入。全程不调用 LLM、不使用网络、不改写输入 PDF。它补充“真实原页有表，但现有 facet 没有 composition 观察”的工程缺口。

当前仍为尚未接入生产的 `composition_regions_v1` 受限合同，算法纠错轮次以 `algorithm_version=composition_region_flat_arithmetic_v2` 单独标明。原 v1 冻结回执保留；当前核验器明确拒绝缺少当前算法版本的旧回执，不把旧算法回执冒作当前算法回放。目录名中的 v1/v2 表示实验轮次。

## 数据合同

公开入口位于 `finance_adapters.providers.composition_regions`：

- `extract_composition_regions(pdf_path, page_numbers)` 返回 `composition_regions_v1`。
- `verify_composition_regions(artifact, pdf_path)` 从提供的原 PDF 重建，完整比较新旧产物，不只检查产物自带哈希。

| 对象 | 保存内容及约束 |
| --- | --- |
| `sources` | 文件 SHA-256、物理页号、未经变换的完整原页文字、原页文字哈希和物理页 bbox；source ID 绑定文档哈希和物理页。 |
| `regions` | `lines_strict` 找到的有线表区域、物理列边界、原页标题和单位锚点；标题须在表前限定距离内，且中间不得跨新章节。 |
| `columns` | 标签/金额/说明列、原页表头 cell、逐列明示期间身份。识别“本报告期金额”“年初至报告期末金额/年初至报告期期末金额”和显式年份金额；裸“金额”保留 unknown，不从文件名或报告登记猜测。 |
| `cell` | 原页 source ID、物理 bbox、精确 span 列表、几何抽取文字、由原页 span 派生的展示文字、绑定状态。先证明原生 `rawdict` 字符流与完整原页文字全等，再按 cell bbox 绑定字符 offset；不重新拼表后伪造 offset。 |
| `amount` | 原始 token、Decimal 数值及正负号形态；括号负数、原生正负号可解析，空格/横杠/非法数字/绑定失败各自保留状态，均不当作零。 |
| `rows` | 原标签及原始 cell、component/deduction/total/unknown、算术操作符、操作符依据和显式假设。 |
| `arithmetic_checks` | 原显示金额在登记规则下的条件算术及状态；unknown、不完整行和缺合计不计算。所有结果 `formal_admitted=false`。 |

原页 span 列表允许一个 cell 的文字在原生抽取顺序中被其他列隔开，但每一段仍必须精确引用同页原文。几何 cell 文字与原页字符不匹配时保留 `geometry_text_mismatch`，不认证绑定。空 cell 是缺值，不合成一个原文不存在的零。

“减：”是原文的显式扣减锚点。紧邻该减项区的“少数股东权益/损益影响额（税后）”可按登记的扣减惯例做条件算术，但本行可能没有减号，因此同时保存 `operator_assumption`。这不称为已认证表格勾稽，所有 `table_closure_certified` 均为 false。期间不明、单位不明时，只能保留裸显示数条件算术，不赋予已确认期间或货币含义。

每项算术还必须保存 `calculation_assumptions`：选取行假定为可按平坦明细相加、互不重叠，但此假设未经语义认证。`flatness_verified`、`semantic_support_verified` 均为 false。`structure_applicable` 仅表示本版本机械适用门槛通过，`structure_applicability_scope` 明确为 `mechanical_conditions_only_not_semantic_flatness`；它不是平坦性、财务正确性或会计含义的认证。

机械门槛要求唯一“合计”处于末行，构成项在扣减区之前；合计前后顺序错误、显式小计/包含行、层级编号、未知列和重复的明示期间身份会保留结构问题，不执行平坦加总，`computed_total_under_rule` 与 `equal_under_rule` 均为 null。显式层级线索只是必要检查条件，未标记的隐式父子关系仍需其他证据与独立评测；本工具没有解决无限自然语言语义，也不靠列举金融错词认证结构。

数值解析使用无损符号操作和十进制普通记法。算术使用完整固定 Context（包含精度、舍入、指数范围、traps、flags、clamp、capitals），不继承调用方 Decimal 环境；检测非有限数和精度异常，不把 Infinity 或舍入结果登记为相等。

## 续页与覆盖边界

相邻物理页、列数相同、列边界相似只建立 `unverified_continuation` 候选关系，不证明同一张表。当前 v1 不执行认证续表连接，也不继承前页期间或单位；无重复表头/明确续表锚点时不跨页算闭合。

当前仅支持选定原生 PDF 页的有线表、明确“非经常性损益项目及/和金额”标题和明确列语法。无框线表、OCR 表、旋转/异常字符流、复杂跨页表头、合并金额列和其他章节名需后续独立适配。没有识别到表或行，不表示财报未披露；行齐全、算术吻合也不表示模型已理解、语义正确或全文无遗漏。

## 离线使用

```powershell
uv run --no-sync python tools/probe-composition-regions.py --pdf "财报.pdf" --pages 2 --output "首次输出.json"
uv run --no-sync python tools/probe-composition-regions.py --pdf "财报.pdf" --verify "首次输出.json" --output "核验回执.json"
```

输出文件使用独占新建模式，不覆盖已有结果；核验入口拒绝重复 JSON key、NaN 和 Infinity。核验只证明当前提供的 PDF、当前提取程序和产物相符，不认证 PDF 发布方真实性，也不替代独立金标准或语义支持评测。

## 2026-10-19 原页实验及算法纠错

原轮产物保留在本地忽略目录 `tmp/composition-regions-20261019-v1/`，修复轮另存 `tmp/composition-regions-20261019-v2/`；四份报告分开处理，不混合为同一报告。原轮被独立反例发现存在 Decimal 上下文依赖、合计位置和非平坦结构未阻断的问题；旧回执不是本轮纠错已通过的证据。

| 报告及物理页 | 区域/金额 cell | 结果 |
| --- | --- | --- |
| Q1 p2 | 1 区域、8 cell | 登记扣减惯例下合计 983,358,000.00 元；表头仅认本报告期身份。 |
| H1 p7–8 | 2 区域、8 cell | p7 5 行缺合计；p8 3 行为未认证续页，单位、期间不继承；不跨页合并、不算闭合。原始总额 1,910,792,000.00 保留在 p8 cell。 |
| Q3 p2 | 1 区域、16 cell | 本报告期及年初至期末两列分开；条件合计分别 931,889,000.00、2,842,681,000.00 元。 |
| 年报 p12 | 1 区域、24 cell | 2025/2024/2023 年明示列分开；条件合计分别 3,173,319,000.00、3,271,459,000.00、1,578,379,000.00 元。 |

本轮每份 CLI 提取两次、分别从原 PDF 核验；同一算法内两份输出字节一致，四 PDF 前后文件哈希未变。与旧轮相比新增算法版本、结构问题、算术环境、适用范围和未认证平坦性假设字段，因此不宣称新旧回执字节相同；实际差异路径保存于本轮 `summary.json`。另对每份做金额、span、期间、区域、单位、删除行、跨 source、操作符、正式准入和续页状态共 10 类篡改，即使重算外层哈希，双次核验均拒收（40 个变异、80 次拒收）。这证明冻结重建能发现这些产物变异，不代表提取准确率或一般语义正确率。

逐项新旧比较确认四份原页 source、表格 bbox、原始列与 cell 行、单位锚点和续页状态均未变；差异为上述新增字段和产物哈希，H1 另在区域 `issues` 汇总原本已经导致其条件算术 unresolved 的缺合计/缺构成项问题。Q1/Q3/年报的六列原显示数与条件合计未变，H1 仍为两段 unresolved，而不是用新版本修成表格闭合。

专项包含合成实际有线 PDF（明确标为程序构造样本）、横杠/空值/非法数字、精确负数、调用方 Decimal 环境隔离、总计顺序与非平坦结构、歧义列、未知期间单位、原页 span、条件算术、篡改、无副作用与 CLI 文件保护：`43 passed`。Ruff 通过。真实原页验证仍是同一发行人的有限样本，无独立测试金标准，无新的模型复跑。

另一个只读审查流程保留了原反例并复测：27 个程序构造表、60 个 Decimal 环境与表的组合、21 个金额解析条件、6 次真实 CLI 调用、8 个重哈希变异和四份当前原页双提取。原位置/嵌套/歧义列反例均停止计算；各组合中当前算法产物稳定，调用方环境未被修改。独立回执位于 `tmp/composition-regions-independent-audit-v3/results/`。这些是受限工程反例覆盖，不是独立金融金标准。
