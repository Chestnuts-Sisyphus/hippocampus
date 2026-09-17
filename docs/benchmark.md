# 公开基准评测（LoCoMo / LongMemEval）

> 这份文档说明：**怎么跑、跑的是什么口径、数字的边界在哪**。
> 数字本身写在 `docs/roadmap.md` 与结果报告里；**官方分**（LLM 作答 + LLM 判分）本项目
> 在离线档不报——报了就是编。

## 一、取数据（钉版本 + 校验 sha256）

数据集不进仓库（体积 + 许可），按下面两条命令拉到本机（建议放非系统盘）：

```bash
cd <数据目录，例如 D:/tmp/hc-bench>
curl -L -o locomo10.json \
  https://raw.githubusercontent.com/snap-research/locomo/3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376/data/locomo10.json
curl -L -o longmemeval_oracle.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_oracle.json
```

校验（版本写死在 URL 里，哈希写死在这里——两者对不上就不是同一份数据）：

| 文件 | 体量 | sha256 |
|---|---|---|
| `locomo10.json` | 2.8 MB（10 段对话 / 1986 题） | `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` |
| `longmemeval_oracle.json` | 15 MB（500 题） | `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c` |

```bash
certutil -hashfile locomo10.json SHA256        # Windows
sha256sum locomo10.json                        # Linux/macOS
```

LongMemEval 还有一个 277 MB 的 `longmemeval_s_cleaned.json`（每题 haystack ≈53 个会话），
需要更大规模时按同样方式取（revision 同上）。

## 二、跑分

```bash
# LoCoMo 全量（10 段对话 / 1986 题）
hippocampus bench locomo --data D:/tmp/hc-bench/locomo10.json --json D:/tmp/locomo.json

# LongMemEval（500 题；--limit/--offset 控制抽样）
hippocampus bench longmemeval --data D:/tmp/hc-bench/longmemeval_oracle.json --limit 100

# 消融：只进经历层（不建记忆条）／改注入条数上限
hippocampus bench locomo --data <同上> --no-memories
hippocampus bench locomo --data <同上> --inject-max-items 20
```

**基准默认钉在离线档**（不发出站请求）。要跑模型臂必须显式 `--online`：
此前在"环境里有 key"的情况下跑基准，记忆层的维护链对模型端点发了 205 次请求（全 401）——
既不可复现，也可能烧掉用户的钱，所以这一条是纪律而不是可选项。

## 三、口径（写在表头上，别让读者猜）

| 项 | 本项目怎么算 |
|---|---|
| 导入 | 每组一段对话（LoCoMo＝一段对话；LongMemEval＝一题）进独立 account：每轮 → 经历条 + 记忆条（`fact`），实体只机械挂说话人；**批内不逐条同步索引**（否则退化成 O(n²)） |
| 日期 | 会话日期写进记忆条内容（`[2023-05-07] …`）；事件层本来就会渲染 `[user 2023-05-08] “…”` |
| 检索 | 走生产路径 `inject_finalize`（四通道 + 断崖 + 预算 + 注入条数上限 8，默认参数） |
| `answer_in_context` | 参考答案的**等价写法**（含日期 ISO 变体 `2023-05-07` / `2023-05` / `2023`）出现在注入上下文里 |
| `evidence_in_context` | 金标准证据原文（LoCoMo 的 `evidence` dia_id 对应轮；LongMemEval 的 `answer_session_ids` 会话）进上下文 |
| `token_f1` | 离线作答器＝注入里分数最高的条目原文；**无模型生成**，所以 F1 只是诊断值，不是"答得好不好" |
| tokens / 延迟 | 上下文按 `len//2+40` 估算；延迟＝单题 `inject_finalize` 的墙钟（p50/p95） |
| 判分 | **无 LLM 判分**：官方口径要用大模型作答 + 判分，本项目离线档不报官方分 |

### 为什么绝对分不可能高（如实说）
1. **语料是英文**，而本项目的分词、停用词、阈值、句法抽取都是**为中文标定**的（默认嵌入档
   `builtin-hash` 是词法级哈希向量，英文同义改写召回弱）——这是实测偏低的主因。
2. **只注入 top-8 条**：LoCoMo 官方给模型的是**整段对话**（数百轮），本项目给的是记忆层
   筛出来的 8 条；两者不是同一件事，**不能把本文的分数和"把整段对话喂给模型"的报分直接比**。
3. 离线档的"作答器"是引文（不做生成、不做推理），多跳/时间推理题天然吃亏。

## 四、复跑与回归

- CI 里跑的是**小样本**（`--limit`）冒烟，判据是"能跑通 + 不崩"；分数回归靠人工比对 JSON。
- 报告 JSON 里带 `dataset_sha256`、`protocol`（含 `date_prefix` / `inject_max_items` /
  `as_memories` / `shadow_log`），复跑时对得上才算同一口径。
