# 离线档能力边界（验收 A36 / A38）

**离线档的判据**：无 key、无网、无 git 也能跑通"记忆纪律"，且 CI 每次 push 都跑这一档。

## 离线可用（规则化，不需要模型）

| 能力 | 落点 |
|---|---|
| 显式记忆（`记住 X` / `忘掉 X`） | `memory_bridge.handle_explicit_memory` |
| **句式规则抽取**（"我只…／我不…／我是…／我正在…"，疑问句不抽） | `memory/extract.extract_by_rules`（本项目新增） |
| 四通道检索／断崖截断／预算装填 | `memory/retrieval` |
| 分层注入（stable／fluid）＋观察轨过滤（`shadow=1` 不注入） | `memory/memory_bridge.prepare_injection` |
| 去重与 P1 分流（supersede／admit／不丢） | `memory/dedup` ＋ `core.MemoryCore.write` |
| **规则冲突检测 → 挂起确认**（同对象取值不同） | `memory/conflict.detect_rule_conflicts`（本项目新增） |
| 确认消费（`确认 n`／`否决`）与候选转正 | `core.MemoryCore.confirm` |
| 生命周期判定与降级（`now` 可注入） | `memory/lifecycle` |
| 安全五组守卫（C·E 硬拦） | `memory/security` |
| 注入开关／学习开关 | `memory_bridge.handle_switch_command` |
| 评测跑批与结果表 | `eval/runner`（离线档作答器是**规则作答器**） |

## 离线档的守卫边界（2026-09-17 明确，B4）

守卫（漏抽补实体／检索兜底／消歧）原本**全链依赖 LLM**：离线档下每次抛 `LLMUnavailable`、
只累计 attempts 而不改动数据——"守卫空转"。现在的边界写死如下：

| 守卫 | 离线档行为 |
|---|---|
| `missed_extract`／`retrieval_guard`（补实体） | **走规则抽取**（jieba 分词→去停用词／偏好提示词→取最长实词，与 `write()` 的机械兜底同一套判据）；抽不出就按老口径累计 attempts，3 次后标 `no_entity_confirmed` |
| `disambiguate`（实体消歧） | 仍**软失败跳过**（消歧要判语义等价，规则判不准会造成错误合并；离线档宁可不错） |
| `hub_guard`（枢纽实体） | 规则可判（关系度数），照常标记 |

判据：`missed_extract.extract_entities_only` 在无端点时返回规则抽取结果，
`last_entity_source()` 可查这次走的是 `llm` 还是 `rules`（测试断言这个）。

## 离线不可用（明确不提供，不假装降级）

| 能力 | 原因 |
|---|---|
| 从**自由文本**里自动抽取记忆（隐含表达、跨句归纳） | 需要模型；离线只做句式规则 |
| 多步 agent 推理（模型决策循环） | 决策节点需要模型；离线档走规则策略 |
| 出站抓取（`fetch_url`） | 离线档不配置抓取器 → 判"环境错" |
| embedding 模型下载（`onnx:…` 档） | 离线档不下模型；用已装好的本地档或退默认词法档（下载链也过 A41 出站校验） |
| 上游模型转发（代理形态） | 没有端点；离线档返回"本地回执"，但记忆纪律照常执行 |

## 怎么验（都是可复跑命令）

```bash
# 1. 无凭据 + 离线跑整套测试（CI 的做法）
set HIPPOCAMPUS_OFFLINE=1
python -m pytest tests/ -q

# 2. 离线跑评测（10 题，记忆开/关对照）
hippocampus --home <tmp> seed
hippocampus --home <tmp> demo --memories

# 3. 离线跑 agent 任务 + 复演一致性
hippocampus --home <tmp> chat "我投简历有什么要求？" --offline --trace <tmp>/trace.json
hippocampus --home <tmp> replay <tmp>/trace.json

# 4. 全新环境（无 git、无网）跑 demo：见 README「快速开始」
```

## 演示纪律

离线档的输出会**明确标注自己是离线回执**（不冒充模型回答）；
评测报告会把"规则作答器"这一事实写进边界声明，不把离线结果当模型效果。
