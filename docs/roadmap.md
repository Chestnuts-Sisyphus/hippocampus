# 已知限制与路线图

> 这份文件列**当前版本已知做得不够的地方**，避免用户按 README 的期待踩空。
> 与 `CHANGELOG.md` 的分工：CHANGELOG 记"改了什么"，本文件记"还差什么"。

## 一、形态与协议

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~代理的流式是"单 delta"~~ | **已解决（0.2.0）**：`stream:true` 时上游 SSE **逐行转发**（chat／anthropic／responses 三种上游格式），每个文本增量一个 delta 事件；流末仍固化、确认块作为末尾 delta 追加 | — |
| ~~代理无鉴权~~ | **已解决（0.2.0）**：实例令牌首次启动生成（`<数据根>/instance_token`），未带 `Authorization: Bearer <令牌>` 回 401；`doctor` 只显前 8 位 | 客户端需带上令牌（README 有示例） |
| chat → responses 组合 | 回 501（与移植来源同口径） | 需要该组合时请把上游 `api_mode` 改成 `responses` 或 `anthropic` |
| 工具调用（function calling） | 转换器齐备（chat／anthropic／responses 三向），**未做端到端联调** | 依赖工具调用的客户端请先自测 |
| 代理 session 默认按天分桶 | `X-Hippocampus-Session`／`X-Session-Id` 可显式指定；缺省按 `day-YYYYMMDD` 分桶，**不可配** | 跨天续接语义固定；需要别的分桶策略请显式传 session 头 |
| `/v1/models` 只回一个占位模型名 | 部分客户端会校验列表 | 用自己的模型名发请求即可；列表仅占位 |

## 二、记忆层

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~三个守卫模块只有单测、未接入生产路径~~ | **已解决（0.2.0）**：`retrieval_guard`／`missed_extract` 接进 `MemoryCore.consolidate` 轮末，`disambiguate` 同路径（无端点时软失败跳过），`hub_guard` 接进维护扫描 | — |
| 语义通道的半成品状态 | 默认内置档 `builtin-hash` 是**词法级**表征；装 `[vector]` 并用 `onnx:bge-small-zh-v1.5` 才是神经语义检索 | 默认档的同义改写召回弱；`docs/embedding.md` 给了实测分布 |
| ~~磁盘/索引异常时的降级是**静默**的~~ | **已解决（0.2.0）**：`doctor` 有"索引健康"行（chroma 可写性＋集合条数 vs 库内 active 条数）；索引写失败/检索失败会记入会话并在注入结果 `note` 里明确告警；索引写入后校验收敛、查询失败自愈一次 | — |
| ~~记忆后端不可替换~~ | **已解决（0.2.0）**：抽出 `MemoryBackend` 协议（`core/backend.py`），SQLite＋Chroma 是默认实现；`hippocampus export/import` 提供目录包迁移（含 schema 版本） | — |
| 可求证机制（正本 §三-4 新增设计） | **未实现**：事实分可求证/不可求证、可求证内容求证后存储（手段/判据待设计过目） | 见设计正本排期第 1 项 |

## 三、评测

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~题量与轮次~~ | **已解决（0.2.0）**：题集 20 题（14 问 6 动作）；**pass^k**（`--pass-k`，k≥3 时每题重复 k 次全过才算过） | — |
| 题集来源 | 20 题含 synthetic 与 **real-jd**（真实岗位 JD **脱敏**派生场景，不含私人 JD 原文） | real-jd 只保留招聘方普遍考察的能力要求 |
| 作答器 | 离线档是**规则作答器**（不是模型推理）；`--model` 可加模型臂（有端点时） | 离线结论测的是"记忆层有没有把依据给出来"，不是模型答得好不好 |
| 对照 | **关键词基线对照**已实现（`--baseline`，仅 BM25 直查库）；实测：记忆开 20/20、记忆关 6/20、关键词基线 18/20 | 合成样本上的对照，不作普适承诺 |
| 评测无 CI 回归阈值 | CI 只跑 demo 冒烟，不校验分数变化 | 分数回归需人工对比历史输出 |

## 四、可观测与可追责

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~`explain` 候选集上限＝检索 top-k~~ | **已解决（0.2.0）**：旁路 `AuditSink` 记录 **top-N=50 候选全集**（`audit.jsonl`，超限只记计数）；`explain` 能答"那条为什么没进" | 审计默认开（`audit_enabled`），开启时每次注入多一次同量级检索 |
| ~~观测两处分裂~~ | **已解决（0.2.0）**：`explain`（不带 `--step`）把 `observe.jsonl`（注入/确认）与轨迹合并成一份 run 视图 | — |
| ~~冲突确认无 TTL~~ | **已解决（0.2.0）**：pending 块默认 **7 天 TTL**，超时标记"未决冲突"、旧值保持生效；`memory review --pending/--candidates/--suspicious` 三个视图 | — |

## 五、工程与发布

| 限制 | 现状 | 影响 |
|---|---|---|
| ~~无版本标签~~ | **已解决**：`v0.2.0` tag＋Release（见 GitHub Releases） | — |
| 未发布 PyPI | 安装走 GitHub 直装 / 源码；分发名 `hippocampus-agent` 已在 PyPI 占位可用 | `pip install` 装不到 |
| ~~无导入/导出命令~~ | **已解决（0.2.0）**：`hippocampus export <目录>` / `import <目录>`（目录包含 manifest 与 schema 版本；导入默认不覆盖，`--force` 时旧库留 `.bak` 副本） | — |
| 演示脚本 | `scripts/demo.sh`／`scripts/demo.ps1`（起代理→灌数据→三格式请求→评测→结果表）；bash 版已实测，**PowerShell 版未实测** | Windows 用户首次运行可能需微调 |

## 六、明确不做（写清楚，避免误会）

- 多用户/多租户、云服务、账号体系：单机单用户是设计目标。
- 图形界面/浏览器插件：交互面是 CLI ＋ HTTP。
- 训练/微调模型：记忆层不训练模型。
- 记忆质量的普适承诺：效果随数据与模型变化，只报实测值并明示样本边界。
- **MCP 工具接入（已取消承诺，2026-09-17 定论）**：早期文案写过"含 MCP"，源码从未实现。
  已从简历与 README 的承诺中移除（简历 v6 无"含 MCP"；README 明说不承诺）。
  依据：Agent 形态当前是内置 5 工具；目标岗位 JD 中 MCP 命中 0 次（85 份统计）；
  接入 `langchain-mcp-adapters` 属投递后的扩展项，不再列为承诺。
