# 已知限制与路线图

> 这份文件列**当前版本已知做得不够的地方**，避免用户按 README 的期待踩空。
> 与 `CHANGELOG.md` 的分工：CHANGELOG 记"改了什么"，本文件记"还差什么"。

## 一、形态与协议

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~代理的流式是"单 delta"~~ | **已解决（0.2.0）**：`stream:true` 时上游 SSE **逐行转发**（chat／anthropic／responses 三种上游格式），每个文本增量一个 delta 事件；流末仍固化、确认块作为末尾 delta 追加 | — |
| ~~代理无鉴权~~ | **已解决（0.2.0）**：实例令牌首次启动生成（`<数据根>/instance_token`），未带 `Authorization: Bearer <令牌>` 回 401；`doctor` 只显前 8 位 | 客户端需带上令牌（README 有示例） |
| chat → responses 组合 | 回 501（与移植来源同口径） | 需要该组合时请把上游 `api_mode` 改成 `responses` 或 `anthropic` |
| ~~工具调用（function calling）未联调~~ | **已解决（0.2.1）**：chat／anthropic／responses **三向都带 `tools` 真服务端到端**（`tests/test_n15_proxy_tools.py`） | — |
| ~~代理 session 分桶不可配~~ | **已解决（0.2.1）**：`proxy.session_bucketing`＝`day`（默认）／`hour`／`none`（不分桶）；显式传 session 头仍以客户端为准 | — |
| ~~`/v1/models` 只回占位名~~ | **已解决（0.2.1）**：回**配置的模型名**（`llm.model`／`HIPPOCAMPUS_MODEL`），未配置才回内置名 | — |
| ~~服务化没有管理口（E1）~~ | **已解决（七轮 T3，随下一个 tag 发）**：`hippocampus serve` 起管理口，三端点 `/run`（执行一轮注入＋固化，回 `run_id`）／`/trace?run_id=`（取该次注入的全链路审计）／`/health`（索引与库健康）。**默认只绑 127.0.0.1＋实例令牌**；要出网卡必须显式 `--allow-remote`，无令牌则拒起 | 与代理形态同一端口（二择一起动）；**不起上游转发**（不把这一轮话转给模型作答），但固化阶段的记忆抽取按配置使用模型端点（可用即出站、仅 https，配不到走规则档）——真机实测订正见 `docs/deployment.md` §二·五第 3 条。用法见 `docs/deployment.md` §二·五 |
| E2（"要不要做 MCP 工具接入"）口径 | **已消解，非缺口（七轮 B1 拍板登记）**：GitTok 线 09-17 上线的 llms.txt＋MCP server 是**对外提供**方向；Hippocampus 正本 §一"不做 MCP 工具接入"说的是**消费**方向（不通过 MCP 接第三方工具）。两句方向相反、不冲突，Hippocampus 侧口径维持不变 | 记录用途：防止以后有人拿 GitTok 的 MCP 反过来说本项目"缺服务化" |
| ~~`GET /health` 免鉴权（管理口探针位）是否收紧~~ | **已解决（八轮 V8）**：绑**非环回**（`--allow-remote`）时 `/health` 同样必须带实例令牌，缺则 401；绑**环回**时维持 0.1.0 以来的免鉴权（本机拨测方便）。`/run`／`/trace` 口径不变（缺令牌 401） | 环回默认＋令牌仍是基线；两态由 `tests/test_n35_r8_health_auth.py` 钉（接线＋行为）。口径见 `docs/deployment.md` §二·五第 2 条 |
| ~~模型口（代理形态）鉴权半成品~~ | **已解决（九轮 W1）**：`serve()` 补齐与管理口同口径的两道闸——非环回必须显式 `--allow-remote` **且**有实例令牌，否则拒起（退出码 2）；非环回时 `/health` 与 `/v1/models` 也要令牌（`/v1/models` 回**配置的模型名**，属信息面，定性为"环回免鉴权、非环回纳入鉴权"）。环回档行为一字未改 | 两形态鉴权面自此一致；`tests/test_n38_r9_proxy_auth.py` 钉接线＋行为＋CLI 透参。口径见 `docs/deployment.md` §一与 §二·五第 1、2 条。**属默认对外行为变更**（非环回部署会受影响）→ 随 v0.5.0 发版 |

## 二、记忆层

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~三个守卫模块只有单测、未接入生产路径~~ | **已解决（0.2.0）**：`retrieval_guard`／`missed_extract` 接进 `MemoryCore.consolidate` 轮末，`disambiguate` 同路径（无端点时软失败跳过），`hub_guard` 接进维护扫描 | — |
| 语义通道的半成品状态 | 默认内置档 `builtin-hash` 是**词法级**表征；装 `[vector]` 并用 `onnx:Xenova/bge-small-zh-v1.5`（**必须带 `Xenova/` 前缀**）才是神经语义检索 | 默认档的同义改写召回弱；实测分布见 `docs/embedding.md`。**换型已于 2026-09-19 复测并关闭（维持内置档）**：中文档在 demo 10 题＝记忆开 **8/10**、关 3/10，同题内置档＝开 **10/10**、关 3/10（20 题全集的旧记录是开 20/20、关 6/20，两套题量口径分列不混写）；**未过 §三 阈值"开 ≥9/10"** → 按护栏不换，负面实测与三档真机变更句对照全部记在 `docs/embedding.md` §七轮换型实测。同轮修掉一处真实漂移：文档与档位表都写裸名 → 加载静默回退 MiniLM **且档位参数整档不生效**（`apply_tier_params` 键名对不上配置串） |
| ~~磁盘/索引异常时的降级是**静默**的~~ | **已解决（0.2.0）**：`doctor` 有"索引健康"行（chroma 可写性＋集合条数 vs 库内 active 条数）；索引写失败/检索失败会记入会话并在注入结果 `note` 里明确告警；索引写入后校验收敛、查询失败自愈一次 | — |
| ~~记忆后端不可替换~~ | **已解决（0.2.0）**：抽出 `MemoryBackend` 协议（`core/backend.py`），SQLite＋Chroma 是默认实现；`hippocampus export/import` 提供目录包迁移（含 schema 版本） | — |
| ~~可求证机制（正本 §三-4 新增设计）~~ | **已实现（七轮 T2，随下一个 tag 发）**：`memory/verification.py` 三级判据（L1 存在性·自洽／L2 库内一致性／L3 外站探测**默认关**）＋三态处置（`verified` 直存留证据／`refuted` 按来源分流：用户挂起询问不静默丢、模型丢弃＋观察日志留痕／`unverifiable` 直存且**不打可疑**）；`memories` 追加四列（全带默认值，v1 只追加契约成立）；开关 `verification_enabled`（默认开）／`verification_external`（默认关）；观察轨那条前身幻觉判据已收敛到同一入口；验收锚点 A42–A45 见 `tests/test_n30_r7_verification.py` | 边界不变：**只对内容里可机械校验的结构求证**，不做"事实正确性"承诺、不对偏好求证、不用模型做判据。八轮 V10 补上真机一侧：显式开 `verification_external` 跑通 **verified／refuted／未取证** 三态与缓存命中（默认档实测零出站），并修掉"HTTP 4xx/5xx 被吞成未取证 → 死链判假分支不可达"这个只有真机才会暴露的缺陷；记录见 `docs/verification-design.md` §五·五，**默认仍为关** |
| ~~模型输出的观察轨没有生产调用点~~ | **已解决（0.2.1）**：`consolidate` 的 `assistant_text` 分支调 `extract_response`（模型输出→`shadow=1`，永不注入），两形态共用；受学习开关约束，离线档零调用 | — |
| ~~向量索引段偶发读不到（hnsw）~~ | **已解决（0.2.1）**：**根因**＝会话初始化时手工 `DELETE FROM chroma.embeddings_queue`（改内部表，会把还没落段的写入抹掉）＋ 段 reader 建不起来时只重试不修；现改为**只写配置**（`automatically_purge`，回收交给 chroma）＋ 打开会话时**预热探测** ＋ 读失败→**从 memory.db 重建向量池**（实测：重试/再 upsert 都无效，重建有效）＋ `hippocampus index rebuild` 运维入口。复现脚本口径见本文件 §七 | 残余风险：chroma 版本升级可能改变内部行为（我们不碰内部表了，只依赖公开 API） |
| 模型口固化阶段遇"上游回体不是合法 JSON"→ 裸 500（**九轮 W6 真机冒烟逮到，本轮只登记**） | 现象：把 `hippocampus proxy` 的上游指向一个回纯文本的端点，`POST /v1/chat/completions` 得到 ASGI **500 `Internal Server Error`**（无 `stage`、无已完成段信息）。链路：`proxy/app.py _handle → core.consolidate → pipeline.process_user_message → extract._extract_with_llm → llm.chat_json` 抛 `ValueError`（JSON 解析失败）。`extract` 只在 `LLMUnavailable` 上降级到规则档，`ValueError` 不在其列。管理口在七轮已把同一段兜成 **502＋回带已完成注入段**（`docs/deployment.md` §二·五第 3 条），两形态口径不一致 | **本轮不修**：改的是错误语义（500→502／是否降级规则档），属对外行为变更，需与 W1/W3 一起进一次 minor 发版并补两形态一致回归；复现随 W6 入 CI（`scripts/live_proxy_smoke.py`，把桩上游回体换成纯文本即红）。判据：两形态遇"抽取器拿到非 JSON"必须同一状态码且回带已完成段 |

## 三、评测

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~题量与轮次~~ | **已解决（0.2.0）**：题集 20 题（14 问 6 动作）；**pass^k**（`--pass-k`，k≥3 时每题重复 k 次全过才算过） | — |
| 题集来源 | 20 题含 synthetic 与 **real-jd**（真实岗位 JD **脱敏**派生场景，不含私人 JD 原文） | real-jd 只保留招聘方普遍考察的能力要求 |
| 作答器 | 离线档是**规则作答器**（不是模型推理）；`--model` 可加模型臂（有端点时） | 离线结论测的是"记忆层有没有把依据给出来"，不是模型答得好不好 |
| 对照 | **关键词基线对照**已实现（`--baseline`，仅 BM25 直查库）；实测：记忆开 20/20、记忆关 6/20、关键词基线 18/20 | 合成样本上的对照，不作普适承诺 |
| ~~评测无 CI 回归阈值~~ | **已解决（0.2.1）**：CI 的 demo 步骤带**阈值断言**（记忆开 ≥9/10、记忆关 ≤6/10），分数回归会红 | — |
| ~~公开基准是"检索口径"不是官方分~~ | **已实现（版本号未升，随下一个 tag 一起发）**：`bench --model-arm` 显式开关接官方判分臂（prompt 与判分算法钉官方仓库 revision，见 `docs/benchmark.md` §二·三）；实测：LME-oracle **全量 500 题官方准确率 74.2%**（CI 70.2–77.8%；早先抽 200 批 70.5% 留作口径历史）、LoCoMo-10 全量官方 F1 **32.55%**（CI 30.8–34.4%，deepseek-chat、temp 0、**输入＝记忆层注入 top-8**） | **输入口径＝8 条注入，不是全文** → 与"把整段对话喂给模型"的报分同表比须带脚注（`docs/benchmark.md` §二·三）；官方分要花钱跑（预算硬停默认 ¥30，实际按余额差如实报） |
| LoCoMo 绝对分偏低 | 英文语料 × 中文标定分词/阈值 × 默认词法级嵌入档 × 离线引文作答器；实测：证据命中 36.7%（LongMemEval-oracle 98.0%） | 要提分得做英文适配或换神经嵌入档（未做，属可用性优化不是纪律问题） |

## 四、可观测与可追责

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~`explain` 候选集上限＝检索 top-k~~ | **已解决（0.2.0）**：旁路 `AuditSink` 记录 **top-N=50 候选全集**（`audit.jsonl`，超限只记计数）；`explain` 能答"那条为什么没进" | 审计默认开（`audit_enabled`），开启时每次注入多一次同量级检索 |
| ~~观测两处分裂~~ | **已解决（0.2.0）**：`explain`（不带 `--step`）把 `observe.jsonl`（注入/确认）与轨迹合并成一份 run 视图 | — |
| ~~审计/观察文件只增不轮转~~ | **已解决（0.2.1）**：`audit.jsonl`／`observe.jsonl` 到上限滚动（`.1`/`.2`…，保留份数可配 `observability.jsonl_max_bytes`／`jsonl_keep`，默认 8 MiB／3 份）；`explain` 可读滚动份 | 极老的日志会被丢弃（保留份数之外），上限可调 |
| 审计开销有实测 | `audit_enabled` 开/关实测：**+41.8 ms/次**（p50，1154 记忆规模库）；默认开 | 嫌慢可关（`audit_enabled=false`），代价是 `explain` 看不到超出 top-k 的候选 |
| ~~冲突确认无 TTL~~ | **已解决（0.2.0）**：pending 块默认 **7 天 TTL**，超时标记"未决冲突"、旧值保持生效；`memory review --pending/--candidates/--suspicious` 三个视图 | — |
| `/trace` 的 `observe[]` 名义是"这一轮"、实际是**整个账户** | **已收口（九轮 W10）**：八轮 V9 只把这条事实写进文档，本轮给出可用收窄——`?observe=account`（默认，行为不变）／`?observe=run` 在该轮审计时间戳（拿不到则退回 `run_id` 前缀毫秒）前后 50 毫秒内**再筛**，响应回显 `observe_granularity`，非法值 400。钉：`tests/test_n45_r9_trace_granularity.py` ＋ 真机冒烟五条新语义（`scripts/live_management_smoke.py`）。首版实现把 `injection` 事件也放进来，"收窄"反而比默认更宽——被真机冒烟当场判红后改成"只会更少" | 残余：**run 粒度是时间窗近似**，同账户并发多 run 时同窗口事件仍会混入；要精确到 run 得把 `run_id` 穿进记忆层的观察写入点（改协议，另案，未列入本轮） |

## 五、工程与发布

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~无版本标签~~ | **已解决**：`v0.2.0` tag＋Release（见 GitHub Releases） | — |
| 未发布 PyPI | 安装走 GitHub 直装 / 源码；分发名 `hippocampus-agent` 已在 PyPI 占位可用 | `pip install` 装不到 |
| ~~出站口径三处不一致（文档说"仅 https"、代码与测试放行公网 http）~~ | **已收口（九轮 W3）**：`validate_outbound_url` 默认**仅 https**（数据/模型提供的 URL），明文公网出口需显式 `HIPPOCAMPUS_ALLOW_PLAINTEXT_OUTBOUND=1`（**默认关**）；本地模型端点走 `validate_endpoint_url`（允许环回 http，行为不变）。`docs/security.md` §②／`docs/deployment.md` §三第 4 条／`src/hippocampus/net.py` 与 `tests/test_a40_a41_safety.py` 四处同一口径，并由 `tests/test_n40_r9_outbound_policy.py` 做"文档↔实现"逐字核对 | **属默认对外行为变更**：抓取记忆里的 `http://` 链接、指明文公网 API 会直接被拒（要显式开闸）→ 随 v0.5.0 发版 |
| ~~无导入/导出命令~~ | **已解决（0.2.0）**：`hippocampus export <目录>` / `import <目录>`（目录包含 manifest 与 schema 版本；导入默认不覆盖，`--force` 时旧库留 `.bak` 副本） | — |
| 演示脚本 | `scripts/demo.sh`／`scripts/demo.ps1`（起代理→灌数据→三格式请求→评测→结果表）；**两版均已实测（2026-09-18）**：`demo.ps1` 修了无 BOM 导致 PowerShell 按 ANSI 解析中文串报语法错的问题（已带 UTF-8 BOM），之后五段全通（doctor→seed→代理 8765→三格式请求→20 题评测 开 20/20／关 6/20／基线 18/20）；**跨会话长任务演示（六轮 G7，2026-09-18）**：`scripts/demo_flow.py` 一键走完三形态（记忆核心/Agent/代理）＋跨会话记忆生效断言（会话 A 写入→会话 B/Agent/代理均复述），4 断言全过（exit 0，离线零凭据，输出样例见结果文档 §十一） | 换系统区域设置（非简体中文）时仍需 BOM 保障 |
| ~~**单条写入随库规模线性变慢**~~ | **已解决（0.3.0，六轮 T6）**：每次 `write` 原触发**全量**索引同步，改为**增量 upsert**（只同步本轮触碰的新记忆／新经历／被取代项）。**成对基线按批引用，不得跨批配对**（九轮 W7 口径）：**批 C**＝2026-09-17 合成库 1154 记忆／2406 实体／6035 关系／497 事件（本文件 §6.1 表）p50 **748.6 ms → 42.0 ms**（同规模同机）；**批 D**＝2026-09-18 五轮 T6 实测（数字正本结果文档 §三，README 性能表照此抄）p50 **632.8 ms → 41.98 ms** | 残余风险：跨账户长跑仍受会话缓存上限约束（`HIPPOCAMPUS_SESSION_CACHE_MAX`）；极端并发写入下增量与全量的收敛差异靠 `index_health`／`index rebuild` 兜底 |
| ~~数字闸只比对头条分，对照表里的多值不带口径标注~~ | **已解决（九轮 W7）**：规则升级为"同一指标 >1 个数值时，每个值必须带（批次／规模／臂）标注"，覆盖 bge 档对照表与写入"修复前／后"成对基线；上闸前先订正三处归属错（46.5／47.8 串到 instr 臂、`46.3%` 无出处、749／632.8 未配批次），钉：`tests/test_n42_r9_number_table_annotations.py`（植入 `bge-base = 50.0%` 必红） | 本文件 §六 的旧口径表**仍不参与**头条分比对（硬边界：旧口径不与 README 增量口径混写）；口径变更须同日改全处 |
| ~~文档章节引用指错、待拍板编号两套并存~~ | **已解决（九轮 W8）**：`§2.3` → `§二·三`（README 与 `docs/benchmark.en.md`）、`docs/benchmark.md` 两段自相矛盾的 judge 表述订正、`docs/release-sync.md` §三 补到本轮；`AGENTS.md`／`.qoder/handoff/STATUS.md` 把待拍板正本写成"P1–P5"→ 改回正本实编号 **B1–B10**（P1–P5 是 v0.3.0 发版任务序，**同名不同物**，两套编号并存已注明）。钉：`tests/test_n43_r9_doc_reference_hygiene.py` | 轻闸只校验"仓内 `§` 引用必须命中、跨仓引用只校验文件存在"——跨仓文档的**内容**一致性仍靠人工（见 K17 简历同源，W12） |
| ~~老库迁移只在真机上手工验过，仓内没有 legacy 夹具~~ | **已解决（九轮 W11）**：`tests/test_n46_r9_legacy_migration.py` 从**现行** `database.SCHEMA` 当场减去"后加列／后建表"派生老库（不放二进制副本、不落本机绝对路径），断言链＝缺 → 一打开就补齐且老行吃默认值 → 二次打开结构一字不差 → 老库经 `MemoryCore` 真读真写。**首跑当场逮到真缺陷并修**：词法档 `lexical_session`（无 chromadb 环境即 CI core-only 的默认档）手抄了一份停在 `ensure_security_schema` 的过时序列，漏了求证四列与 `pending_blocks`，老库在该档一写就 `no column named verification_status` → 改与 `MemorySession` 同走 `apply_migrations` 唯一入口，并加"两档迁移终点必须一致"闸防再各抄一份 | 口径订正：库内**没有**版本号字段（`PRAGMA user_version` 全程未使用，补它＝动 accounts schema，撞硬边界），所以"迁移前后"用**可见的结构差异**断言；真正的版本号在导出包 manifest（`transfer.SCHEMA_VERSION = 1`），其跨版本拒绝文案已被测试钉死（含两个版本号＋指向 CHANGELOG，且拒绝先于落盘），并有"同版本必须放行"对照防闸变常闭 |
| ~~时间预算台账是手写清单，没有机器校验~~ | **已解决（九轮 W9）**：`docs/ci-time-budgets.md` 补全为逐文件全量表，并新增反向校验闸 `tests/test_n44_r9_time_budget_ledger.py`（扫 `tests/`＋`scripts/` 每个等待点，要求"文件＋秒数"同行，缺行即红；植新等待点与"把值塞进外部常量"两类对照均判红）。按判据改判三处功能性预算：`test_n28` 30 秒（八轮首版判错，已公开更正）、`test_n31` `/run` 20 秒（→ `RUN_TIMEOUT_S`＋审计按 `run_id` 轮询完成标记）、`demo_flow` "8 秒起不来"（→ 兜底内就绪轮询，顺带修"非 200 不 sleep 会热转"） | 残余风险：`src/` 不在闸范围内（产品代码的等待值属运行时配置）；台账只保证"每个等待点都被定性登记"，不保证阈值取值的合理性——取值改动仍需人在评审时判它属哪一类 |
| ~~治理项挂"待"字不收口（六项）~~ | **已解决（九轮 W14，逐项终态）**：①**共享 chroma client＝不做**，六轮否决策经复核维持（翻案触发条件写死：≥50 账户常驻 或 峰值占比 >10%）——`docs/chroma-client-sharing.md` 状态行；②**安全 seal 正本改为本仓**（`pip-audit` 复扫＋可达性＋凭据扫描＋CI，每次发版重跑），Mimosa 不在交付链里、只作未来"第二 seal"——`docs/dependency-audit.md` §六 终态口径；③**实例令牌无过期/无吊销＝设计边界不是待办**，撤销手段只有"换令牌＝删文件重启"，对外措辞模板已给——`docs/security.md` §③ 使用边界行；④**不发 PyPI＝不做**，三条理由＋真要发的三步（版本自证 → 干净环境安装验证 → 扫描全绿后用 scoped token）——`docs/naming.md` 末节；⑤`tests/test_zz_dbg.py` **文件名与内容一律不动**（八轮请示无答复，按"无答复只加注记"处理，改名时机绑定下次实质修改）；⑥**八轮"把 tag 从旧位置移到 `49d3a09`"的代拍＝接受，不回退挂 v0.4.1**（理由：移动只发生在未发布 tag 上、GitHub Release 从未指向旧 tag，且 W15 的 v0.5.0 会让"回补 v0.4.1"变成多余噪音） | 六项各自的"翻案触发条件"都写成可判定事件，不留"以后再议"；⑤那条如果栗子给了答复（改或不改），以答复为准覆盖本行 |

## 六、实测数字（2026-09-17 · 本机 AMD 7500F／Windows／Python 3.11）

### 6.1 性能（**旧口径：全量 re-sync，修复前** · 同规模合成库：1154 记忆／2406 实体／6035 关系／497 事件）

| 指标 | p50 | p95 |
|---|---|---|
| 四通道检索 `search(limit=8)` | **37.7 ms** | 49.5 ms |
| 注入装配 `inject_finalize` | **98.9 ms** | 124.8 ms |
| 轮末固化 `consolidate`（含守卫） | 513.2 ms | 787.0 ms |
| 审计通道额外开销 | +41.8 ms/次 | +47.0 ms |
| 单条写入（含**全量**索引同步） | 748.6 ms | 924.7 ms（吞吐 1.32 条/秒） |

复跑：`python scripts/bench_scale.py --json <out.json>`（`--keep` 复用已建库）。
数字随机器与嵌入档变化；本表是**默认词法级嵌入档 + 离线档**的口径。

> **口径边界（七轮 T7 钉住）**：本表最后一行是**修复前**的全量 re-sync 口径。六轮 T6 已改为
> **增量索引同步**，同规模下单条写入 p50 **42.0 ms**（见 `CHANGELOG.md` [0.3.0] 与 README 性能表）。
> 引用"写入多快"一律用增量口径；本表只作**修复前后对照**，不得当现行数字外发。

### 6.2 公开基准（离线口径，非官方分）

**嵌入档是最大的一个变量**（同一份数据、同一套代码、同一批题，只换
`HIPPOCAMPUS_EMBEDDING_MODEL`）：

| 基准 | 嵌入档 | 题量 | 证据进上下文 | 答在文内 | 上下文 tokens | 延迟 p50 |
|---|---|---|---|---|---|---|
| LongMemEval-oracle（抽 200） | 默认 `builtin-hash`（词法） | 200 | **98.0%** | 40.0% | 864 | 78.1 ms |
| LongMemEval-oracle（抽 200） | **神经 ONNX** `bge-small-en-v1.5` | 200 | **100.0%** | 42.5% | 1057 | 74.8 ms |
| LoCoMo-10（全量） | 默认 `builtin-hash`（词法） | 1986 | **36.7%** | 14.7% | 1611 | 75.5 ms |
| LoCoMo-10（全量） | **神经 ONNX `bge-small-en-v1.5`** | 1986 | **45.7%** | 17.4% | 1354 | 45.8 ms |
| LoCoMo-10（全量） | 神经 ONNX `bge-base-en-v1.5` | 1986 | 47.8%（批 A：09-17 首轮，k=8） | 18.0% | 1432 | 47.0 ms |

> 后两行差 2.1 pp ≈ 1.1 pp 的标准误的两倍，看着像"大有提升"；但换**同一脚本同参数**再测
> （`scripts/bench_ablation.py`，k=8／k=20 全量，下称**批 B**）两档是
> **小档 `bge-small-en-v1.5` 45.2／49.8** vs **大档 `bge-base-en-v1.5` 46.5／48.8**——
> 打平。所以推荐档仍是 `bge-small-en-v1.5`；`bge-base-en` 只作为"想试更大模型"的可选项。

选档对照（80 题抽样 A/B，`scripts/bench_ab.py`；**样本小、只看方向**）：

| 嵌入档 | 证据进上下文（80 题） | 说明 |
|---|---|---|
| `bge-small-en-v1.5`（384 维） | 33.8% | 全量 45.2～45.7% |
| `bge-base-en-v1.5`（768 维） | 41.3% | 全量 46.5%（k=8）——**样本上的 +7.5 pp 没有在全量上复现**（见下） |
| `gte-small`（mean 池化，384 维） | 27.5% | 更差 → 不推荐 |
| `bge-small-en` + 官方英文查询指令 | 32.5% | 无收益（+80 tokens）→ **不登记** |

> **别只看小样本**：80 题的 95% 置信区间约 ±8 pp，那一版 +7.5 pp 在全量 1986 题上只复现出
> **+1.3 pp（k=8）且 k=20 时反超 −1.0 pp**——两个模型在 LoCoMo 上**统计上打平**。
> 既然打平，默认推荐**小的那个**（`bge-small-en-v1.5`：384 维、126 MB、更快）。
> 这条教训比数字本身值钱：**换档要么全量跑，要么明确标注"样本级、方向性"**。

消融（LoCoMo 全量）：注入条数 8→20 几乎不动（默认档 36.7%→37.2%）；只进经历层掉到 27.7%
（说明瓶颈是召回质量、不是预算）。协议、数据集版本与哈希、复跑命令见 `docs/benchmark.md`。

## 七、明确不做（写清楚，避免误会）

- 多用户/多租户、云服务、账号体系：单机单用户是设计目标。
- 图形界面/浏览器插件：交互面是 CLI ＋ HTTP。
- 训练/微调模型：记忆层不训练模型。
- 记忆质量的普适承诺：效果随数据与模型变化，只报实测值并明示样本边界。
- **MCP 工具接入（已取消承诺，2026-09-17 定论）**：早期文案写过"含 MCP"，源码从未实现。
  已从简历与 README 的承诺中移除（简历 v6 无"含 MCP"；README 明说不承诺）。
  依据：Agent 形态当前是内置 5 工具；目标岗位 JD 中 MCP 命中 0 次（85 份统计）；
  接入 `langchain-mcp-adapters` 属投递后的扩展项，不再列为承诺。
