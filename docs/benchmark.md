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

### 二·二b 失败归因（`bench_diag`）与复用导入的两个坑

**`bench_diag` 用法**（E2/G7：文档补齐；离线档，复用 A/B 的数据根免重复导入）：

```bash
# 证据没进上下文时，拆一层：证据轮的一上/一下轮在不在？同会话其它轮在不在？
HIPPOCAMPUS_OFFLINE=1 HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-base-en-v1.5" \
  python scripts/bench_diag.py --data D:/tmp/hc-bench/locomo10.json \
  --home D:/tmp/hc-bench/ab/onnx-Xenova-bge-base-en-v1.5 --convs 3 --limit 80 \
  --json D:/tmp/hc-bench/diag_neighbors.json
```

输出三行：证据命中；未命中里**证据轮上一轮/下一轮已在上下文**（=「±1 轮扩展」能捞回的上界）；
未命中里至少同会话有其它轮（会话级召回到了、轮级定位没到）。判据细节：同会话/邻居判据为
**整句精确匹配**（normalize 后全文在上下文里才算），不用短前缀——前缀会撞上别的轮导致高估
（E3/G8 修正，宁可低估不高估）。

**复用导入双表坑**（E5/G10）：`bench_ab.py --reuse-import` 判定"这个数据根导过没有"查的是
**memories＋episodes 双表**——基准导入口径把用户轮落记忆条、助手轮落经历层，只查一张表会把
"只有助手轮的对话"误判成没导入而重复导入（向量重复计数、数字虚低）。

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

**实测结果（官方判分臂，2026-09-18 批次；全部取自既有报告 JSON，零新花费）**

| 基准 | 题量 | 官方分（95% CI） | 同批检索（证据命中／答在文内） | 花费（估算／余额可见） |
|---|---|---|---|---|
| LongMemEval-oracle | **全量 500** | **74.2%（70.2%–77.8%，Wilson）**，0 失败 0 跳过 | 99.6%／40.0% | ≈¥1.104／¥0 |
| LongMemEval-oracle（早先抽样批，留作口径历史） | 抽 200 | 70.5%（63.8%–76.4%，Wilson） | 100.0%／48.5% | ≈¥0.45／¥0.39 |
| LoCoMo-10（基线） | 全量 1986 | F1 **32.55%（30.8%–34.4%，bootstrap）** | 45.52%／17.37% | ≈¥3.87／滞后未显 |
| LoCoMo-10 + `--neighbors` | 全量 1986 | F1 **38.68%（36.8%–40.5%，bootstrap）** | 63.65%／22.7% | 同批 |

- LME 分型（全量 500，官方 judge 口径）：knowledge-update 84.6（n=78）、temporal-reasoning 76.7（n=133）、
  multi-session 62.4（n=133）、single-session-assistant 41.1（n=56）、single-session-preference 96.7（n=30）、
  single-session-user 97.1（n=70）。
- judge 一致性标定（同题双判 50 题，deepseek-chat ↔ glm-4-flash）：**分歧率 8.0%（4/50）**，4 条都是
  "deepseek 判错／glm 判对"形态 → 本表官方分统一标注为 **deepseek-judge 口径**（人工裁决未做，不假装已做）。
- **八轮 V11 零花费扩样后的完整地基**（`scripts/bench_judge_audit.py` 可复跑；判分口径与所有分数**一律未动**）：
  1. **判分实现层**：把该批 500 题落库的 `judge_response` 按官方逐字规则（`'yes' in lower` 为对）
     重推一遍标签，与记的 `label` 逐条比对 → **500/500 一致，0 处分歧**（两批合计 1000 次判分同样 0 处）；
     也就是说"我们把 judge 的话折成对/错"这一层没有抖动，有抖动的是 judge 模型本身；
  2. **跨批复判不可用**（如实记，别拿它当证据）：仓外两份全量批报告虽同为 500 题、`qid` 全交集，
     但 **prediction 相同的题数为 0**（两批各自重新作答，混着作答漂移），且其中一批存在
     `label` 未判成的情形 → 371/500 的"标签差异"**不能**解释成 judge 抖动，只能当参考；
     另一个坑：`label` 在 JSON 里可能是 `None` 或字符串，用 `bool(label)` 判真假会把 `"False"` 也判成对，
     必须显式解析（审计脚本里已写死这条）；
  3. **人工抽判 20 题**（按 `qid` 升序取 judge 判"否"的前 20 题，逐题读 question/gold/prediction 重判）：
     与 judge 不一致 **2/20 = 10.0%**，两条方向一致——都是"judge 判否、人工可放宽"（一题答对了资源名但
     表述方式不同，一题问的是活动建议、回答里含对了偏好对象）→ 说明这条地基**偏严不偏松**，
     即官方分更可能是保守下界，不是虚高。
  - **结论仍未定**：双判 50 题＋人工 20 题合并也只有 **n=70 < 100**，且人工裁决只覆盖"判否"侧。
    要把分歧率做到统计上有意义，需要一次带第二 judge 的 ≥100 题批次——那属付费评测批次，
    **维持冻结**（本轮一分钱不花）。引用本表时请带上这条限定，别把 74.2% 说成"judge 无关的客观分"。
- **两批 LME 不同源**（n=500 与 n=200 的检索与花费各自成行），引用时**不要混用**；CI 统计相容
  （70.2–77.8 ⊃ 70.5），全量批 CI 更窄，故以 74.2% 为头条。

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

### 二·四 ±1 轮邻居扩展（A1/T7，`--neighbors` 显式开）

**机制**：基础注入（top-8）之后，把**已注入轮的上一轮/下一轮**（同 session 内，不跨会话越界）
追加进上下文（追加预算 `--neighbor-budget`，默认 6，只裁剪追加量）。为什么这样扩：
`bench_diag` 实测未命中题里 38.3% 的**证据轮邻居已在上下文**——检索命中的往往是证据轮的
邻居，扩一轮就能把证据本身带回来（原检索口径上界 +22.5pp，本轮把上界兑现了大半）。

**实测（LoCoMo 全量 1986，bge-small 神经档）**：

| 口径 | 证据在文内 | 答在文内 | tokens | 官方 F1（模型臂） |
|---|---|---|---|---|
| 无邻居（既有） | 45.7% | 17.4% | 1354 | 32.55%（CI 30.8–34.4） |
| **`--neighbors`（budget 6）** | **63.65%** | 22.7% | **1767（+413，+30%）** | **38.68%（CI 36.8–40.5，+6.13pp）** |

分类看：cat2 时间 6.7→**11.06**（短板类被正面改善）；cat4 单跳 38.5→51.77；cat1 20.8→25.38；
cat5 对抗 51.4→**48.0（−3.4pp，对抗题邻居可能引入干扰，如实记录）**。检索口径≥48% 达标；
官方分联动验证（T7-④，预算内）也过了——"检索提升 → 官方分提升"因果链闭合。

> **cat5 负效应归因注（六轮 G5/B4 收口，2026-09-18，0 花费，逐题对照两份官方分 JSON 的
> `official.rows`，2026-09-18 本机独立复算复核）**：机理实锤＝**错位锚定**，不是稀释——
> 对抗题（gold=`['None']`）的正确行为是拒答（"Not mentioned in the conversation"，判 1 分）；
> ±1 邻居把相邻轮的**貌似相关的事实**推进上下文，诱导模型改口作答具体内容，被判 0 分。
> 逐题配对 n=446：**46 题 1→0（拒答被诱导为作答）、31 题 0→1、持平 369，净 −15 题＝−3.36pp**
> （与总表 −3.4pp 吻合）；掉分题 46/46 的答案文本均发生漂移，且邻居追加数（均值 5.98）
> 与全体 cat5 无差——掉分与邻居**数量**无关，与邻居轮**是否含干扰事实**有关。
> 掉分样例（前 4 条，pred 原文）：conv-26-q169「Why Caroline took up running」gold=None，
> 无邻居="(b) Not mentioned"→邻居="(a) To de-stress and clear her mind"；
> conv-26-q174 gold=None→"(a) once or twice a year"；conv-26-q197 gold=None→
> "(b) Went on a nature walk or hike."；conv-30-q85 gold=None→"(a) Glad"。
> **建议（结论即可，改不改听栗子）**：维持邻居扩展不变——整体 +6.13pp ≫ cat5 按题加权
> 的 −0.77pp；若按类别裁剪（对抗类不加邻居）代价约 −0.8pp 整体分，属提分方向，
> 按"评估数据优化暂停"纪律不主动实施。与正本 §10.1 注同源。

复跑：
```bash
HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-small-en-v1.5" hippocampus \
  --home D:/tmp/hc-bench/run-loco-neighbors bench locomo --data D:/tmp/hc-bench/locomo10.json \
  --neighbors --json D:/tmp/hc-bench/loco_neighbors.json   # 检索口径
# 官方联动：+ --model-arm（需端点+凭据；预算闸 ¥30 内，全量 ≈¥4.6）
```

### 二·五 英文实体抽取 A/B 与逐通道消融（T8/T9，2026-09-18）

**英文实体（T8，A4）**：`--english-entities` 在导入时机械抽取英文专名（句中大写连续词，排除
句首/停用词）挂进图通道。全量 LoCoMo 1986 对照（同一 `bench` 口径、同批题）：
**off 43.0% → on 42.55%（−0.45pp，n=1986）——否定结论**：图通道对英文实体的贡献接入后仍
无收益，瓶颈仍在轮级定位（与 §二·四 的邻居扩展结论一致）。复跑：`bench locomo --english-entities`。

**逐通道消融（T9，A5/A6）**：`scripts/bench_ablation_channels.py`（497 题抽样，3 段对话，
bge-small，每变体独立数据根，只关一个通道）：

| 变体 | 证据在文内 | 差（pp） |
|---|---|---|
| 全开 | 42.66% | — |
| 关 semantic | 42.86% | +0.20 |
| 关 bm25 | 43.26% | +0.60 |
| 关 graph | 42.86% | +0.20 |
| 关 event | 42.45% | −0.21 |

**结论一句话**：四通道两两高度冗余，关任何一个都在 ±0.6pp 内（n=497 标准误 ±1.1pp 内=统计打平）——
**单一通道都不是瓶颈**；既有"去掉整个记忆条层掉 9pp"的粗口径依然成立（那是整层，不是单通道）。
哨兵：`--sentinel --sentinel-threshold 45`（神经档全开证据命中下限）；CI 用全量档跑，低于则红。

### 二·六 多账户常态压测（T4/A15，`scripts/bench_multi_account.py`）

日常可跑的多账户/长跑压测（合成数据、离线、零凭据）：N 账户每账户几条记忆 + 1 次查询，
报告**证据命中**与**进程峰值工作集**（LRU 上限生效时多账户内存有界）。200 账户实测
（2026-09-18，cache_max=16）：证据命中 **100%**、峰值 **365.8 MB**、45s。看门限
（`--sentinel` 启用）：证据命中 ≥98%、峰值 <2000 MB。CI 可带。复跑：
```bash
python scripts/bench_multi_account.py --accounts 200 --json D:/tmp/multi_account_200.json
```

### 二·七 批次漂移归因（T10，C1 收口）

LME 200 题三臂对照（同数据同代码同嵌入，离线）：fresh home A 第一遍 **48.5%**；**同一 home
第二遍 42.5%（−6.0pp）**；fresh home B 48.5%（与 A1 差 0.0pp）。**结论：跨 fresh 批次恒等、
同库重放掉 6pp**——漂移变量是**同库重放的候选排序**（第一遍 touch 了命中记忆 → 第二遍
"最新优先"排序失去区分度 → 12 题答案被挤出注入位，n_injected/tokens 分布同步变化），
**不是批次/数据/嵌入变量**。测量纪律：**官方分与检索口径的基准数字以 fresh 库第一次跑为准**；
复跑必须清 home（`run-*` 目录不复用）。

### 二·八 cat3 开放域低分归因（七轮 T8，2026-09-19 · **零新花费**）

本轮评测批次冻结，所以这里**不重跑模型**——只读已付过费的两份官方判分产物
（`loco_official_full.json` 基线臂／`loco_nb_official.json` ±1 轮邻居臂）＋数据集本身，
按 `qid` 逐题配对。方法沿用 §二·四 的 cat5 归因。

| 口径 | cat3（开放域，n=96）平均官方分 |
|---|---|
| 基线臂（top-8 注入） | **13.91%** |
| 邻居臂（`--neighbors`） | **12.25%** |

**配对结果**：0→1 共 3 题、1→0 共 3 题、**两臂都不过 83/96**。
→ 邻居扩展对 cat3 **没有方向性作用**（涨跌对称），且短板是**稳定**的、不是抖动。

**机理拆分**（以基线臂不过的 86 题为样本）：

| 观察 | 数量 | 读法 |
|---|---|---|
| 作答为空（模型没给内容） | **0/86** | 记忆层**没有交白卷**：证据基本都给到了注入位 |
| 金标是多项／含逗号（须并列全对） | 22/86 | 例：金标 `Psychology, counseling certification`，模型答 `Helping others` |
| 金标是长句（词面 F1 天然吃亏） | 25/86 | 例：`Yes, since she collects classic children's books` vs 模型只答 `Yes` |
| 模型作答极短（≤2 词） | 29/86 | 短判定词题被答成解释句、或解释句被答成短语 |
| 证据条数分布 | 1 条 39、2 条 23、3 条 9、4 条 8、**0 条 4** | 4 题数据集本身没标证据（无依据可召回） |

样例（截断）：

```
conv-26-q14  金标: Likely no            模型: Yes, passionate about creating a safe, inviting place…
conv-26-q30  金标: Likely no, she does not refer to herself as part of it   模型: Yes   （证据 0 条）
conv-26-q50  金标: Liberal               模型: Standing up for equality
conv-26-q59  金标: Somewhat, but not extremely religious   模型: Not enough information to answer.
```

**结论（机理，不是提分方向）**：cat3 是**开放域推断题**（"Would X likely…"），金标常是一个
**短判定词**（`Likely no`／`Liberal`／`Somewhat`），而模型按注入证据作答时给出的是**解释性表述
或近义短语**——官方指标是 **Porter 词干后的词面 token-F1**，语义对、词面不对就判 0。
所以 cat3 的低分主要是**判据口径 × 题型**的错配，而非记忆层召回失效（0 空答是最直接的证据）。
要真提这一档，得换判据（cat3 用 LLM 判分）或做英文原生适配——**两者都属"提分方向"，本轮不做**。

> **口径订正（同日记）**：`CHANGELOG.md` [0.3.0] 里"cat3 开放域（12.25%）"是**邻居臂**的值，
> 基线臂是 13.91%（正本 §分类表 13.9% 与之一致）。引用时请带臂名，两个数不是同一次运行。

复跑（只读，零出站）：

```bash
python scripts/bench_cat_attrib.py --category 3 --json D:/tmp/hc7/cat3.json
```

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
