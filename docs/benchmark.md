# 公开基准评测（LoCoMo / LongMemEval）

> 这份文档说明：**怎么跑、跑的是什么口径、数字的边界在哪**。
> 数字本身写在 `docs/roadmap.md` 与结果报告里；**官方分**（LLM 作答 + LLM 判分）已实现，
> 走 `bench --model-arm` 显式开关（见 §二·三）——离线档不报，不显式开就不出站、不编数字。

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

**基准默认钉在离线档**（不发出站请求）。`--online` 只是"允许联网"这道闸（让维护链的
软失败路径也能测）；**官方分走 `--model-arm`**（§二·三）：模型作答 + LLM 判分两步已实现，
但必须显式开——不开就不出站、不花钱。
此前在"环境里有 key"的情况下跑基准，记忆层的维护链对模型端点发了 205 次请求（全 401）——
既不可复现，也可能烧掉用户的钱，所以这一条是纪律而不是可选项。

### 换嵌入档（当前最大的一个变量）

```bash
# 神经档：首次联网下载一次 ONNX 到 ~/.hippocampus/models/<repo>/（之后纯本地推理）
HIPPOCAMPUS_OFFLINE=1 HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-base-en-v1.5" \
  hippocampus bench locomo --data D:/tmp/hc-bench/locomo10.json --json D:/tmp/locomo-bgebase.json
```

A/B 对照（一次跑多档、同批题、每档独立数据根；离线档）：

```bash
HIPPOCAMPUS_OFFLINE=1 .venv/Scripts/python.exe scripts/bench_ab.py \
  --data D:/tmp/hc-bench/locomo10.json --home-root D:/tmp/hc-bench/ab \
  --convs 3 --limit 80 --home-scope model --reuse-import \
  --variants "onnx:Xenova/bge-small-en-v1.5|instr=off,onnx:Xenova/bge-base-en-v1.5|instr=off,onnx:Xenova/gte-small|instr=off"
```

| 档 | 80 题 A/B | 全量 1986 题（k=8／k=20） | 说明 |
|---|---|---|---|
| `builtin-hash`（默认） | — | 36.7%／37.2% | 零下载、纯词法哈希；英文召回弱是它的**已知边界** |
| **`bge-small-en-v1.5`（384 维）** | 33.8% | **45.2%／49.8%** | **推荐档**：126 MB、p50 45.8 ms（比默认档还快） |
| `bge-base-en-v1.5`（768 维） | 41.3% | 46.5%／48.8% | 415 MB、768 维，**全量上与 bge-small 打平** → 不划算 |
| `gte-small`（mean 池化） | 27.5% | 未跑 | 本任务上更差 → 不推荐 |
| `bge-small-en` + 官方英文查询指令 | 32.5% | 46.5%（另一轮 47.8%） | 无收益（+80 tokens）→ **不登记** |

> **小样本会骗人**：80 题的 95% 置信区间约 ±8 pp。上表 bge-base 在 80 题上领先 7.5 pp，
> 全量上只差 1.3 pp 且 k=20 时反超——**换档结论必须用全量（或明确标注"样本级"）**。

> 选档只有"跑 A/B"这一条路（模型大小／名气／官方建议都不算证据，本表两条否定结论就是这么来的）。
> 池化方式按模型官方配置（bge＝CLS、gte＝mean）：拿错池化会得到"越换越差"的假结论。
> 换档只影响**查询侧**吗？不是——文档侧向量也变，所以**换档必须重新导入**（A/B 脚本按数据根隔离）。

### 二·三 官方判分臂（显式开关 `--model-arm`）

官方口径要两步：**模型基于注入上下文作答** ＋（LongMemEval）**LLM 判分**。prompt 与判分
算法**逐一对应官方仓库钉死的 revision**（实现见 `src/hippocampus/eval/model_arm.py`）：

| 基准 | 官方出处（钉 revision） | 判分口径 |
|---|---|---|
| LoCoMo-10 | `snap-research/locomo` @`3eb6f2c`（ACL'24） | 模型作答（官方 QA_PROMPT，temp 0）后按官方 `evaluation.py` 判分：cat1 拆子答案取 max、cat2/3/4 Porter 词干词面 F1、cat5 判拒答（"not mentioned / no information available"）；总体＝每题均值 |
| LongMemEval-oracle | `xiaowu0162/LongMemEval` @`9e0b455`（ICLR'25） | 官方生成 prompt（history_format=full，temp 0，max_tokens 500）＋官方 `get_anscheck_prompt` 逐题型 judge（temp 0、max_tokens 10，`'yes' in lower` 为对；`_abs` 题走 abstention 模板）→ 准确率 |

**唯一输入差异（口径脚注，对照表每行必带）**：官方喂的是**全文**，我们喂的是**记忆层
选出的注入上下文（top-8，`inject_finalize` 原文）**——这正是要测的东西（记忆系统选得
对不对），所以我们的数字**不与全文基线直接等同**，要同表、带脚注地比。

```bash
# 需要环境变量：HIPPOCAMPUS_API_KEY（或密钥服务）、HIPPOCAMPUS_BASE_URL、HIPPOCAMPUS_MODEL
# 护栏（写死在实现里，可调）：并发 16、失败重试 2、单调用超时 120 s、预算 ¥30 硬停
# （按 usage 估算花费累计；跑前/跑后各查一次余额，实际花费＝余额差）
export HIPPOCAMPUS_BASE_URL="https://api.deepseek.com/v1" HIPPOCAMPUS_MODEL="deepseek-chat"

# LME-oracle 官方判分（全量 500 题会花几分钟到十几分钟；--limit 控制抽样）
HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-small-en-v1.5" \
  hippocampus bench longmemeval --data D:/tmp/hc-bench/longmemeval_oracle.json \
  --limit 200 --model-arm --json D:/tmp/hc-bench/lme_official.json

# LoCoMo-10 官方判分（全量 1986 题 ≈ 2000 次调用）
HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-small-en-v1.5" \
  hippocampus bench locomo --data D:/tmp/hc-bench/locomo10.json \
  --model-arm --json D:/tmp/hc-bench/loco_official.json
```

报告 JSON 的 `official` 段含：模型名／temperature／并发／重试／超时／预算、`input_spec`
（8 条注入非全文）、调用次数、tokens、估算花费、**跑前/跑后余额与实际花费**、逐题
prediction/label/error、失败数与跳过数（有失败必须在报告里如实写"未跑完"）。

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
| 判分 | 离线档**无 LLM 判分**（`token_f1` 只是诊断值）；官方分走 §二·三 的 `--model-arm`（显式开关才出站） |

### 为什么绝对分不可能高（如实说）
1. **语料是英文**，而本项目的分词、停用词、阈值、句法抽取都是**为中文标定**的（默认嵌入档
   `builtin-hash` 是词法级哈希向量，英文同义改写召回弱）——这是实测偏低的主因。
2. **只注入 top-8 条**：LoCoMo 官方给模型的是**整段对话**（数百轮），本项目给的是记忆层
   筛出来的 8 条；两者不是同一件事，**官方分也因此不能与"全文基线"直接等同**——
   要同表、带口径脚注地比（§二·三）。
3. 离线档的"作答器"是引文（不做生成、不做推理），多跳/时间推理题天然吃亏；`--model-arm`
   的模型作答不受此限，但受第 2 条的输入限制。

## 四、复跑与回归

- CI 里跑的是**小样本**（`--limit`）冒烟，判据是"能跑通 + 不崩"；分数回归靠人工比对 JSON。
- 报告 JSON 里带 `dataset_sha256`、`protocol`（含 `date_prefix` / `inject_max_items` /
  `as_memories` / `shadow_log`），复跑时对得上才算同一口径。
