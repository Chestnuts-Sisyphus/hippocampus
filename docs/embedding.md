# 嵌入档与阈值标定

## 为什么分档

相似度是**相对量**：换嵌入模型 = 换量纲。前身的教训正好是这条——线上用
`onnx_mini_lm_l6_v2`（英文模型）时，中文短句相似度整体虚高（实测「你好」与任意记忆
sim 0.7578），标定报告结论是"三视角无可行单阈值"。所以本项目把"档位 → 参数"做成
显式表（`hippocampus/memory/calibration.py`），并配一个可复跑的标定脚本。

## 两级默认（验收 A37）

| 档 | 配置值 | 依赖 | 网络 | 定位 |
|---|---|---|---|---|
| **内置档**（默认） | `builtin-hash` | 无（纯 Python 标准库） | **零下载** | 确定性、离线可跑；词法级表征 |
| **中文推荐档**（可选） | `onnx:Xenova/bge-small-zh-v1.5` | onnxruntime / tokenizers | 首次需模型文件 | 中文语义更好。**仓库名必须带 `Xenova/` 组织前缀**：裸名 `onnx:bge-small-zh-v1.5` 不是合法的 HF 仓库路径（官方与镜像端点均 401），会**静默回退** `onnx_mini_lm_l6_v2`（2026-09-19 实测，见下文「裸名陷阱」） |
| **英文推荐档**（可选） | `onnx:Xenova/bge-small-en-v1.5` | 同上 | 首次需模型文件（126 MB） | 英文语料：公开基准实测 +9.0 pp（见下）。**档位数值已标定（八轮 V6）**：`scripts/calibrate.py --lang en` 2026-09-19 实测正样本 0.728–0.8371（6/6 命中）／负样本 0.4452–0.5705，两分布可分 → `answer_floor` 取中点 **0.649**（数值在 `memory/calibration.py` 的 `TIER_PARAMS`，cliff／restate／semantic_dup 本轮未量、沿用中文神经档值并已在表内注明）。注意两条口径别混用：本表是**合成标定集**的量纲，英文侧的真实效果结论仍以 `docs/benchmark.md` 的检索口径实测为准 |
| 回退档（不推荐） | `onnx_mini_lm_l6_v2` | chromadb 内置 | 首次需下载 | 仅兼容用；中文虚高已知 |

> **选档看语料语言，且必须实测**：英文档在 LoCoMo 全量 1986 题上把证据命中率从
> 36.7%（默认词法档）抬到 **45.7%**；但"更大的模型"未必更好——768 维的
> `bge-base-en-v1.5`（768 维）与 384 维的 `bge-small-en-v1.5` **同批打平**
> （批 B＝`bench_ablation.py` 同脚本同参全量 1986 题，k=8：46.5% vs 45.2%；批 A＝09-17 首轮 k=8：47.8% vs 45.7%），
> gte-small 更差，bge 官方英文查询指令无收益。完整 A/B 表、否定结论与复跑命令见
> `docs/benchmark.md` §"换嵌入档"；口径边界（非官方分）也写在那里。

切换：

```bash
# 方式一：环境变量（不改配置文件）
set HIPPOCAMPUS_EMBEDDING_MODEL=onnx:Xenova/bge-small-zh-v1.5
# 方式二：写进 <数据根>/config.json 的 embedding.model
# 换档后必须重标定（下面的脚本会给出建议值）
python scripts/calibrate.py --model onnx:Xenova/bge-small-zh-v1.5
```

> 名字必须逐字对上 Hugging Face 仓库路径（含 `Xenova/`）。写成裸名
> `onnx:bge-small-zh-v1.5` 不会有报错，只会**换档失败、回退 MiniLM**——见下文「裸名陷阱」。

换档时 `calibration.apply_tier_params()` 会**覆盖**该档的参数（换量纲必须重标定），
同档重复进入则只补缺——人工调过的值不会被启动流程改回去。

## 内置档是什么（`builtin_embedding.py`）

字符 n-gram 带符号哈希（hashing trick）：

1. 文本 → 字符 uni/bi-gram ＋ 拉丁数字词元（中文免分词，语言无关）；
2. 词元用 `blake2b`（**跨进程稳定**，不用 Python 内置 `hash`——它带随机盐）散列到 384 维；
3. 次线性 tf 加权（`1+log tf`）＋ L2 归一化。

它是**词法级**表示，不是神经语义。代价与边界见下一节。

## 标定结果（`scripts/calibrate.py`，合成示例数据）

样本：6 条有真值的查询（正样本）+ 5 条库里无对应记忆的查询（负样本）；
每条查询用**独立会话**（避免会话实体兜底把前几轮实体带进来污染度量）。

内置档实测：

| | min | median | max |
|---|---|---|---|
| 正样本 top1 相似度 | 0.135 | 0.321 | 0.435 |
| 负样本 top1 相似度 | 0.000 | 0.131 | 0.160 |

**结论：两个分布有重叠 → 单一分数不可分**。取值取向是"宁可说不知道"（保精确率）：

| 参数 | 取值 | 依据 |
|---|---|---|
| `absolute_floor`（通道底线） | 0.10 | 让正样本全部进候选（召回优先），误报交给后面的证据闸 |
| `answer_floor`（证据线：低于它不算"有依据"） | 0.17 | 负样本最大值 0.160 + 0.01 → 负样本 0/5 误入，正样本 5/6 命中 |
| `cliff_gap_min` / `cliff_ratio` | 0.06 / 0.25 | 词法分差本身就小，沿用神经档的 0.1/0.3 会永不触发 |
| `restate_threshold` | 0.40 | 重述检测（"你上回提过"），词法档量纲下调 |
| `semantic_dup_threshold` | 0.80 | 去重基准；**语义去重只作用于 preference**（沿用前身边界） |

**已知边界（诚实列出）**：

1. 正样本里"我可以接受出差吗" top1 = 0.135 < 0.17 → 会被判"无依据"（漏答）。
   这是词法档的真实短板：短查询与长记忆的字面重合少。
2. 词法档识别不了同义改写（"我可以接受出差吗" vs "我不投需要长期出差的岗位"）。
3. 换 bge 档后量纲不同，**上面的数字不适用**（该档 `answer_floor` 已于 2026-09-19 在本
   仓库重标定为 **0.465**，见下文「七轮换型实测」；`cliff_gap_min`／`cliff_ratio`／
   `restate_threshold`／`semantic_dup_threshold` 仍沿用前身标定报告口径，本仓库未量出）。

> 边界声明：以上样本是**合成示例数据**（含已知真值），只证"方法可复现"；
> 效果结论只来自真实数据，且单列为运行史，不作普适承诺。

## 七轮换型实测（2026-09-19 · 本机复现，非引用）

栗子 09-19 拍板「换型批推进」，回滚护栏＝真机开臂 <9/10 即不换并如实记负面实测。
以下全部为本机当轮实测（评测批次冻结口径下**零花费**：只跑离线合成 demo 与标定，不调模型端点）。

### 1. 重标定（`scripts/calibrate.py`，合成示例数据）

| | 正样本 min／median／max | 负样本 min／median／max | 可分？ | 建议 `answer_floor` |
|---|---|---|---|---|
| 内置档 `builtin-hash`（在案） | 0.135／0.321／0.435 | 0.000／0.131／0.160 | 否（重叠） | 0.17 |
| **中文档 `onnx:Xenova/bge-small-zh-v1.5`** | **0.5255／0.5745／0.6293** | **0.2568／0.3488／0.4043** | **是** | **0.465** |

- 中文档正负分布**可分**（前身报告没量过这一条），所以按 `calibrate.py` 的口径取中点 0.465；
- 表里旧值 0.55（前身标定报告）高于本轮实测正样本最小值 0.5255，会把一条真命中判成
  "无依据"，已按实测替换（`memory/calibration.py` 有溯源注）。
- 换档生效已核对：`param_snapshots.params.embedding_tier == onnx:Xenova/bge-small-zh-v1.5`
  且 `answer_floor==0.465`（修表键之前是 `builtin-hash`＋0.17，即**整档参数没落**）。

### 2. 真机 demo 回归（判分口径＝CI 同款阈值断言）

```bash
HIPPOCAMPUS_OFFLINE=1 hippocampus --home <fresh> --account t1 demo --questions 10 --memories --json demo.json
```

| 档 | 记忆开 | 记忆关 | 标签一致率 | ≥9/10 闸 |
|---|---|---|---|---|
| 内置档 `builtin-hash`（现档） | **10/10** | 3/10 | 100%（9 题可比） | ✅ 过 |
| 中文档 `onnx:Xenova/bge-small-zh-v1.5` | **8/10**（q02 多条并列偏好、q10 动作题失败） | 3/10 | 83% | ❌ **不过** |

失败分类都是「检索未召回」，与 09-18 记录的 8/10 一致（本轮独立复现同数）。

### 3. 真机变更句实测（`scripts/live_supersede_probe.py`，七轮 T5）

不钉桩、走真实写入通道，按对隔离账户，验三条去路（supersede／admit／挂起确认）
＋"改口后注入取到新值"。7 对同构变更句：

| 档 | 分支分布 | 被吞 | 挂起可裁决且确认后生效 | 结论 |
|---|---|---|---|---|
| 内置档 | supersede 3／pending 2／admit 2 | 0 | 是 | 全过 |
| 中文档 bge-zh | pending 5／admit 2（supersede 0） | 0 | 是 | 全过，但**改口一律要人工确认**（即时取代为 0） |
| 回退档 MiniLM | supersede 3／pending 2／admit 2 | 0 | 否（1 项：确认后该问句注入为**空**） | 有失败项 |

### 4. 裸名陷阱（本轮修掉的真实缺陷）

`docs/embedding.md` 与 `memory/calibration.py` 原先都写裸名 `onnx:bge-small-zh-v1.5`：

- HF 官方与镜像端点都回 **401**（那不是合法仓库路径，合法路径是 `Xenova/bge-small-zh-v1.5`）；
- 加载器随即**静默回退 MiniLM**，标定实测负样本 top1 高达 **0.7485**（正是本项目反复警告的
  "中文相似度整体虚高"），建议 `answer_floor` 被抬到 0.777；
- 后果有两层：①用档的人以为换了神经档，实际在用回退档；②档位表键名对不上真实配置串
  （真串带组织前缀），`apply_tier_params()` 直接查不到表 → **整档参数不生效**。
- 现文档与表键都改成带前缀的合法仓库名，并加守护测试（`tests/test_n29_r7_model_tier.py`）。

### 5. 裁决与口径并陈（重要）

- **裁决：不换型。** 中文档在本机中文合成 demo 上 8/10，**未过 ≥9/10 闸**（护栏条件触发），
  且变更句实测里"即时取代"退化为"一律挂起待确认"。负面实测按纪律如实记录，**不为此调参**。
- **回退目标口径**：任务书护栏写的是"回退 MiniLM"，但 A 级实测——`~/.hippocampus` 根库
  `param_snapshots.embedding_tier == builtin-hash`，即换型前实际生效档是**内置词法档**；
  MiniLM 只是配置串未命中时的**回退档**，且本轮实测 MiniLM 档有失败项（§3）。
  因此"回退"落到 **保持 builtin-hash 不动**，而不是切到更差的 MiniLM。
- **两说并陈**（仓库内／仓库外口径不同，都要认）：交接文档与本仓库 §一 曾并称"线上现 MiniLM"
  （前身 `hippocampus_prototype` 时代的真实现网档，属实）；本仓库 v0.2.x 起默认档已是
  `builtin-hash`（验收 A37 两级默认），MiniLM 降级为回退选项。**同一句"线上"在两套代码库里
  指代不同对象**，引用时必须带仓库名。

### 6. 复跑命令（全部本机可复跑）

```bash
cd D:/AI/Hippocampus
# 重标定（两档对照）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/calibrate.py --json D:/tmp/hc7/calib_builtin.json
HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-small-zh-v1.5" PYTHONIOENCODING=utf-8 \
  .venv/Scripts/python.exe scripts/calibrate.py --model onnx:Xenova/bge-small-zh-v1.5 --json D:/tmp/hc7/calib_bge.json
# demo 阈值断言回归（fresh home）
HIPPOCAMPUS_OFFLINE=1 PYTHONIOENCODING=utf-8 .venv/Scripts/hippocampus.exe --home D:/tmp/hc7/t1-builtin --account t1 demo --questions 10 --memories
HIPPOCAMPUS_OFFLINE=1 HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-small-zh-v1.5" PYTHONIOENCODING=utf-8 \
  .venv/Scripts/hippocampus.exe --home D:/tmp/hc7/t1-bge2 --account t1 demo --questions 10 --memories
# 真机变更句实测（三档）
HIPPOCAMPUS_OFFLINE=1 PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/live_supersede_probe.py --home D:/tmp/hc7/supA --json D:/tmp/hc7/sup_builtin.json
```

## E4/G9 风险登记（2026-09-18 · 未处理、已声明）

**e5 前缀型模型未处理**：e5 系模型官方用法要求文档侧加 `passage: `、查询侧加 `query: ` 前缀；
本项目嵌入函数不实现前缀注入（`query_instruction_for` 只覆盖 bge 的查询指令）。若把
`HIPPOCAMPUS_EMBEDDING_MODEL` 配成 `onnx:Xenova/e5-*`，表征会**静默变差**（无报错）。
处置：**已知限制，不承诺支持 e5**；要支持需在嵌入函数按 repo 加前缀（工作项，未排期）。
