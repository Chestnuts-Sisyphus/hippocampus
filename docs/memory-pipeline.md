# 长期记忆流水线：算法细节（阈值 / 合并策略 / 版本链）

> **本文档由 2026-09-22 面试复盘轮新建**，用于把已有实现成文；**所有 file:line 均逐条核实过**（核实基准：本仓库工作树 `HEAD=80200df`，核实日期 2026-09-22）。
> 本文只描述**已经存在的实现**；凡未实现的设计一律显式标注 `[设计未实现]`，凡未取得直接证据的判断一律标注 `[假设]` / `[未能考证]`。
> 适用范围：`src/hippocampus/memory/` 下的长期记忆写入与读出流水线。不含评测（见 `docs/benchmark.md`）、不含代理协议（见 `docs/proxy.md`）。

---

## 〇、流水线总览

### 0.1 八段与两侧

| 段 | 名称 | 触发时机 | 主实现入口 |
|---|---|---|---|
| 1 | 抽取 | 一轮对话结束后（固化） | `memory/pipeline.py:52` → `memory/extract.py:373` |
| 2 | 消歧 | 同上（＋轮末守卫） | `memory/pipeline.py:54-67`、`memory/disambiguate.py:68` |
| 3 | 去重 | 同上 | `memory/dedup.py:90`、`:136` |
| 4 | 冲突确认 | 同上（＋离线规则探测） | `memory/conflict.py:171`、`memory/confirm.py:44` |
| 5 | 写入 | 同上 | `memory/database.py:550`、`:603`；写后收尾 `core/core.py:630` |
| 6 | 检索 | 下一轮对话开始时 | `memory/retrieval.py:1364` |
| 7 | 注入 | 同上 | `memory/memory_bridge.py:828`（稳定层 `:689`） |
| 8 | 修正/过期 | 人工入口＋定时/低频触发 | `core/core.py:1036`／`:1069`、`memory/lifecycle.py:70` |

**两侧的划分是本文档的组织约定**（不是仓库原文）：1–5 段是**写侧**，由 `MemoryCore.consolidate` 串起来；6–7 段是**读侧**，由 `MemoryCore.inject_finalize` 串起来；第 8 段是**运维侧**，两个入口各自独立。

仓库自己的固化口径是**五段**，见 `core/core.py:838`（`consolidate` docstring：「一轮对话结束后的固化（抽取 → 消歧 → 去重 → 冲突 → 挂起确认）」）与 `memory/pipeline.py:6`（模块 docstring：「流程：LLM 提取实体+记忆 → 消歧第一层（按名匹配已有实体）→ 写入 SQLite → 按实体查回」）。

### 0.2 两条轨（写侧的分流，影响后面所有判据）

| 轨 | 函数 | 处理类型 | 是否建确认块 |
|---|---|---|---|
| 确认轨 `fire_track_a` | `memory/memory_bridge.py:1065` | `preference`／`fact` | 是（冲突时挂起人工裁决） |
| 非确认轨 `after_response` | `memory/memory_bridge.py:1014` | `status`／`resource` | 否（自动入库） |
| 观察轨（不是"双轨"之一） | `memory/memory_bridge.py:1144`（`extract_response`） | 模型输出里提到的 resource/status | 否，且 `shadow=1` **永不注入** |

命名口径出处：`docs/security.md:99-102`（明写「"双轨"专指确认轨（`fire_track_a`）／非确认轨（`after_response`）；模型侧一律叫**观察轨**（`shadow=1`），两者不是一个维度」）。
调用点：`core/core.py:849-869`（确认轨与规则冲突）＋ `:870-881`（非确认轨与观察轨）。

### 0.3 一个贯穿性纪律：主路径零 LLM

`memory/retrieval.py:12` 明写：「**主路径零 LLM**：查询实体机械匹配（jieba + entities 表），检索全程无任何大模型调用」。写侧的机械通道：规则抽取 `extract.py:137`、规则冲突 `conflict.py:171`、完全去重 `dedup.py:90`、实体名匹配消歧 `pipeline.py:60`。**离线能力的边界表见 `docs/offline.md`。**

---

## 一、段 1｜抽取

### 1.1 入口与两条实现路径

- 主入口 `memory/extract.py:373-401`（`extract`）。返回 `{'entities': [...], 'memories': [...]}`（`:374`）。
- **LLM 路径** `extract.py:404-478`（`_extract_with_llm`），prompt 常量 `EXTRACT_SYSTEM` 起点 **`extract.py:213`**。
- **规则兜底路径** `extract.py:137-195`（`extract_by_rules`），模式表 `_RULE_PATTERNS` 起点 **`extract.py:91`**。
- **降级判据**：`extract.py:398-401` —— `try: return _extract_with_llm(...)` / `except LLMUnavailable: return extract_by_rules(...)`。设计理由写在 `:395-397`：**不做 `llm.available()` 预检查**，因为预检查会让注入进来的模型客户端（测试替身、用户自定义客户端）永远拿不到调用；降级挂在**真实失败**上，不挂在配置快照上。

### 1.2 抽取的内容判据（prompt 侧）

`extract.py:228-250` 的【记忆提取原则】整段，包含四件：

1. **判别四问**（`:230-234`）：preference＝是否约束"以后/每次"的行为方式；fact＝是否不依赖当前计划、不指挥行为的客观陈述；resource＝是否指向位置/文件/工具/人的去处；status＝是否描述"当前"状态、将来会变化。
2. **类型白名单＋禁类型**（`:235`）：只能是 preference／fact／resource／status 四种，**禁止输出 task/goal/plan/progress**。
3. **正反例**（`:236-241`）：每类给正例与"最像该类但实属别类"的反例。
4. **禁瞬时状态与情绪**（`:242-250`）：情绪宣泄不提取；版本号/HEAD/commit/哈希、运行号/任务号/构建号、完成态任务断言一律不提取。

### 1.3 抽取的机械兜底（代码侧，不依赖模型）

| 机制 | 位置 | 判据 |
|---|---|---|
| 句式规则抽取 | `extract.py:91-109`（模式表）＋`：137-195`（函数） | 只认显式句式（"我只…／我不…／我是…／我正在…"）。`extract.py:87-89` 明写取舍：「它只认显式句式——**宁可少抽，不许抽错**」 |
| 疑问句／祈使句剔除 | `extract.py:196`（`_is_question`）、`:206`（`_is_imperative`） | 疑问句与祈使句不作为记忆陈述 |
| 瞬时状态模式 | `extract.py:28-38`（`TRANSIENT_STATUS_RE`）＋ `:39-59`（`is_transient_status`） | 只匹配 `status` 类：HEAD／commit 哈希（7-40 位）／run·构建·任务号／完成态任务断言 |
| 情绪模式 | `extract.py:60-68`（`is_emotional_status`） | 纯主观情绪发泄 |
| 类型白名单过滤 | `extract.py:74`（`VALID_MEMORY_TYPES`）＋ `:77-81`（`filter_valid_memories`） | 剔除 type 不在四类内的记忆（防类型泄漏／乱造类型） |

**这些机械拦截的调用点**（prompt 是第一道，这里是兜底）：`pipeline.py:112-120`（瞬时状态 `:114`、情绪 `:118`）。

### 1.4 长文本与分块

`extract.py:376-379`：文本 >2500 字符时自动分块提取后合并——entities 按 name 去重（同名保留第一个）、memories 全量合并、后续块 user 提示附「已知实体名」辅助跨块命名一致。分块函数 `extract.py:289`（`_split_chunks`）／`:321`（`_halve`）；阈值常量 `_CHUNK_THRESHOLD = 2500` 定义在 `extract.py:265`，比较处 `extract.py:406`。

### 1.5 抽取的诚实边界

- `[已证明]` 规则档从**自由文本**里抓不了隐含表达、跨句归纳（`docs/offline.md:39`）；`docs/offline.md:10` 注明句式规则抽取是**本项目新增**的离线通道。
- `[已证明]` 全库有 PII/密钥前置检测：`extract.py:386-392`（命中外层 PII 则**不调 LLM** 直接空返回）；`pipeline.py:38-52`（`secmod.precheck`）。五组规则见 `memory/security.py:7-15`，硬拦截组常量 `security.py:39`。

---

## 二、段 2｜消歧

### 2.1 第一层：名字精确匹配（写侧，机械）

`pipeline.py:54-67`：对抽取出的每个实体名调 `db.find_entity_by_name`（`database.py:703`），命中则复用已有实体 id，否则 `db.add_entity` 新建（`database.py:525`）。**这一层零 LLM。**

### 2.2 第二层：两两语义聚类合并（LLM，可缺省）

- 函数 `memory/disambiguate.py:68`（`run_disambiguation`），判据 prompt `disambiguate.py:17-22`（`DISAMBIG_SYSTEM`：不同语言/不同叫法同一事物、缩写/全称 → 同一；只是语义相关 → 不同）。
- 单对判定 `disambiguate.py:25-34`（`check_same`），合并动作 `disambiguate.py:36-67`（`merge_entities`）：别名并入主实体、关系转移、被合并实体标 `status='merged'`（该取值见 `database.py:36` 的列注释）。
- **触发点**：轮末守卫 `core/core.py:889-921`（`_run_turn_guards`），其中 `:918` 调 `disambiguate.run_disambiguation(session.conn, max_pairs=5)`。守卫的纪律：全部软失败，坏掉只记 stderr、**绝不阻断本轮固化结果**（`core.py:896`）。

### 2.3 第三层：检索侧实体解析（只读）

`memory/retrieval.py:987-995`（`resolve_entities`）：实体名 → 规范实体 ID 的精确匹配；**匹配不到不新建**（检索侧不写库，`:994` 注释）。查询侧实体抽取在 `retrieval.py:949`（`extract_query_entities`，机械匹配，零 LLM）。

### 2.4 消歧的诚实边界

- `[已证明]` **离线档消歧是软失败跳过的**：`docs/offline.md:29` 明写理由「消歧要判语义等价，规则判不准会造成错误合并；离线档宁可不错」。
- `[已证明]` 会话实体兜底（纯指代）单独成一条检索机制，`retrieval.py:998-1010`（`session_entity_set`），带开关 `session_entity_fallback`（`retrieval.py:133`、`:1478-1500`）。

---

## 三、段 3｜去重（含合并策略）

### 3.1 两级去重

| 级 | 函数 | 判据 |
|---|---|---|
| ① 完全重复 | `dedup.py:90-116`（`find_exact_duplicate`） | `normalize_content` 规范化后**完全相等**。规范化＝NFKC（全角转半角）＋剔标点 ＋ 压缩空白 ＋ 去首尾空白（`dedup.py:43-49`）；标点集合 `dedup.py:32` |
| ② 语义重复 | `dedup.py:136-179`（`find_semantic_duplicate`） | embedding 余弦相似度 ≥ 阈值。取近邻 `_SEMANTIC_TOP_N = 5`（`dedup.py:29`），先算 `q_emb`（`:154`）再走 `rt.semantic_search`（`:155`） |

**两级共同的范围约束**（关键设计）：
- 只与 `status='active' AND shadow=0` 的正式记忆比较（`dedup.py:104`、`:110`、`:167`、`:173`）——观察轨不参与去重基准，避免观察期噪声压制正式记忆（`dedup.py:16-18` 的 docstring）。
- `exclude_session_id`：**同会话已有记忆不参与基准**（`dedup.py:95-96`、`:147`；SQL 条件 `COALESCE(e.session_id,'') != ?`）。这是 tracka 验收语义：同会话跨轮重复允许再入库。调用点 `core/core.py:486`。

### 3.2 语义去重的阈值（按嵌入档标定）

阈值不在 `dedup.py` 写死。取法是三级回落，实现在 `dedup.py:119-133`（`semantic_dup_threshold`）：

1. 读活跃参数快照 `param_snapshots.params` 里的 `semantic_dup_threshold`（`dedup.py:126-130`），合法条件是 `0 < v <= 1`；
2. 读不到 → 回落模块常量 `SEMANTIC_DUP_THRESHOLD = 0.85`（`dedup.py:26`）。

该值的档位来源：`memory/calibration.py:23-80` 的 `TIER_PARAMS` 表（四档各自一格），写入动作在 `calibration.apply_tier_params`（`calibration.py:174-214`）。**为什么中文档要取 0.85 而不是英文常用的 0.7**：`dedup.py:10-14` 与 `:25` 记录的理由是中文 MiniLM embedding 虚高（「你好」sim 0.7578 误报，发现 22/29 同根）。

### 3.3 语义命中后的合并策略：三态机械分类

函数 `dedup.py:52-87`（`classify_dup_action`）。**纯机械，零 LLM 零 embedding**（`:53`），任何异常返回 `"drop"`（`:64`、`:86-87`）——即回落现状行为，绝不阻断入库主流程。

| 判定序 | 判据 | 返回 | 语义 |
|---|---|---|---|
| ① | 数值集合不同：`_NUM_RE`（`dedup.py:38`）抓出的集合不相等且两边都非空（`:73-75`） | `supersede` | 值变更（42 码 → 40 码） |
| ② | 去否定字后规范化相等：`_NEG_RE`（`dedup.py:36`）剔除后相等（`:77-78`） | `supersede` | 极性翻转（喜欢深色 ↔ 不喜欢深色） |
| ③ | 删连续拉丁/数字段后相等：`_LATIN_NUM_RE`（`dedup.py:40`）剔除并重压缩空白后相等（`:81-84`） | `admit` | 同构换值（VS Code ↔ Vim），**放行入库** |
| 兜底 | 以上都不满足 | `drop` | 保守拦（改写型重复），调用方打观察日志 |

**三态的落地处置**：
- `supersede` → 入库后调 `db.supersede_memory`（`pipeline.py:138-140`、`:174-176`；`core/core.py:500-501`）
- `admit` → 放行（`pipeline.py:141-142`；`core/core.py:502-503`）
- `drop`／判据不确定 → `pipeline.py:143-148` 保守拦并打日志；**对话路径更保守**：`core/core.py:504-510` 把新条以 `candidate` 入库并**挂起确认**，旧值在裁决前保持生效

**为什么宁冗余不丢**（讲这条时的背景）：`dedup.py:55-57` 记录定标实测——「MiniLM 现网 **8/14 变更句被吞**」，即"同构变更句 embedding 高分被去重命中后若一律丢弃 = 用户改主意永远不生效"。

### 3.4 去重的诚实边界

- `[已证明]` 语义去重**只对 `preference` 开**（`core/core.py:573-574`：`if kind != "preference": return None`；`pipeline.py:131` 同款条件）——这是 6c 收口时的边界，未扩到其他类型。
- `[已证明]` 语义级任何异常（chroma/embedding 挂了）→ 返回 `None`（`dedup.py:156-157`），**退化成只做完全去重**。
- `[已证明]` `dedup.py` 有第二处同名入口 `find_duplicate`（`dedup.py:182`）作为兼容包装。

---

## 四、段 4｜冲突确认

### 4.1 三条判据

| 判据 | 位置 | 依赖 | 何时用 |
|---|---|---|---|
| **规则判据（主力，离线可用）** | `conflict.py:171-206`（`detect_rule_conflicts`）；核心判定 `conflict.py:150-168`（`is_same_subject_value_change`） | 纯 SQL ＋ 机械串比对，**零模型** | 写入路径 `core.py:515-527`；对话路径 `core.py:936-941` |
| LLM 判据 | `conflict.py:29-47`（`detect_conflicts`），prompt `conflict.py:20-26` | 模型 | 批量化入口 `conflict.py:55-92`（`batch_detect_conflicts`） |
| 求证否证 | `memory/verification.py:218`（`verify`）；三态常量 `verification.py:40-42` | L1 存在性／L2 库内一致性（L3 默认关） | 写入前 `core.py:428-456` |

### 4.2 规则冲突的机械判据（`is_same_subject_value_change`，`conflict.py:150-168`）

三步，按顺序：

1. **共同前缀判据**（`:160-162`）：`prefix >= 0.5 * short` 且前缀之后两串不同 → **直接判 True**（"同前缀、差异在尾部：明确是同对象的取值变了"）。
2. **门**（`:163-164`）：`SequenceMatcher(None, norm_old, norm_new).ratio() < 0.6` → **直接判 False**（"主题都不像，后面的机械分流不适用"）。
3. **极性/同值段翻转**（`:165-168`）：委托 `dedup.classify_dup_action(old, new) == "supersede"`；异常 → False。

判据位置：`conflict.py:171-206`。函数签名含 `also_types` 与 `limit=20`（`:176-177`）。

### 4.3 规则冲突的比对面与上限

- **比对面**：`type IN (mtype, *also_types)` 且 `status='active'` 且 `COALESCE(shadow,0)=0`，按 `created_at DESC`，`LIMIT limit`（默认 20）。SQL 在 `conflict.py:194-199`。
- **为什么要能跨类型比**（`conflict.py:184-187` 原文理由）：同一件事在两条轨上的标注可能不同——「我的期望城市是北京」可能被抽成 preference，「我的期望城市是南京」被抽成 fact；只看同类型就会漏掉真正的改口。调用方通常传 `also_types=("preference", "fact")`（陈述类互相可比），`resource`／`status` 属操作性信息，不参与。
- **LLM 批量的候选上限**：`_BATCH_CANDIDATE_LIMIT = 30`（`conflict.py:52`），用法在 `conflict.py:79-80`（`ORDER BY created_at DESC LIMIT ?`）。理由（`conflict.py:65-67`）：候选集从「按类型分别限量」改为「**全类型统一限量**」——技术栈矛盾（fact「用 SQLite」 vs resource「用 PostgreSQL」）是**跨类型冲突**，原按类型分批永远不同批（SUPERSEDES 盲区）。

### 4.4 挂起与裁决的数据流

1. **降为候选**：`core/core.py:537`（`status = "candidate" if hold_old is not None else "active"`）；对话路径 `core.py:948-950`（`UPDATE memories SET status='candidate'`）。
2. **裁决前旧值继续生效**：`candidate` 的定义即"等裁决"——它不参与注入（`core/core.py:1335-1336` 的剔除理由：「待确认候选（尚未生效）」）、不参与去重基准（`database.py:568` 注释）。
3. **建块并落库**：`core/core.py:597-628`（`_queue_pending`：入队 `session.push_pending_block` ＋ `INSERT OR REPLACE INTO pending_blocks`）。建块本体 `confirm.py:44-65`（`build_confirm_block`：按 `created_at` 升序编号，`num` 从 1 起）。
4. **跨进程恢复**：`core/core.py:236-277`（`_restore_pending`：从 `pending_blocks` 表按 `resolved_at IS NULL AND created_at >= cutoff` 重建块；记忆内容按 id 现取，条目已不存在的跳过；恢复后队列保留最后 3 个块 `:275-277`）。
5. **裁决**：`core/core.py:957-994`（`confirm`）→ `confirm.py:68-83`（`parse_confirmation`：`确认N`／`否决`）→ `confirm.py:86-104`（`apply_confirmation`）。
   - `confirm`：败方全部 `supersede_memory(winner, loser)`（`confirm.py:92-93`）；**胜方若原是 candidate 必须转回 active**（`core.py:979-986`）。
   - `veto`：新记忆全部标 superseded、旧记忆保持 active（`confirm.py:97-104`）。
6. **TTL 过期**：`core/core.py:1090-1091`（`pending` 先调 `_expire_pending`）；`_expire_pending` 在 `core.py:1151`，语义是「超过 TTL 的块不恢复进队列（视为已退休）」＋「旧值保持生效」（`core.py:241-242`）。

### 4.5 求证否证的分流（写入前）

`core/core.py:428-456`：

| 情形 | 处置 | 位置 |
|---|---|---|
| `refuted` ＋ 来源 `model` | **丢弃留痕**（观察日志），不打扰用户 | `core.py:444-452` |
| `refuted` ＋ 来源 `user` | **不静默丢**——挂起一次询问 | `core.py:453-454`（置 `ver_ask_user`）→ `:531-535`（`hold_old=""` 表示"没有对立旧条，只是问一句"） |
| `verified` | 直存并留证据 | `core.py:548-554` |

`observe.jsonl` 落点：`core.py:581-595`（`_log_verification`）→ `memory/observe_log.py:71`（`log_verification`）。

---

## 五、段 5｜写入

### 5.1 落库动作

- 经历层：`pipeline.py:96-104`（`db.add_episode`，`database.py:603`）
- 经验层：`pipeline.py:165-173`（`db.add_memory`，`database.py:550`）
- 关系边：`database.py:637-650`（`add_relation`）
- 取代动作（写新＋标旧）：`pipeline.py:174-176` → `database.py:653-658`（`supersede_memory`）

### 5.2 显式写入通道（不经模型抽取）

`core/core.py:381-570`（`MemoryCore.write`），签名见 `docs/memory-core-v1.md:26-31`。要点：
- `source="model"` → `shadow=1`（`core.py:406`），**永不注入**。
- `explicit=True` → **跳过冲突挂起**，直接写入（`core.py:397-399` 的理由："用户的编辑本身就是决定"）。
- 安全守卫：PII（E 组）丢弃进 `skipped`；密钥（C 组）**标记入库**可审计（`core.py:415-426`）。

### 5.3 写后索引同步（含一条刻意的"不做"）

`core/core.py:630-652`（`_reindex`）：

- **增量同步**：`ids` 非空时只同步本次触碰的 id——新记忆 ＋ 新经历 ＋ 被取代的旧记忆（`core.py:564-569`）。BM25 仍按库全量重建（内存级，开销小，`core.py:635`）。
- **故意不在每次写后清 WAL**：`core.py:637-640` 记录实测——`purge_embeddings_wal` 会把 chroma `embeddings_queue` 里还没落进索引的行删掉，导致"刚写的记忆检索不到"（实测 demo 从 10/10 掉到 7/10）。WAL 排空留在**会话初始化**时做（那里面对的是别的进程写下的积压）。
- **同步失败不静默**：原因记在 `session._index_error`，后续注入会把它带进 `Injection.note`（`core.py:762-766`），`doctor` 的索引健康行也读它。

---

## 六、段 6｜检索

### 6.1 主入口与输出

`memory/retrieval.py:1364`（`retrieve`），docstring `:1374-1384`。返回 `{'results': [...], 'channels': {...}}`（`:1383`）。
调用点：`core/core.py:674-684`（`search`，`top_k=limit`）、`memory_bridge.py:883-893`（注入路径，`top_k=8`）、`core/core.py:901-910`（轮末守卫）。

### 6.2 四个通道

| 通道 | 池 | 实现 | 量纲 |
|---|---|---|---|
| 语义 | mem（经验层） | `retrieval.py:716`（`semantic_search`）；查询出口 `:383-389`（`_query_collection`，`query_embeddings` 在 `:387`） | 余弦相似度 |
| BM25 | mem（经验层，按 id 前缀过滤经历） | `retrieval.py:818-845`（`build_bm25` 自建倒排，postings `:835-840`）＋ `:848-884`（`bm25_score`）＋ `:899-911`（`bm25_search`）；参数 `BM25_K1 = 1.5`／`BM25_B = 0.75`（`retrieval.py:56-57`） | sigmoid |
| 图 | mem（走 relations 边表） | `retrieval.py:926-947`（`graph_search`），邻居展开 `:912-925`（`_neighbors`） | 离散 0.5／0.3／0.15 |
| 事件线索 | ep（经历层） | `retrieval.py:1156`（`episode_clue_search`） | 线索匹配（时间/会话/实体/主题） |

### 6.3 排序纪律：禁跨通道比分数

`retrieval.py:10` 与 `:14-15`（模块 docstring）：

> 「合并：区块顺序输出（语义 → BM25 → 图 → 事件层），**禁跨通道比分数**，token 预算裁剪」
> 「三通道量纲不可比（语义=余弦 / BM25=sigmoid / 图=离散 0.5-0.3-0.15），各通道只与自己的质量线比较。事件层按线索（结构化证据）检索，不与经验层互相否决。」

### 6.4 四条守门规则（阈值与默认值）

| 规则 | 默认值 | 常量位置 | 执行位置 |
|---|---|---|---|
| 各通道绝对底线 | 语义/BM25 `0.3`；图 `0.15` | `DEFAULT_ABSOLUTE_FLOOR = 0.3`（`retrieval.py:60`）、`DEFAULT_GRAPH_FLOOR = 0.15`（`:61`） | `retrieval.py:1453-1456`（`_channel_floor`，定义 `:1051-1053`） |
| 各通道断崖 | `gap_min = 0.1`、`ratio = 0.3` | `DEFAULT_CLIFF_GAP_MIN`（`:62`）、`DEFAULT_CLIFF_RATIO`（`:63`） | `retrieval.py:1458-1477`（`_cliff_cut`，定义 `:1056-1072`） |
| token 预算 | `1200` | `DEFAULT_INJECTION_TOKEN_BUDGET`（`:64`） | `_apply_budget`（`:1080-1096`）；估算式 `_est_tokens = len(content)//2 + 40`（`:1075-1077`） |
| 注入条数上限 | `8` | `injection_max_items`（`get_active_params` 默认值 `retrieval.py:141`） | `_apply_max_items`（`:1099-1106`） |

**断崖的确切判据**（`_cliff_cut`，`retrieval.py:1056-1072`）：取相邻最大分差 `best_gap`，当且仅当 `best_gap > gap_min` **且**（`span <= 0` 或 `best_gap / span > ratio`）才切，切在最大 gap 处、保留 gap 以上全部（`:1070-1071`）；否则**不切**（底线以上全放行，交给预算兜底，`:1072`）。`span = 最高分 - 最低分`（`:1064`）。
**守门员／加速器的分工**（`retrieval.py:11`）：守门员 ＝ 绝对底线 ＋ token 预算；断崖 ＝ 加速器，"明显间隙才切，无间隙不硬切"。

### 6.5 阈值如何按嵌入档取用

`retrieve` 读活跃参数快照的三行：`retrieval.py:1416-1419`（`absolute_floor`／`cliff_gap_min`／`cliff_ratio`）。事件层线索检索另有一处同款读取：`retrieval.py:1175-1177`。
`get_active_params` 的定义与默认值兜底：`retrieval.py:117-142`。

### 6.6 一个被实测暴露并已处置的排序缺陷（图通道同分）

`retrieval.py:1511-1516` 的注释记录了完整因果：图通道给离散分且**同分候选之间没有任何相关性排序**——问 "When did Caroline go to the LGBTQ support group?" 时，"Caroline" 名下有 30 条记忆同为 0.5 分，而注入只有 8 个位子，导致真正回答该问题的那条排到随机的第 7/8 位甚至被截掉。**处置方式**：只改**同分内部**的次序，用 BM25 原始分（词面相关度）作 tie-break；**分数本身仍是通道分（跨通道仍禁比分数）**。

### 6.7 检索的诚实边界

- `[已证明]` **`fuse()` 是废弃路径**：`retrieval.py:1016-1045`，`:1017` 自称 "⚠️ DEPRECATED：仅供 console debugRetrieve 展示（console 不可改）。主流程排序已由 retrieve 内区块管线取代（各通道独立底线→断崖→预算，禁跨通道比分数）。" **不要把它当"融合排序实现"引用。**
- `[已证明]` 无 chromadb 时语义通道关闭，BM25／图／事件线索照常（`memory_bridge.py:125-147`）。
- `[已证明]` 读向量索引失败有三级处置与重建回调（`retrieval.py:442` `_semantic_with_repair`；`retrieval.py:370-380` 的错误记录与清理；`repair` 参数见 `retrieve` 签名 `:1372`）。

---

## 七、段 7｜注入

### 7.1 两层结构

| 层 | 函数 | 内容 | 位置 |
|---|---|---|---|
| **稳定层**（stable） | `memory_bridge.py:689-...`（`stable_layer`） | 身份声明 ＋ 主题层。主题层＝机械实体匹配 → `entities` 表 → `memories.entity_ids LIKE '%"id"%'` → `status='active' AND type IN ('preference','fact')` → `updated_at` 降序 → 前 `theme_layer_max` 条 | 定义 `:689`，docstring `:690-699`；调用 `:876-880` |
| **流动层**（fluid） | 检索结果拼装 | `"[海马体记忆]\n" + rt.format_injection(...)`（`format_injection` 定义在 `retrieval.py:1648`） | `memory_bridge.py:977-985` |

装配总入口：`memory_bridge.py:828`（`prepare_injection`）；核心区间 **`:875-1000`**；对外视图 `core/core.py:708-771`（`inject_finalize`）。

### 7.2 流水线内部顺序（`memory_bridge.py:875-1000`）

1. 稳定层取文本与 ids（`:876-880`）
2. 流动层检索 `top_k=8`（`:882-893`）
3. **安全与状态过滤**（`:894-926`）：`security_flag > 0` 剔除、`status != "active"` 剔除、`shadow=1` 剔除；查询异常 → 该条放行并记 stderr（软失败）
4. 去重（`:928-930`，`dedup_enabled` 开关；`_dedup_results`，定义 `memory_bridge.py:756`）
5. 自主流独立门槛（`:932-938`，`flow == "auto"` 时：只留 `channel == "semantic"` 且 `score >= 0.75`，最多 3 条）
6. 命中回写 `last_hit_at`（`:940-948`，稳定层与流动层都写）
7. 生命周期低频扫描（`:950-959`，每天最多一次）
8. 巩固/图式化维护低频触发（`:961-967`，每天最多一次）
9. 注入排序（`:969-970`，`_sort_injection_results`（定义 `memory_bridge.py:801`），memory 最新优先）
10. 空注入零影响（`:972-975`）
11. 组装两层文本（`:977-985`）
12. 注入观察落点（`:987-998`，`observe_log.log_injection`）

**注入侧剔除理由的集中版**：`core/core.py:1329-1343`（`_exclusion_reason`，六条：安全标记／superseded／candidate／其他 status／shadow=1／lifecycle dormant 或 archived）。

### 7.3 旁路审计（与注入链并行的一次 top-50 检索）

`core/core.py:773-830`（`_audit_retrieval`）：与正常注入链（top-k≈8）**分离**——正常链决定"注入了什么"，审计链记录"本来还有哪些候选、为什么没进"（`:776-779`）。落盘 `audit.jsonl`：`memory/audit.py:37-65`（`record_retrieval`），候选上限 `TOP_N = 50`（`audit.py:22`），超限只记 `capped` 计数（`audit.py:62`）。三份文件的位置与内容表见 `docs/security.md:79-81`。

### 7.4 注入的确定性约束

`core/core.py:743-749`：分数相同时**按 `doc_id` 定序**。理由（`core.py:743-745`）：前身只按"分数 + 新鲜度"排，同分时次序取决于向量库返回次序（跨进程/跨库副本不稳定），**复演会漂**。

---

## 八、段 8｜修正与过期

### 8.1 修正（人工入口）

| 动作 | 函数 | 语义与位置 |
|---|---|---|
| 修改 | `core/core.py:1036-1067`（`update_memory`） | supersede 语义（docstring `:1044`）：写新条 → `db.supersede_memory(新, 旧)`（`:1063`）。**注意 `explicit=True`**（`:1059`）：用户直接编辑＝已表达确定意图，不再挂起确认 |
| 确认/否决 | `core/core.py:957-994`（`confirm`） | 见 §4.4 |
| 删除 | `core/core.py:1069-1084`（`delete_memory`） | 软删：`SET status='archived', lifecycle='archived', change_context=?`（`:1077-1081`） |

### 8.2 过期（规则与阈值）

阈值常量 `memory/lifecycle.py:25-29`：

| 常量 | 值 | 含义 |
|---|---|---|
| `ACTIVE_MAX` | `5000` | active 记忆总量上限（估计值，可配置） |
| `DORMANT_AFTER_MS` | `6 * 30 * 24 * 3600 * 1000`（6 个月） | 未命中 → dormant |
| `ARCHIVE_AFTER_MS` | `2 * 365 * 24 * 3600 * 1000`（2 年） | dormant 持续未命中 → archived |
| `TRANSIENT_DEGRADE_AFTER_MS` | `7 * 24 * 3600 * 1000`（7 天） | 瞬时状态超龄降级 |

**主扫描 `lifecycle.py:70-124`（`scan_lifecycle`）的三条规则**（执行顺序有讲究）：
- 规则0（`:80-85`）：瞬时状态定时失效 `degrade_transient`（**先跑**，保证老库历史瞬时样本一次扫描即降级）
- 规则3（`:87-94`）：`dormant` 且 `status='active'` 且超 2 年 → `archived`（**先于规则1**，避免刚转 dormant 的记忆被立刻归档）
- 规则1（`:96-103`）：`lifecycle='active'` 且 `status='active'` 且超 6 个月 → `dormant`
- 规则2（`:105-121`）：`active` 总量超 5000 → 按"最后命中时间（0 则按 `created_at`）"升序取 `over` 条转 `dormant`
- 返回 `{"to_dormant", "to_archived", "total_active"}`（`:124`）

**瞬时状态降级 `lifecycle.py:37-67`（`degrade_transient`）**：
- **只处理 `type='status'`**（SQL `:54` `WHERE lifecycle='active' AND type='status'`）
- 匹配模式与 `extract.is_transient_status` **同源**（`:48` 从 `extract` 导入 `TRANSIENT_STATUS_RE`，避免双源漂移）
- 超龄判据 `now - created_at > max_age_ms`（`:61-62`）
- **打回补丁的实测依据**（`lifecycle.py:44-46`）：不限定类型时误伤过 fact——实测 fact「HTTP HEAD 方法不带 body」被一次扫描降级，所以补丁是"只处理 type=status"

**命中复活**：`database.py:661-670`（`touch_memory`）：更新 `last_hit_at`，且 `lifecycle='dormant'` 被命中即转回 `active`（"不删任何数据"）。
**在线触发点**：`memory_bridge.py:950-959`，低频（`_LIFECYCLE_SCAN_INTERVAL_MS = 24 * 3600 * 1000`，`memory_bridge.py:140`）。

### 8.3 三轴的最终语义（定性）

| 轴 | 取值 | 管什么 | 定义位置 |
|---|---|---|---|
| `status` | `active`／`superseded`／`candidate`／`archived` | **正确性** | 列定义 `database.py:45`；`candidate` 写入点 `core.py:537`／`:949`，`archived` 写入点 `core.py:1078` |
| `lifecycle` | `active`／`dormant`／`archived` | **活跃度** | `database.py:46`；规则 `lifecycle.py:5-18`、`:70-124` |
| `shadow` | `0`／`1` | **来源轨** | `database.py:57`；`1 = 观察期（不污染正式检索）` |
| `type`（不是轴，是分类） | `preference`／`fact`／`resource`／`status` | 内容类型 | `core/types.py:15` |

> ⚠️ **注释滞后**：`database.py:45` 的列注释只写了 `active | superseded`，但代码实际还会写 `candidate`（`core.py:537`、`:949`、`:983`）与 `archived`（`core.py:1078`）。**以代码为准，不是以列注释为准。**

---

## 九、版本链的数据结构（`SUPERSEDES` 边）

### 9.1 存储形式

版本链**没有独立的表，也没有版本号列**。它由两样东西构成：

1. **旧条的 `status`** 被置为 `superseded`（`database.py:656`）
2. **一条 `relations` 记录**：方向是「胜出方 SUPERSEDES 落败方」，`rel_type='SUPERSEDES'`

`relations` 表定义：`database.py:80-89`（字段 `from_type／from_id／to_type／to_id／rel_type／source_episode_id／extracted_at`；`rel_type` 的七个合法取值列在 `:86`：`ABOUT | MENTIONS | DERIVED_FROM | SUPERSEDES | COEXISTS_WITH | PART_OF | RELATED_TO`）。另有两条索引：`idx_relations_from`／`idx_relations_to`（`database.py:91-92`）。

`supersede_memory` 全实现（`database.py:653-658`）：

```python
def supersede_memory(conn, winner_id, loser_id, source_episode_id=""):
    """确认机制落库（B1 新增）：胜出记忆保持 active，落败记忆标 superseded，
    并建 SUPERSEDES 关系（胜出方 SUPERSEDES 落败方）。只更新状态，不删任何数据。"""
    UPDATE memories SET status='superseded' WHERE id=loser_id
    UPDATE memories SET updated_at=?           WHERE id=winner_id
    add_relation(conn, "memory", winner_id, "memory", loser_id, "SUPERSEDES", source_episode_id)
```

**建边的唯一出口**：`database.py:637-650`（`add_relation`）。

### 9.2 读出（对外视图）

`core/types.py:53`：`MemoryItem.supersedes: list[str]`——对外视图里带一个"本条的取代链"字段。`MemoryItem` 的其余相关字段：`status`（`:44`）、`lifecycle`（`:45`）、`shadow`（`:46`）、`created_at`（`:51`）、`last_hit_at`（`:52`）。

### 9.3 已知缺口（如实登记）

- **无独立版本号列**：`memories` 表没有 `version` 字段（全表定义见 `database.py:42-65`）。
- **无独立版本链历史表**：不存在"每次修改一行记录"的表；`param_versions`（`database.py:114-123`）是**参数**版本表，不是记忆版本表。
- **因此**："这是第几版"只能沿 `SUPERSEDES` 边反推（从一条记忆出发，用 `idx_relations_to`／`idx_relations_from` 两个索引递归查上下游）。
- `[未能考证]` 仓库内**没有**现成的"沿 SUPERSEDES 边回溯整条链"的辅助函数或 CLI 命令——`grep` 未定位到这类入口。（检索侧的 `graph_search`（`retrieval.py:926`）会走边表拿邻居，但它不是版本链回溯工具。）
- `[已证明]` 全仓**没有物理删除记忆**：`grep -rn "DELETE FROM" src/` 只命中 `embeddings_queue`（chroma 内部表，`retrieval.py:630`；且 `retrieval.py:575-580` 与 `memory_bridge.py:318` 都记录了"手删该表会抹掉未落段写入"的教训），**没有一句 `DELETE FROM memories`**。

---

## 十、阈值总表

### 10.1 分档阈值表 `TIER_PARAMS`（`memory/calibration.py:23-80`）

六个参数、四档（**注意：表起点是 `:23`，终点是 `:80`**；`DEFAULT_TIER = "builtin-hash"` 在 `:82`）：

| 档 | `absolute_floor` | `cliff_gap_min` | `cliff_ratio` | `restate_threshold` | `semantic_dup_threshold` | `answer_floor` |
|---|---|---|---|---|---|---|
| `builtin-hash`（默认） | 0.10 | 0.06 | 0.25 | 0.40 | 0.80 | 0.17 |
| `onnx:Xenova/bge-small-zh-v1.5` | 0.50 | 0.08 | 0.30 | 0.58 | 0.85 | 0.465 |
| `onnx_mini_lm_l6_v2`（回退档） | 0.30 | 0.10 | 0.30 | 0.80 | 0.85 | 0.35 |
| `onnx:Xenova/bge-small-en-v1.5` | 0.50 | 0.08 | 0.30 | 0.58 | 0.85 | 0.649 |

> 行号定位：`builtin-hash` `:25-37`；`bge-small-zh-v1.5` `:49-56`；`onnx_mini_lm_l6_v2` `:58-65`；`bge-small-en-v1.5` `:72-79`。

### 10.2 六个参数各自的消费点（**逐条核实**）

| 参数 | 消费点（读快照 → 用） |
|---|---|
| `absolute_floor` | `retrieval.py:1416`（retrieve）／`retrieval.py:1175`（事件层线索）；用在 `:1453-1456` |
| `cliff_gap_min` | `retrieval.py:1418`／`:1176`；用在 `:1458-1477` |
| `cliff_ratio` | `retrieval.py:1419`／`:1177`；用在 `:1458-1477` |
| `semantic_dup_threshold` | `dedup.py:126-130`（`semantic_dup_threshold()`）；用在 `dedup.py:150`、`:161` |
| `restate_threshold` | `memory_bridge.py:73-81`（`_restate_threshold()`）；用在 `memory_bridge.py:1483` |
| `answer_floor` | `agent/policy.py:74-86`（证据线：检索最高分低于它就不算"有依据"——宁说不知道，不硬答） |

### 10.3 阈值的写入与覆盖策略

`calibration.apply_tier_params`（`calibration.py:174-214`），三态覆盖（docstring `:176-182`）：

| 情形 | 策略 | 位置 |
|---|---|---|
| 首次应用（快照里还没有 `embedding_tier`） | **覆盖**：库里的通用默认值按档位重写 | `:196`（`prev_tier is None` → `overwrite=True`） |
| 同档重复进入 | **只补缺**：人工调过的键保留 | `:202`（`overwrite or key not in params`） |
| 换档（`embedding_tier` 与当前档不同） | **覆盖**：换嵌入档必须重新标定 | `:196`；理由 `:180-181`「否则拿旧量纲的阈值去比新量纲的相似度，检索会静默失真」 |

未标定档的处置：`UNCALIBRATED_TIERS`（`calibration.py:94`，**八轮 V6 起为空表**）→ 未知档回落内置档参数并**打 stderr 告警**（`_warn_tier_fallback` `:143-167`，机制保留不删）。`PARAMETER_TIER_MAP`（`:98-131`）给出"已标定档的完整参数对照基准"，用于告警时列出缺失键名。

### 10.4 标定方法与诚实边界

- 标定脚本：`scripts/calibrate.py`（调用方式见 `calibration.py:165` 的提示串、`docs/embedding.md:33`）。
- **数字的来源边界**（`calibration.py:11-12` 明写）：表里的数字来自**合成示例数据 + 固定查询集**的扫描结果，**只证"方法可复现"**；效果结论在私有库运行史上另行单列。
- `[已证明]` 有档的参数是**沿用**而非实测，表内已就地标注：zh 档 `cliff／restate／semantic_dup` 未量出（`calibration.py:47-48`）、en 档同（`:71`）。

---

## 十一、其余散落的阈值与常量（一次性登记）

| 项 | 值 | 位置 |
|---|---|---|
| 语义重复近邻条数 | `_SEMANTIC_TOP_N = 5` | `dedup.py:29` |
| 语义重复阈值（兜底常量） | `SEMANTIC_DUP_THRESHOLD = 0.85` | `dedup.py:26` |
| 规则冲突候选上限 | `limit=20` | `conflict.py:177` |
| LLM 批量冲突候选上限 | `_BATCH_CANDIDATE_LIMIT = 30` | `conflict.py:52` |
| 重述阈值（中文／非中文兜底） | `0.8` / `0.7` | `memory_bridge.py:61`、`:63` |
| 自主流门槛 | 语义通道 ＋ `score >= 0.75` ＋ 最多 3 条 | `memory_bridge.py:933-938` |
| 注入 token 预算 | `1200` | `retrieval.py:64` |
| 注入条数上限 | `8` | `retrieval.py:141` |
| 事件层线索粗筛 LIMIT | `200` | `retrieval.py:65` |
| 审计候选上限 | `TOP_N = 50` | `audit.py:22` |
| 挂起块队列上限 | 3 | `core/core.py:275-277` |
| BM25 参数 | `K1 = 1.5`、`B = 0.75` | `retrieval.py:56-57` |
| 生命周期扫描间隔 | 24 小时 | `memory_bridge.py:140` |
| 分块阈值 | `_CHUNK_THRESHOLD` | `extract.py:406`（比较处） |
| 会话锚点缓存上限 | `_MAX_ANCHORS = 20` | `pipeline.py:19` |
| 消歧每轮最多对数 | `max_pairs=5` | `core/core.py:918` |
| 守卫补实体最大经历数 | `max_episodes=20` | `core/core.py:917` |
| 索引缓存上限 | `_BM25_CACHE_MAX` | `retrieval.py:842-844`（淘汰处） |

---

## 十二、诚实边界与已知缺口（汇总）

1. `[已证明]` **默认嵌入是词法档，不是神经语义**——`builtin-hash`／384 维／字符 n-gram 带符号哈希（`builtin_embedding.py:25-26`、`docs/embedding.md:44-48`）。中文近义改写召回弱于 bge 档（`builtin_embedding.py:13-14`）。
2. `[已证明]` **离线档的能力边界是成文的**：规则抽取只认显式句式（`docs/offline.md:10`、`:39`）；消歧软失败跳过（`docs/offline.md:29`）。
3. `[已证明]` **版本链无独立版本号列**，只能靠 `SUPERSEDES` 边反推（§9.3）。
4. `[已证明]` **`retrieval.fuse()` 是废弃函数**（`retrieval.py:1017`），主流程不用（§6.7）。
5. `[已证明]` **`database.py:45` 的 `status` 列注释滞后于代码**（§8.3）。
6. `[已证明]` **四通道不是差分优势**：本仓消融成文数字为"注入条数 8→20 几乎不动（默认档 36.7%→37.2%）、只进经历层掉到 27.7%"（`docs/roadmap.md:124`）。
7. `[未能考证]` 上述消融数字的**上游产物文件未在本仓定位**（本文件引用的是 `docs/roadmap.md` 的成文数字，未复核其数据文件）。
8. `[未能考证]` §9.3 的"无版本链回溯辅助函数"来自 grep 未命中，**不排除存在但命名不含 `supersede` 关键字的入口**。
9. `[设计未实现]` 多 agent／多会话的逻辑维度（namespace）**未实现**，全仓 `grep -rn "namespace" src/ docs/ README.md` = 0 命中；产品定位为单机单用户（`docs/roadmap.md:129`、`docs/security.md:86`）。

---

## 十三、复跑验证

```bash
cd /d/AI/Hippocampus

# 阈值表与档位
grep -n "TIER_PARAMS\|^DEFAULT_TIER\|UNCALIBRATED_TIERS" src/hippocampus/memory/calibration.py

# 版本链：建边与状态更新
grep -n "def supersede_memory" -A 6 src/hippocampus/memory/database.py

# 无物理删除
grep -rn "DELETE FROM" src/

# 流水线相关行为回归（本轮实测：14 项全过 / 48 项全过，退出码均 0）
.venv/Scripts/python.exe -m pytest tests/test_a9_memory_crud.py tests/test_n48_calibration_guards.py -p no:cacheprovider --tb=no -q
.venv/Scripts/python.exe -m pytest tests/test_a29_p1_no_drop.py tests/test_n5_pending_ttl_review.py tests/test_n4_audit_sink.py tests/test_memory_core_v1.py tests/ported/test_lifecycle.py tests/ported/test_lifecycle_supersede_hc0901.py -p no:cacheprovider --tb=no -q
```

**测试与本文件章节的对应**：

| 测试文件 | 覆盖章节 |
|---|---|
| `tests/test_a9_memory_crud.py` | §5、§8.1 |
| `tests/test_n48_calibration_guards.py` | §10（阈值表与覆盖策略） |
| `tests/test_a29_p1_no_drop.py` | §3.3、§4.4（不静默丢、挂起） |
| `tests/test_n5_pending_ttl_review.py` | §4.4（TTL、旧值生效） |
| `tests/test_n4_audit_sink.py` | §7.3 |
| `tests/test_memory_core_v1.py` | §5.2（接口冻结） |
| `tests/ported/test_lifecycle.py`、`tests/ported/test_lifecycle_supersede_hc0901.py` | §8.2、§9 |
