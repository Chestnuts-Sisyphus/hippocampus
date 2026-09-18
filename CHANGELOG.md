# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)；
`MemoryCore` 接口自 v1 起**只允许追加字段**（见 `docs/memory-core-v1.md`）。

## [Unreleased] — 七轮（工程完善轮，2026-09-19 起）

> 本段随**下一个 tag** 发布；`v0.3.0`（→`5f154a5`）不移动。本轮**评测批次冻结**（零新花费）。

模型层（T1 换型 · 复测后关闭）：

- 按拍板执行换型前置流程：`scripts/calibrate.py` 重标定中文档 → 正样本 0.5255–0.6293／
  负样本 0.2568–0.4043（**两分布可分**，建议 `answer_floor` 0.465，旧表值 0.55 系前身报告、
  高于实测正样本最小值已替换）；
- 真机回归（CI 同口径阈值断言）：中文档 demo 10 题＝记忆开 **8/10**（内置档 10/10），
  **未过 ≥9/10 闸 → 按护栏不换型**，负面实测如实入 `docs/embedding.md` §七轮换型实测；
- **修掉一处会让"换型"整体失效的漂移**：文档与档位表都写裸名 `onnx:bge-small-zh-v1.5`
  （合法仓库名带 `Xenova/` 前缀）→ 下载 401 **静默回退 MiniLM**，且 `apply_tier_params()`
  查不到表键 → **整档参数不生效**。表键改合法仓库名、文档改口径、未标定档显式登记
  `calibration.UNCALIBRATED_TIERS` 并加 stderr 告警，配守护测试
  `tests/test_n29_r7_model_tier.py`。

记忆层（T2 可求证机制，正本 §三-4 施工）：

- 新模块 `memory/verification.py`：L1 存在性·自洽（路径／URL／日历日／百分数）＋
  L2 库内一致性 ＋ L3 外站探测（**默认关**，开了才出站）；三态处置——`verified` 直存留证据、
  `refuted` 用户来源挂起询问不静默丢／模型来源丢弃＋观察日志留痕、`unverifiable` 直存不打可疑；
- `memories` 追加四列（全带默认值，`check_interface.py` 的 v1 只追加契约成立）；
  开关 `verification_enabled`（默认开）／`verification_external`（默认关）；
- 观察轨那条前身幻觉判据收敛到同一入口；
- 附带修掉一处建库漂移：账户库的迁移序列在 `memory_bridge` 里另抄了一份，新增列/参数
  只在一处生效 → 统一为 `database.apply_migrations()` 单一入口。
- 验收锚点 A42–A45：`tests/test_n30_r7_verification.py`。

形态层（T3 服务化 E1）：

- `hippocampus serve` 管理口三端点：`POST /run`（跑一轮注入＋固化，回 `run_id`）／
  `GET /trace?run_id=`（取该次注入的全链路审计＋观察事件）／`GET /health`（补索引健康）；
- 默认只绑 127.0.0.1＋实例令牌，非环回必须 `--allow-remote` 且强制有令牌；零出站。
  验收：`tests/test_n31_r7_serve.py`（真 uvicorn）。

治理（T5 真机验收）：

- 新增 `scripts/live_supersede_probe.py`：真机变更句实测（不钉桩、按对隔离账户、
  覆盖 supersede／admit／挂起确认三条去路，并断言"改口后注入取到新值"）；
  三档实测对照记入 `docs/embedding.md`。

文档一致性与仓库卫生（T7／T8／T11）：

- README 双语测试数按实测改 **476 → 511**（本轮新增 35 条用例），并加**守护测试**：
  `tests/test_n32_r7_docs_drift.py` 钉住"README 声明数 == `pytest --collect-only` 真值"
  （**双语两处都查**）、双语头牌数字同源、roadmap 旧口径表必须带标注；
- `docs/roadmap.md` §6.1 性能表加**「旧口径：全量 re-sync，修复前」**题注并指向现行
  增量口径（42.0 ms）；§五 那行"增量 upsert 本轮未做"订正为已解决（与 [0.3.0] 一致）；
- **中文 README 三处过期口径订正**（v0.3.0 只改了英文版）：性能数字改成发布口径
  （检索 48 ms／注入 109 ms／写入 42 ms／峰值 ~1.1 GB）、基准表补 `--neighbors` 的
  38.68% 行、"已知限制"里"无管理口／缓存无上界／写入全量同步"三条改为与实现一致；
- 去掉文档里会随提交漂移的硬编码扫描文件数（README／`docs/security.md` 改记"tracked 树零命中"）；
- **cat3 低分归因（§二·八，零新花费）**：只读既有官方判分 JSON 配对分析——
  基线臂 13.91%／邻居臂 12.25%，两臂都不过 83/96，**空作答 0 例**；
  机理＝开放域推断题的短判定词金标 × 词面 token-F1 判据的口径错配，不是记忆层召回失效；
  顺带订正 [0.3.0] 段"cat3 12.25%"未标臂的引用；
- `uv.lock` 入库（可复现安装；实测无个人绝对路径／无凭据字面量，CI 仍走 `pip install -e`，不受影响）。

安全面（T6 六轮 D5 欠账）：

- **Mimosa 正式 seal 本机仍不可跑**（命令面无该工具，它是 ZCode 侧 PreToolUse 钩子），
  如实维持**替代 seal**并重新量一遍：`pip-audit` 联网 OSV 复扫 132 包（命中仍是 `chromadb` 5 条
  ＋ `nltk` 1 条，与 09-18 同批、使用面不可达）＋ tracked 树凭据零命中 ＋ CI 全绿，
  记入 `docs/dependency-audit.md` §六（含"Mimosa 可用时必须复扫替换本节"的待办口径）。

凭据卫生（T9）：

- `.qoder/handoff/PROMPTS.md:925` 经复核是**扫描器误报**（命中的是普通词 `task-overlay` 里的
  子串），没有真凭据可打码 → 修的是判据：`scan_credentials.py` 短密钥形状规则补词首边界 `\b`，
  并补两侧对照测试 `tests/test_n33_r7_scan_credentials.py`（真 key 形态必命中／普通词与占位行不误报／
  整仓 tracked 零命中）；
- KEY 母库 `D:/AI/KEY/GITHUB-TOKENS.txt` 以**文末登记表**方式逐把补注 scope 能力面
  （既有行一字未改，`git-askpass` 垫片取第 2 行长度复核仍是 93 字符，不影响任何按行号取值的读取方）；
- 顺带测得一条硬边界：**在册凭据没有一把带 `delete_repo`**，删仓需 owner 网页端操作
  （T4 安全闸已通过但执行被权限挡住，见沉淀文档证据表）。



## [0.3.0] — 2026-09-19

第五与第六轮（T2–T14 / G4–G10）的集中发版，共 18 个提交：性能、评测可信度、
工程韧性与发布治理一次收口。**双形态（代理 + Agent）共用一份记忆核心的定位不变，
`MemoryCore` 接口仍为 v1（只追加，本轮无契约变更）**。

性能（本机实测，fresh 库首跑）：

- 单条写入 p50 **632.8 ms → 42.0 ms**（增量索引同步，T6）；
- 会话缓存 LRU 上限可配（T5）：LongMemEval 神经档 200 账户峰值 **~4 GB → ~1.1 GB（−72%）**，
  证据命中仍 100%；
- 四通道检索 p50 48 ms、注入组装 p50 109 ms（README 性能表同源）。

评测（公开基准 + 官方判分口径）：

- LoCoMo-10 全量 1986 官方 F1 **32.55%（CI 30.8–34.4）→ ±1 轮邻居扩展后 38.68%
  （CI 36.8–40.5，+6.13pp，`--neighbors` 显式开）**；口径三件套：deepseek-chat 判分、
  temperature=0、输入＝记忆层 top-8 注入非全文；
- LongMemEval-oracle 判分准确率 **70.5%（Wilson 63.8–76.4，n=200 抽样）**维持既有口径；
- cat5 对抗邻居负效应（51.4→48.0）完成逐题归因：**错位锚定**（邻居轮的具体事实诱导
  对抗题放弃正确拒答），46 题 1→0 / 31 题 0→1，机理注记入 `docs/benchmark.md` §二·四；
- 英文实体抽取 A/B 否定结论（−0.45pp）、四通道消融单通道非瓶颈、多账户常态压测
  （200 账户 100% / 365.8 MB / 45 s）入档；候选集哨兵接入 CI（词法档阈值 25，红测已证）。

工程与韧性：

- `ingest_history` 精确去重（重复导入不再滚大库）、LRU 逐出连带清 `_locks`；
- `scripts/demo_flow.py` 跨会话三形态演示（4 断言）；judge 双判分歧率脚本 `bench_judge_cross.py`；
- CLI 帮助行漂移修复、psutil 进 dev extras＋缺装降级；
- 共享 chroma client 完成一页方案并以**否决策**收口（`docs/chroma-client-sharing.md`）；
- e5 前缀型模型登记为已知限制（`docs/embedding.md` E4/G9）。

文档与治理：

- README 双语测试数订正为 **476（3 xfailed）**；新增 `docs/benchmark.en.md`、
  `docs/deployment.md`、`docs/release-sync.md`（F1/F2 同步纪律）；
- 版本三方不一致（pyproject 0.2.1 / tag v0.2.2 / CHANGELOG）随本段归一为 **0.3.0**。

诚实限制：cat3 开放域（邻居臂 12.25%／基线臂 13.91%，口径见 `docs/benchmark.md` §二·八）与多跳仍是短板；官方分为抽样/单机口径，不作普适承诺；
`bge-small-zh` 中文端到端实测不优于默认档（未过 CI 开 ≥9/10 阈值）；评估数据"优化"暂停纪律继续有效。

## [0.2.2] — 2026-09-18

第四个版本：**官方判分臂**。README 转英文（GitHub 主页对齐 GitTok/Infinigrow 包装方式）；
公开基准的**官方口径**（模型作答 + LLM 判分）落地为显式开关 `bench --model-arm`，
两个基准的官方分已实测（LongMemEval-oracle 抽 200：判分准确率 **70.5%**，
CI 63.8–76.4%；LoCoMo-10 全量 1986：官方 F1 **32.55%**，CI 30.8–34.4%）。
**接口仍为 v1（只追加）**。

### 新增

- **官方判分臂**（`src/hippocampus/eval/model_arm.py`）：模型作答（deepseek-chat, temperature 0）
  基于记忆层注入上下文（top-8，非全文）＋ 官方判分——LongMemEval 官方 judge prompt
  （`xiaowu0162/LongMemEval @9e0b455`）、LoCoMo 官方判分算法（`snap-research/locomo @3eb6f2c`，
  与官方 evaluation.py 逐题对拍一致）；显式 `--model-arm` 才出站，离线默认零变化。
- **调用链护栏**：并发 16、失败重试 2、单调用超时 120 s、按 usage 估算花费累计 **¥30 预算
  硬停**、跑前/跑后各查一次余额（零成本）；每次调用计数 + 估算花费打印。
- **CLI 参数**：`bench --model-arm`、`--budget-yuan`、`--concurrency`、`--retries`、`--timeout-s`；
  修 `bench --online` 未把 `allow_online` 传给运行层的旧 bug。
- **测试**：模型臂 21 条用例（判分保真/护栏/预算硬停/失败如实上报/离线零变化），
  全量 421 passed。

### 资料

- 基准结果正本（数字 + CI + 花费 + 复跑命令）：`投递/Hippocampus-基准评测结果-20260917.md`（v2）；
  官方判分协议：`docs/benchmark.md` §二·三。

### 已知限制（新增部分，完整清单见 docs/roadmap.md）

- 官方分是**单模型单次测量**（deepseek-chat）；答辩引用必须带"top-8 注入、非全文"脚注。
- LoCoMo 官方 F1 32.55% 与全文基线（GPT-3.5-16k 37.8）同表比时必须带口径注记。

## [0.2.1] — 2026-09-17

第三个版本：**二轮缺口清单 N11–N20**。主题＝"承诺与实现对齐"——把正本新口径落到实现与文档，
把两条稳定性长尾（hnsw 索引读失败、A30-3 间歇失败）修到根上，并给评测补齐公开基准与压测。
**接口仍为 v1（只追加：`TurnResult.observed_ids`、`MemoryCore.ingest_history/active_params/rebuild_index`）。**

### 修复

- **向量索引间歇读失败（`Error creating hnsw segment reader: Nothing found on disk`）**：
  根因＝会话初始化时手工 `DELETE FROM chroma.embeddings_queue`（改内部表，会把"还没被
  compactor 落进段"的写入一起抹掉）＋ 段 reader 建不起来时只重试不修。现改为：只写配置
  （`automatically_purge`，回收交给 chroma）、打开会话**预热探测**、读失败→**从 memory.db
  重建向量池**（实测：重试与再 upsert 都无效，重建有效）、新增 `hippocampus index rebuild`。
  这同时消掉了 `test_a30_3`（约 1/6 概率）的间歇失败——它的失败原文就是语义通道读失败。
- **Agent 形态漏执行用户意图**：检索无命中时直接收口，`记住 X`／`列出偏好`／`写成文件`
  什么都不做还报 `completed`（假完成）；现检索后一律回 think 执行意图动作，检索类答复如实
  报命中条数（空命中按"没依据"走三出口）。
- **观察轨在生产路径上没有调用点**：`extract_response`（模型输出→`shadow=1`）只有随迁测试在调；
  现接进 `consolidate` 的 `assistant_text` 分支（两形态共用），`TurnResult.observed_ids` 留证。
- **神经嵌入档在"多账户"下把进程打爆**：`_resolve_embedding_function` 每次调用都新建一份
  ONNX `InferenceSession`（每份数十 MB 常驻），而 `MemorySession.__init__` 每账户调一次——
  LongMemEval 一题一账户（200 个）跑到中途被 onnxruntime 的 Rust 侧
  `memory allocation of 2097152 bytes failed` 直接杀进程（异常 catch 不到）。
  现按模型名进程级 memo（切换配置走 `invalidate_embedding_cache` 清空），200 个账户只加载一份模型。

### 新增

- **Agent 形态不阉割**（正本 §一）：agent 轮末走 `consolidate`（对话固化＋守卫＋确认块）、
  `think` 轮首消费 `confirm`（`确认 n`／`否决`）；覆盖表 `docs/forms-parity.md`。
- **公开基准评测**：LoCoMo-10 与 LongMemEval 适配层（`hippocampus bench …`），报
  "答案/证据是否进上下文＋tokens＋延迟"，**默认钉离线档**（不发任何出站请求），
  `--online` 才可走模型臂；协议与哈希见 `docs/benchmark.md`。
- **性能压测脚本** `scripts/bench_scale.py`：自建真实运行库同规模合成库
  （1154 记忆／2406 实体／6035 关系／497 事件）测检索/注入/固化/写入/审计与守卫开销。
- **审计/观察文件轮转**：`audit.jsonl`／`observe.jsonl` 到上限滚动（保留份数可配，
  默认 8 MiB×3 份），`explain` 可读滚动份。
- **代理线收口**：带 `tools` 的 chat／anthropic／responses 三向真服务端到端；
  `cache_control_passthrough` 从活跃参数生效；`/v1/models` 回配置的模型名；
  `proxy.session_bucketing`＝day／hour／none 可配。
- **离线档守卫边界**：补实体走**规则抽取**（不再空转），消歧仍软失败跳过；`docs/offline.md` 写死。
- **CI 加两条闸**：形态路径（真服务／流式／鉴权／tools）先断言 proxy extra 在再跑；
  demo 加**阈值断言**（记忆开 ≥9/10、记忆关 ≤6/10）。
- **选档与归因工具**：`scripts/bench_ab.py`（A/B 对照：同一批题多档并跑，每档独立数据根、
  可复用已有导入）、`scripts/bench_ablation.py --reuse-import`（条数消融省掉重复导入）、
  `scripts/bench_diag.py`（失败归因：证据没进上下文时，是"邻居也没进来"还是"整个会话都没进"）。
- **池化按模型官方配置**（`_pooling_for`）：bge 系＝CLS、gte 系＝mean。此前一律 CLS——
  拿 gte 去测会得到"越换越差"的**假结论**（向量打偏，不是模型差）。
- **公开基准实测（神经档补齐）**：LongMemEval-oracle 抽 200 题 **100.0%** 证据命中
  （默认档 98.0%）；LoCoMo-10 全量 **47.8%**（`bge-base-en-v1.5`）／45.7%（`bge-small-en-v1.5`）／
  36.7%（默认词法档）。选档 A/B 表与两条**否定结论**（官方英文查询指令无收益、gte-small 更差）见
  `docs/benchmark.md`。
- **文档**：`docs/benchmark.md`（评测协议）、`docs/forms-parity.md`（两形态覆盖表）、
  `docs/framework-ammo.md`（框架弹药卡）、`docs/verification-design.md`（可求证机制设计稿，待过目）。

### 安全（Mimosa 审计 9 项处置）

- SQL 标识符白名单：`PRAGMA/ALTER` 拼串前校验（表名／列名／DDL 片段）；
  BM25 建索引的表名走白名单（只允许 `memories`／`episodes`）。
- `embedding_models` 下载链：仓库名白名单（`org/name` 两段）＋ 出站 URL 校验
  （只 http/https、**主机必须等于配置端点**）＋ 可注入抓取器（`set_http_fetcher`）。
- `llm_proxy.call_upstream`（随迁备用路径）：出站前过 `validate_endpoint_url`。

## [0.2.0] — 2026-09-17

第二个版本：把"移植了但没接线"的守卫接进生产路径，补齐流式／鉴权／审计／评测四块，
并给仓库打上第一个版本标签。**接口仍为 v1（只追加：`Injection.note`）。**

### 新增

- **轮末与维护守卫接线**：`retrieval_guard`（语义命中但实体未命中 → 轮末补实体、
  只加不删）、`missed_extract`（孤立经历补实体，≤3 次，第 3 次仍空标
  `no_entity_confirmed`）接进 `MemoryCore.consolidate` 轮末；`disambiguate`
  （实体消歧第二层）同路径软失败降级；`hub_guard`（mega-hub 标记）接进维护扫描。
  三类新测试：补实体生效／孤立经历补实体／守卫失败不阻断。
- **索引健康可诊断**：`doctor` 增"索引健康"行（chroma 目录可写性＋集合条数 vs
  库内 active 条数＋索引积压队列＋上次同步错误）；索引同步/检索失败不再静默——
  `inject_finalize` 的 `note` 携带告警（v1 追加字段），chroma 不可写时明确报出。
- **AuditSink 旁路审计**（A18 完整形态）：每次注入旁路记录 **top-N=50 候选全集**
  （doc_id／通道／分数／已注入／被剔理由）到账户目录 `audit.jsonl`，超限只记计数；
  开关 `audit_enabled`（默认开）。`explain` 升级：能答"那条为什么没进"（含超出
  检索 top-k 的候选），并把 `observe.jsonl`（注入/确认事件）与轨迹合并成一份 run 视图。
- **pending TTL（A35）**：未决确认块默认 **7 天** TTL，超时标记"未决冲突"落库、
  旧值保持生效、新候选保持候选态；超时块不再回到 pending 队列。
- **`memory review`**：`--pending`（未决块＋TTL 剩余）／`--candidates`（可疑候选）／
  `--suspicious`（安全标记＋候选＋TTL 未决冲突）三个视图。
- **真·流式转发（A1）**：客户端 `stream:true` 时逐行转发上游 SSE（chat／anthropic／
  responses 三种上游格式各一条测试），每个文本增量一个 delta 事件；**流结束后仍完成
  固化**；确认块作为**末尾 delta** 追加。
- **代理鉴权（A2）**：实例令牌首次启动生成并落盘（`<数据根>/instance_token`），
  请求需带 `Authorization: Bearer <令牌>`，否则 401；`doctor` 只显示前 8 位。
- **上游错误透传（A3）**：上游非 2xx **原样透传状态码与错误体**（不再统一包成 502）。
- **评测升级（C1–C4）**：题集扩到 **20 题**（14 问 6 动作；含 real-jd 真实岗位 JD
  派生场景，**脱敏**）；**pass^k**（`--pass-k`，每题重复 k 次全过才算过）；
  **模型臂**（`--model`，有端点时同一套题走模型作答器）；**关键词基线对照**（`--baseline`，
  仅 BM25 直查库）；报告分列 synthetic／real-jd，边界声明含 k 与模型名。
- **一键演示脚本**：`scripts/demo.sh`＋`scripts/demo.ps1`（起代理 → 灌数据 →
  三格式请求 → 跑评测 → 出结果表；离线可跑、无弹窗）。

### 修复

- **`MemoryCore.close()` 泄漏索引句柄**：此前只关 SQLite 连接、不关向量库客户端，
  长进程（或一条测试套件）跑几百个实例后触发 `OSError: Too many open files`
  （实测拖垮真实服务器测试）。现在连向量库客户端一起关。
- **离线档 responses 入站回执丢正文**：曾只回 `{"model": ...}`；现在按响应适配器
  构造完整回执（含 `output[].content[].text`）。
- **请求体非法 JSON 回 500**：现在明确回 400（含编码问题提示）。

### 变更（口径）

- README 的"可插拔三点（含 MCP）"改为如实表述：**未做 MCP 等第三方工具接入，
  不承诺"含 MCP"**（MCP 接入列为明确不做，见 `docs/roadmap.md` §六）。
- `docs/roadmap.md` 同步本轮完成项与取消项。

### 已知限制

- MCP 工具接入：不做（已从承诺移除）。
- 记忆后端仍写死 SQLite＋Chroma（可替换后端列入后续版本）。
- 评测效果结论只对合成/脱敏样本成立，不作普适承诺。

## [0.1.0] — 2026-09-17

首个可运行版本（alpha）。记忆核心取自既有实现的抽取式移植（机制未简化），
本版本的工作集中在**接口化、去耦合与可验证**。

### 新增

- **`MemoryCore` v1 冻结接口**：`write` / `search` / `inject_finalize` / `confirm` /
  `consolidate`（scope 为第一参数；不含任何 HTTP 概念；只允许追加字段）。
  自动检查：`scripts/check_interface.py`。
- **代理形态：三种入站格式**（默认 `127.0.0.1:8765`）——OpenAI Chat Completions、
  OpenAI Responses、Anthropic Messages，按请求体形状判定，回复按客户端自己那套协议返回；
  上游格式由配置 `llm.api_mode` 决定（chat／anthropic／responses）。确认块由**记忆层生成、
  代理追加**（可关，`确认 n`／`否决` 在入口消费）。不支持的组合如实回 501。
  格式矩阵与已知边界见 `docs/proxy.md`。
- **Agent 形态**：LangGraph 三节点（think / act / answer）＋ LangChain 工具接入；
  三出口（完成／无法完成／需人工升级）；轨迹可复演（`replay` 两次指纹一致）。
- **命令行**：`doctor` / `seed` / `demo` / `proxy` / `chat` / `replay` / `explain` /
  `memory list|pending|candidates|update|delete|off|on|scopes` / `learning off|on`。
- **离线档**：无 key／无网可跑记忆纪律；新增**句式规则抽取**与**规则冲突检测**
  （原先两者都依赖模型，离线会断链）。
- **内置嵌入档 `builtin-hash`**（默认，零下载、确定性）＋ 推荐档 `onnx:bge-small-zh-v1.5`
  可选；新增按档标定表 `memory/calibration.py` 与标定脚本 `scripts/calibrate.py`。
- **单写者锁**（库目录级，含僵尸锁回收）与 `doctor --unlock`。
- **出站 URL 校验**（仅 http/https；拒环回／私有／保留／元数据地址，含 IPv6 与
  十进制／十六进制写法）。
- **评测最小版**：10 题（7 问 + 3 动作）＋ 记忆开/关对照 ＋ 标签双来源一致率 ＋ 边界声明。

### 修复

- **P1（数据丢失级）**：语义去重命中后，机械判据拿不准时会**静默丢弃**新句（用户改口不生效）。
  现在三条路——`supersede`（确认值变更）／`admit`（同构换值，两条都留）／
  **挂起确认**（判据不确定时新条以候选入库，旧值继续生效）——**任何一条都不丢数据**。
- **离线断链**：无模型时"改口 → 挂起确认"整条链曾不可用，现由规则冲突检测承接。
- **生命周期与固化**的"当前时间"参数化（演示可用压缩时间跑同一代码路径）。
- **Windows 控制台编码**：脚本与 CLI 在 stdout/stderr 上强制 UTF-8（`errors="replace"`）。
  此前在 Windows CI 上打印 `✓`／中文会抛 `UnicodeEncodeError`，让检查脚本整条失败。

- **显式写入不再挂起确认**：`write(..., explicit=True)`（用户直接编辑记忆、或显式"记住 X"）
  跳过冲突挂起——用户自己下的决定不需要再问一遍；冲突挂起留给"对话里冒出来的新说法"。
- **对话改口在离线档也能挂起**：`consolidate()` 补规则冲突检测（此前这条链是纯 LLM，无 key 就断）。

### 变更（相对移植来源）

- 去代理形态耦合：记忆语义入口收进 `MemoryCore`，HTTP 形状只留在代理层。
- 去 Windows 专有：删除 DPAPI 加密；凭据只从环境变量／密钥服务读取（**不落盘**）。
- 去个人数据：不再有账号体系；示例数据由 `hippocampus seed` 生成（合成、含已知真值）。
- 数据根与 scope 解析改为显式注入（前身为模块级全局单例，多 scope 并行会串库）。

### 已知限制

- 代理流的转发是**单 delta**（协议正确、无逐字效果）；上游逐行流式转发尚未实现。
- 代理**未做鉴权**（默认只监听 127.0.0.1）；实例令牌尚未移植。

- **并发写不保证**：同一记忆库目录同时只有一个写者（第二个排队或被告知）。
- 内置嵌入档是**词法级**表征，同义改写召回弱于神经档；边界写在 `docs/embedding.md`。
- 评测为最小版（10 题／合成数据），只证方法可复现，不构成效果结论。
- 尚未发布到 PyPI（安装以源码／GitHub 直装为准）。
