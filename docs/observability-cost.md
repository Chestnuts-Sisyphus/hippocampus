# 结构化 trace 与成本表（GF1·①②）：一次检索花多少钱、慢在哪一段

> 这份文档回答两个会被面试官追问的问题，并给出**可复跑的产物**：
> ① "你这条记忆链路，一次检索的延迟怎么分解？" ② "跑一次要花多少钱？贵在哪？"
>
> 口径纪律：文档里每个数字都标了证明等级 —— `[已证明]`（可复跑命令或 file:line）、
> `[归纳待证]`（派生量，方法写在旁边）、`[假设]`（场景参数，不是测量值）。
> **本文件不含任何自造数据**：真值来自既有付费批次的 provider usage，派生量由零花费脚本算出。

## 〇、三件产物（都在仓内，可复核）

| 产物 | 路径 | 复跑命令 |
|---|---|---|
| 一次检索的 OTLP trace（16 span／1 trace） | `results/trace_retrieval_otlp.json` | `python scripts/trace_retrieval.py` |
| 成本表（真值拆段＋现价） | `results/cost_retrieval_injection.json` | `python scripts/bench_cost_model.py --report <仓外 lme_official_500b.json>` |
| 行为闸（trace 语义） | `tests/test_n54_r10_gf1_trace_spans.py` | `pytest tests/test_n54_r10_gf1_trace_spans.py` |

## 一、为什么需要 trace（此前缺什么）

仓内原有两类**扁平事件**，都不是 trace：

- `audit.jsonl`：一次检索的**候选全集**（谁进了、谁被剔、为什么剔）——回答"选得对不对"；
- `observe.jsonl`：injection／confirmation／verification 三类事件——回答"发生了什么"。

它们没有 span 层级、没有父子关系、没有延迟分解，也无法回答"这次检索里嵌入推理占了多少毫秒、
向量查询发了几次"。`hippocampus/observability/` 补的就是这一层。

**不引 opentelemetry-sdk 的理由（重要，别当成偷懒）**：`pyproject.toml` 没声明它，
它只随**可选档 `vector`（chromadb）** 进环境；核心档与 CI 的 core-only job 装不上。
引成硬依赖＝十轮 X15 点名的"未声明／漂移依赖"同类问题（当时是 onnx 三件靠 chromadb 传递依赖偶遇）。
所以本模块按 OTel 数据模型自实现，导出 **OTLP/JSON**（标准工具可直接吃），零新依赖。

## 二、埋点落在哪三个出口（为什么是这三处）

| span 名 | 埋点位置 | 为什么是这里 | 关键属性 |
|---|---|---|---|
| `hippocampus.retrieval_request` | `core/core.py` `inject_finalize` | 检索的**公开入口**，由它开根 ⇒ "一次 `inject_finalize` ＝ 一条 trace"是**系统属性**，不靠调用方自觉 | `account`／`session`／`flow`／`query_chars`／最终 `items` |
| `hippocampus.injection` | `memory/memory_bridge.py` `prepare_injection` | 注入侧唯一出口（检索＋分层组装都在这层） | `stable_chars`／`fluid_chars`／`n_retrieved`／`layered` |
| `hippocampus.embedding` | `memory/retrieval.py` `_query_embedding` | 查询向量只在这里算一次，LRU 命中与否只有这里知道 | `dim`／`cache_hit`／`text_chars`／`has_query_instruction` |
| `hippocampus.vector_query` | `memory/retrieval.py` `_query_collection` | **向量查询唯一出口**（mem/ep 两池、两种入参形态共用一个函数） | `n_requested`／`n_returned`／`collection`／`used_query_embedding` |

口径细节（刻意分开的两个计数，别混）：
`hippocampus.injection.n_retrieved` 是**检索返回条数**；最终注入条数记在根 span 的
`hippocampus.injection.items`——上层还会定序／限额／逐条装载，可能再减几条。
实测样例里 `n_retrieved=2` 而 `items=1`，两个数都对，是两层的口径不同。

## 三、一次检索的实测 trace（延迟分解）

`python scripts/trace_retrieval.py` 的真跑输出（离线档、`builtin-hash` 384 维、装了 chromadb）：

```
[PASS] ① OTLP/JSON 可解析：16 个 span
[PASS] ② trace 数 = 1
[PASS] ③ hippocampus.retrieval_request × 1 ／ embedding × 4 ／ vector_query × 10 ／ injection × 1
[PASS] ④ 时间戳齐全（延迟可分解）／父子关系闭合（悬挂：[]）

  - hippocampus.retrieval_request            19.00 ms
      - hippocampus.injection                14.00 ms  n_retrieved=2
          - hippocampus.embedding             1.00 ms  dim=384 cache_hit=False
          - hippocampus.vector_query          1.00 ms  n_returned=3   （×8，mem/ep 两池各多次）
      - hippocampus.embedding                 0.00 ms  dim=384 cache_hit=True
      - hippocampus.vector_query              1.00 ms  n_returned=3   （×2，注入后的旁路检索）
```

**怎么读这张图**（面试可以直接这么讲）：

1. **一条 trace ＝ 一次检索**，根是 `inject_finalize`；`19 ms` 里 `injection` 段占 `14 ms`，
   其余是它之外的旁路检索（去重/维护扫描），不是重复劳动。
2. **嵌入不是瓶颈**：4 次 embedding 里 3 次 `cache_hit=True`（LRU 256 容量），
   首次 `1 ms`、命中后 `0.00 ms`——**查询侧重复问题几乎零成本**。
3. **向量查询是次数多、单次便宜**：10 次查询每次 `0–1 ms`，因为 `n_returned=3` 是小集合；
   次数多的原因是 mem/ep 双集合 × 多通道调用，不是性能问题。
4. **口径诚实点**：`n_returned=3` 是这条演示库里只有 3 条种子记忆，
   不代表生产规模；延迟数值随机器与数据量变，**结构**（谁是谁的子、各有几个）才是这份产物的稳定部分。

> 复跑注意：traceId／spanId 每次随机（OTel 语义），毫秒数每次不同；
> 脚本自己断言的是**结构**（trace 数、span 数、三类 span 齐全、父子闭合），不是毫秒数。

## 四、成本表：一次「检索＋注入＋作答」要多少钱

### 4.1 真值来源（不花钱，但也不是估算）

既有付费批次 `lme_official_500b.json` 的 `official` 块是 **DeepSeek 返回的 `usage` 逐次累加**：

| 项 | 值 | 等级 |
|---|---|---|
| 模型 / 批次 | `deepseek-chat`，n=500，2026-09-18 | `[已证明]` |
| 调用次数 | 作答 500 ＋ 判分 500 | `[已证明]` |
| **prompt tokens** | **399,373** | `[已证明]` |
| **completion tokens** | **38,184** | `[已证明]` |
| 仓内估算花费 | ¥1.1042（`cost_est_yuan`） | `[已证明]` |

**拆段方法**（`[归纳待证]`，脚本可复跑）：用仓内**同一套 prompt 模板**＋报告里逐题的
`context`／`question`／`prediction`／`judge_response` 在本地重建四段文本，量出占比，
再按占比把上面的**真值总量**拆到各段。拆分系数 `k_prompt=1.018`／`k_completion=1.015`
——代理分词器（`tiktoken:o200k_base`）与真值总量只差 ~2%，说明拆分口径站得住。
已知偏差：`{date}` 槽（`question_date`）不在报告里，按空串渲染 → 作答输入段**偏小**十几个字符。

### 4.2 单请求口径（生产臂，不含判分）

| 项 | 值 | 等级 |
|---|---|---|
| 输入 tokens | **578.6** | `[归纳待证]` |
| 输出 tokens | **75.4** | `[归纳待证]` |
| 其中**记忆注入段** | **524.9 tok（占输入 90.7%）** | `[归纳待证]` |
| 判分臂（只在评测出现） | 输入 220.1 ／ 输出 1.0 | `[归纳待证]` |

**这张表最该被记住的一行**：输入里 **90.7% 是记忆层注入的上下文**（top-8 条）。
所以"记忆系统的成本"≈"注入预算的成本"——想省钱，动的是注入条数/长度，不是别的。

### 4.3 现价与钱（来源 URL 见下）

价目来源：<https://api-docs.deepseek.com/zh-cn/quick_start/pricing>（2026-09-22 核对）

| 档 | 输入 cache miss | 输入 cache hit | 输出 |
|---|---|---|---|
| deepseek-flash 高峰 | ¥2 / M | ¥0.04 / M | ¥8 / M |
| deepseek-flash 空闲（半价） | ¥1 / M | ¥0.02 / M | ¥4 / M |
| deepseek-v4-pro 高峰 | ¥9.0 / M | ¥0.30 / M | ¥27.0 / M |

**仓内 `model_arm.PRICE_*`（¥2/M 输入、¥8/M 输出）与现价页的 flash 高峰档逐位一致**——
代码注释写的"按最贵口径估，宁高不低"在今天仍然成立（`[已证明]`）。

按最贵档算（**$ 与 ¥ 分开列，避免汇率臆测**）：

| 口径 | 结果 |
|---|---|
| 单次请求 | **¥0.00176** |
| 每 1000 次 | **¥1.76** |
| 每 1000 次中**只算注入段** | **¥1.05** |
| 每月 @ `[假设]` DAU=1000 × 10 次/日 × 30 天（30 万次） | **¥528.02** |

**$ 口径换算须知**：本表按人民币计（价目页单位是元）。要报美元，用当日报价换算并注明汇率与日期
——本文**不给美元数**，因为我没有可考证的当日汇率来源，不编。

### 4.4 稳定层是每请求动态生成，还是可缓存？（两个问题要分开答）

这是本题最容易答错的地方，**答案分两层，两层都要说**：

1. **生成侧：每请求动态生成。** `memory/memory_bridge.py` `stable_layer(conn, user_text)`
   每次都跑一遍机械实体匹配（`extract_query_entities` → `entities` 表 → `memories.entity_ids LIKE`
   → `status='active' AND type IN ('preference','fact')` → `updated_at` 降序取前 `theme_layer_max` 条）。
   它**吃当前 query**，所以严格说不是"常量前缀"。
2. **可缓存侧：内容高度可复用，且已经有落点。** 身份声明是固定文本（`IDENTITY_DECLARATION`），
   主题层按"最近更新"取前 N 条——同一话题连续几轮几乎不变。**缓存落点证据**：

   - `docs/proxy.md:31-38`（分层口径正本）："稳定层每轮都在、可被缓存；流动层只放本轮相关"，
     表格写明 anthropic 入站时 stable 追加进 `system` 块数组、"够长时挂 `cache_control: ephemeral`"；
   - `src/hippocampus/proxy/formats.py:126`（**落点实现**）：
     `if len(stable) // 3 >= 1024: stable_block["cache_control"] = {"type": "ephemeral"}`
     —— 阈值注释写明"近似 token 数（Sonnet 最小可缓存 1024 token）"；
   - `src/hippocampus/proxy/format_converters.py:188-202` ＋ `proxy/app.py:266-274`：
     入站 `cache_control` 的**透传开关** `cache_control_passthrough`（默认 True，按账户活跃参数读）。

3. **诚实的现状（这一条最容易被追问穿，主动说）**：实测演示里稳定层只有 **82 字符**
   （`82 // 3 = 27 < 1024`）→ **当前默认配置根本没到可缓存阈值**，也就是说"设计上可缓存、
   实测里还没吃到"。要达到阈值，稳定层得有约 3KB 文本（≈1024 token）。

**缓存收益敏感性**（`[假设]`：稳定层 token 数为场景参数，非实测值；按 flash 高峰档）:

| 稳定层规模 | 不缓存 | 命中缓存后 | 每 1000 次省 | 每月省（@30 万次） |
|---|---|---|---|---|
| 200 tok | ¥0.000400/次 | ¥0.000008/次 | ¥0.39 | ¥117.60 |
| 500 tok | ¥0.001000/次 | ¥0.000020/次 | ¥0.98 | ¥294.00 |
| 1000 tok | ¥0.002000/次 | ¥0.000040/次 | ¥1.96 | ¥588.00 |

读法：缓存省的是**输入单价**（¥2/M → ¥0.04/M，50 倍差），所以稳定层越大、缓存越值；
但在本项目当前的稳定层规模下，绝对金额是"几毛钱/千次"量级——**该讲的是机制与阈值，不是收益数字**。

### 4.5 三个必须一起说的 caveat

1. 拆段是**线性比例法**：真值总量是 provider 实测，段间占比来自本地重建（代理分词器）；
2. 作答 prompt 的 `{date}` 槽缺失 → 作答输入段偏小（量级：十几个字符）；
3. **`deepseek-chat` 在 2026-09-22 的现价页上已不单列**（页面现列 `deepseek-flash`／`deepseek-v4-pro`，
   并说明旧名仍可调用、由 V4.1-Flash 承接）。批次当时用的是 `deepseek-chat`；本表按现价页的
   flash 档计价——**若模型映射变化，金额要重算**。这条是"数字会过期"的实证，不是免责声明。

## 五、怎么把它讲成一段面试答案（30 秒版）

> "我的记忆层一次检索会落一条 OTLP trace：根 span 是 `inject_finalize`，下面是注入层，
> 再下面挂嵌入和向量查询——实测 19ms 里注入段 14ms，嵌入有 LRU、命中后接近零，向量查询
> 次数多但每次不到 1ms。成本我按 provider 返回的真实 usage 算：一次请求输入 579 token、
> 输出 75 token，其中 **91% 的输入是记忆注入本身**，按现价最贵档约 ¥1.76/千次。
> 稳定层是每请求算出来的，但内容可复用、已经有 `cache_control: ephemeral` 的落点；
> 我如实说一句：当前稳定层只有 82 字符，**还没到 1024 token 的可缓存阈值**，
> 所以这个优化现在是"设计就位、收益未兑现"。"
