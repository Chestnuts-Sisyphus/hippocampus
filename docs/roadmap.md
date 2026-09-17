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

## 二、记忆层

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~三个守卫模块只有单测、未接入生产路径~~ | **已解决（0.2.0）**：`retrieval_guard`／`missed_extract` 接进 `MemoryCore.consolidate` 轮末，`disambiguate` 同路径（无端点时软失败跳过），`hub_guard` 接进维护扫描 | — |
| 语义通道的半成品状态 | 默认内置档 `builtin-hash` 是**词法级**表征；装 `[vector]` 并用 `onnx:bge-small-zh-v1.5` 才是神经语义检索 | 默认档的同义改写召回弱；`docs/embedding.md` 给了实测分布 |
| ~~磁盘/索引异常时的降级是**静默**的~~ | **已解决（0.2.0）**：`doctor` 有"索引健康"行（chroma 可写性＋集合条数 vs 库内 active 条数）；索引写失败/检索失败会记入会话并在注入结果 `note` 里明确告警；索引写入后校验收敛、查询失败自愈一次 | — |
| ~~记忆后端不可替换~~ | **已解决（0.2.0）**：抽出 `MemoryBackend` 协议（`core/backend.py`），SQLite＋Chroma 是默认实现；`hippocampus export/import` 提供目录包迁移（含 schema 版本） | — |
| 可求证机制（正本 §三-4 新增设计） | **设计稿已出、未实现**：`docs/verification-design.md`（可求证判定／三级判据 L1 存在性·L2 库内一致性·L3 外站探测／三态失败处理／与四类型交互＋A42–A45 锚点） | 交栗子过目 → 写回正本 → 施工 |
| ~~模型输出的观察轨没有生产调用点~~ | **已解决（0.2.1）**：`consolidate` 的 `assistant_text` 分支调 `extract_response`（模型输出→`shadow=1`，永不注入），两形态共用；受学习开关约束，离线档零调用 | — |
| ~~向量索引段偶发读不到（hnsw）~~ | **已解决（0.2.1）**：**根因**＝会话初始化时手工 `DELETE FROM chroma.embeddings_queue`（改内部表，会把还没落段的写入抹掉）＋ 段 reader 建不起来时只重试不修；现改为**只写配置**（`automatically_purge`，回收交给 chroma）＋ 打开会话时**预热探测** ＋ 读失败→**从 memory.db 重建向量池**（实测：重试/再 upsert 都无效，重建有效）＋ `hippocampus index rebuild` 运维入口。复现脚本口径见本文件 §七 | 残余风险：chroma 版本升级可能改变内部行为（我们不碰内部表了，只依赖公开 API） |

## 三、评测

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~题量与轮次~~ | **已解决（0.2.0）**：题集 20 题（14 问 6 动作）；**pass^k**（`--pass-k`，k≥3 时每题重复 k 次全过才算过） | — |
| 题集来源 | 20 题含 synthetic 与 **real-jd**（真实岗位 JD **脱敏**派生场景，不含私人 JD 原文） | real-jd 只保留招聘方普遍考察的能力要求 |
| 作答器 | 离线档是**规则作答器**（不是模型推理）；`--model` 可加模型臂（有端点时） | 离线结论测的是"记忆层有没有把依据给出来"，不是模型答得好不好 |
| 对照 | **关键词基线对照**已实现（`--baseline`，仅 BM25 直查库）；实测：记忆开 20/20、记忆关 6/20、关键词基线 18/20 | 合成样本上的对照，不作普适承诺 |
| ~~评测无 CI 回归阈值~~ | **已解决（0.2.1）**：CI 的 demo 步骤带**阈值断言**（记忆开 ≥9/10、记忆关 ≤6/10），分数回归会红 | — |
| **公开基准是"检索口径"不是官方分** | 已实现 LoCoMo／LongMemEval 适配层（`hippocampus bench …`）：报"答案/证据是否进上下文"＋tokens＋延迟；**官方口径（模型作答＋LLM 判分）在离线档不跑**，有端点时 `--online` 可加 | 绝对分不与"把整段对话喂给模型"的报分可比（口径不同，见 `docs/benchmark.md` §三） |
| LoCoMo 绝对分偏低 | 英文语料 × 中文标定分词/阈值 × 默认词法级嵌入档 × 离线引文作答器；实测：证据命中 36.7%（LongMemEval-oracle 98.0%） | 要提分得做英文适配或换神经嵌入档（未做，属可用性优化不是纪律问题） |

## 四、可观测与可追责

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~`explain` 候选集上限＝检索 top-k~~ | **已解决（0.2.0）**：旁路 `AuditSink` 记录 **top-N=50 候选全集**（`audit.jsonl`，超限只记计数）；`explain` 能答"那条为什么没进" | 审计默认开（`audit_enabled`），开启时每次注入多一次同量级检索 |
| ~~观测两处分裂~~ | **已解决（0.2.0）**：`explain`（不带 `--step`）把 `observe.jsonl`（注入/确认）与轨迹合并成一份 run 视图 | — |
| ~~审计/观察文件只增不轮转~~ | **已解决（0.2.1）**：`audit.jsonl`／`observe.jsonl` 到上限滚动（`.1`/`.2`…，保留份数可配 `observability.jsonl_max_bytes`／`jsonl_keep`，默认 8 MiB／3 份）；`explain` 可读滚动份 | 极老的日志会被丢弃（保留份数之外），上限可调 |
| 审计开销有实测 | `audit_enabled` 开/关实测：**+41.8 ms/次**（p50，1154 记忆规模库）；默认开 | 嫌慢可关（`audit_enabled=false`），代价是 `explain` 看不到超出 top-k 的候选 |
| ~~冲突确认无 TTL~~ | **已解决（0.2.0）**：pending 块默认 **7 天 TTL**，超时标记"未决冲突"、旧值保持生效；`memory review --pending/--candidates/--suspicious` 三个视图 | — |

## 五、工程与发布

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~无版本标签~~ | **已解决**：`v0.2.0` tag＋Release（见 GitHub Releases） | — |
| 未发布 PyPI | 安装走 GitHub 直装 / 源码；分发名 `hippocampus-agent` 已在 PyPI 占位可用 | `pip install` 装不到 |
| ~~无导入/导出命令~~ | **已解决（0.2.0）**：`hippocampus export <目录>` / `import <目录>`（目录包含 manifest 与 schema 版本；导入默认不覆盖，`--force` 时旧库留 `.bak` 副本） | — |
| 演示脚本 | `scripts/demo.sh`／`scripts/demo.ps1`（起代理→灌数据→三格式请求→评测→结果表）；bash 版已实测，**PowerShell 版未实测** | Windows 用户首次运行可能需微调 |
| **单条写入随库规模线性变慢** | 每次 `write` 触发**全量**索引同步：1154 记忆规模下 p50 **749 ms**／1.32 条/秒（`scripts/bench_scale.py` 实测） | 会话里每轮多几次写入即可感知；改进方向＝**增量 upsert**（只同步变化行），本轮未做（改动面大、需重新标定） |

## 六、实测数字（2026-09-17 · 本机 AMD 7500F／Windows／Python 3.11）

### 6.1 性能（同规模合成库：1154 记忆／2406 实体／6035 关系／497 事件）

| 指标 | p50 | p95 |
|---|---|---|
| 四通道检索 `search(limit=8)` | **37.7 ms** | 49.5 ms |
| 注入装配 `inject_finalize` | **98.9 ms** | 124.8 ms |
| 轮末固化 `consolidate`（含守卫） | 513.2 ms | 787.0 ms |
| 审计通道额外开销 | +41.8 ms/次 | +47.0 ms |
| 单条写入（含**全量**索引同步） | 748.6 ms | 924.7 ms（吞吐 1.32 条/秒） |

复跑：`python scripts/bench_scale.py --json <out.json>`（`--keep` 复用已建库）。
数字随机器与嵌入档变化；本表是**默认词法级嵌入档 + 离线档**的口径。

### 6.2 公开基准（离线口径，非官方分）

**嵌入档是最大的一个变量**（同一份数据、同一套代码、同一批题，只换
`HIPPOCAMPUS_EMBEDDING_MODEL`）：

| 基准 | 嵌入档 | 题量 | 证据进上下文 | 答在文内 | 上下文 tokens | 延迟 p50 |
|---|---|---|---|---|---|---|
| LongMemEval-oracle（抽 200） | 默认 `builtin-hash`（词法） | 200 | **98.0%** | 40.0% | 864 | 78.1 ms |
| LongMemEval-oracle（抽 200） | **神经 ONNX** `bge-small-en-v1.5` | 200 | **100.0%** | 42.5% | 1057 | 74.8 ms |
| LoCoMo-10（全量） | 默认 `builtin-hash`（词法） | 1986 | **36.7%** | 14.7% | 1611 | 75.5 ms |
| LoCoMo-10（全量） | 神经 ONNX `bge-small-en-v1.5` | 1986 | **45.7%** | 17.4% | 1354 | 45.8 ms |
| LoCoMo-10（全量） | **神经 ONNX `bge-base-en-v1.5`** | 1986 | **47.8%** | 18.0% | 1432 | 47.0 ms |

选档对照（LoCoMo 前 3 段对话抽 80 题的 A/B，同批同口径；`scripts/bench_ab.py`）：

| 嵌入档 | 证据进上下文 | 相对 bge-small |
|---|---|---|
| `bge-small-en-v1.5`（384 维） | 33.8% | — |
| **`bge-base-en-v1.5`（768 维）** | **41.3%** | **+7.5 pp** |
| `gte-small`（mean 池化，384 维） | 27.5% | −6.3 pp |
| `bge-small-en-v1.5` + 官方英文查询指令 | 32.5% | −1.3 pp（噪声级，且 +80 tokens）→ **不登记** |

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
