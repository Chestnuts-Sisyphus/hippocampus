# GitHub 成果同步纪律（F1/F2 · 2026-09-18 起强制执行）

> 用户 09-18 指令："对于 github 各项内容的同步成果更新甚至重写是非常有必要的"。
> 本文件把"线上不能落后于本地"变成**每次交付的固定动作清单**，照单执行即可。

## 一、每次交付的固定动作（有代码/数字/文档变更就做，全部做完才算交付）

1. **推送 main**：本地分组提交后立即推，不等攒批。
   推送命令（凭据零字面量，走 GIT_ASKPASS 垫片）：
   ```bash
   GIT_ASKPASS='D:/tmp/git-askpass-gh.sh' GIT_TERMINAL_PROMPT=0 git -c credential.helper= push origin main
   ```
   （垫片如被 `D:/tmp` 清理需重建：用户名 `x-access-token`，口令取**本机凭据登记簿**里具备仓库推送权限的那一条；
   **登记簿路径与行号不写入本仓任何文档**，只记在本地治理文档里。）
2. **数字变更时**：同源更新 README 官方分/性能表 ← 结果文档 v（数字正本）＋ `docs/benchmark.md`／
   `docs/roadmap.md`。**数字只在一处定稿（结果文档），其余文件引用并照抄最新值**；改完自查：README 里
   每个数字都能在结果文档找到同值。检查命令：`grep -nE "74\.2|70\.5|32\.55|38\.68|632\.8|41\.98|63\.65" README.md`
   与结果文档逐值核对（**头条分随正本演进**：LongMemEval 现为全量 500 题 **74.2%**，70.5% 是抽 200 的历史行）。
3. **按需 tag + Release**（英文 notes，含两个官方分与口径三件套）：每个 tag 前本地全量
   `pytest --basetemp=D:/tmp/pt` 全绿；Release notes = 做了什么 / 数字（带 CI+口径） / 安全 seal。
   **版本号一致是"六处"**（九轮 W2 归一，此前一处写"四处"一处写"五处"）：
   `pyproject.toml` ／ `hippocampus.__version__`（自 W2 起**改读安装元数据**，不再手写）／
   `importlib.metadata` ／ FastAPI `app.version`（OpenAPI 文档里的版本）／ **`uv.lock` 里的根包版本** ／
   `CHANGELOG.md` 顶部版本段。机器闸：`tests/test_n39_r9_version_single_source.py`
   （六处逐一比对＋"改一处必红"对照，不再靠人肉数处数）。
   `uv.lock` 由 `uv run` 自动同步，改完 `pyproject.toml` 后**必须再 `git status` 看一遍**再打 tag，
   否则 tag 内锁文件仍写旧版本（八轮 V7 就漏过一次，补提交后把 tag 移到新提交）。
4. **description/topics 复核**：仓库名、description、topics 是否与本期成果一致（README 顶部
   对齐 GitTok/Infinigrow 包装时一并检查）。
5. **数字正本可复跑**：结果文档里的每个新数字必须带复跑命令（**数字正本＝仓库外的本地结果文档，
   路径不入本仓**；仓库侧 `docs/benchmark.md` 是评测协议正本，两处分工不混写）。

## 二、同源机制（F2，防两处手写漂移）

- **唯一事实源**：仓库外的本地结果文档（`Hippocampus-基准评测结果-*.md`，数字正本；文件名模式仅内部约定）。README 表、结果文档表、
  会话沉淀三处**必须同值**；任何一处改了数字，另外两处同步改。
- **官方分口径三件套**（CI/判分模型与 temp/输入＝top-8 注入非全文）在 README 与结果文档各带一份，
  引用时互相指认，不另起炉灶。
- 联动检查：每轮交付把 `grep` 到的 README 数字清单附在回写区，防"只改了结果文档忘了 README"。

## 三、按轮次的同步记录（最新在上）

### 九轮（2026-09-19，W1–W15；发版 `v0.5.0`）

- **版本面**：`hippocampus.__version__` 改读安装元数据＋FastAPI `app.version` 同源（W2）→ 六处一致
  由 `tests/test_n39_r9_version_single_source.py` 核对；本文件"五处"口径升为**六处**。
- **对外行为口径**：W1 非环回时 `/health`＋`/v1/models` 要令牌、W3 出站默认仅 https →
  `docs/deployment.md`、`docs/security.md`、`docs/roadmap.md`、README 双语**同日改**（同一批 push 内），
  Release notes 须带"匿名 `/health` 部署受影响"的硬化提醒（W15）。
- **公开面出口**：W5 `--git-text`（提交信息／标签注解／Release 正文）纳入同一闸；
  Release 正文今后写作也要过同一条口径（不含仓外路径与行号指针）。
- 数字闸：W7 把"同一指标多值必须带（批次／规模／臂）标注"升到对照表级别（`tests/test_n42_r9_number_table_annotations.py`）。
- **接口面（W10）**：`/trace` 的 `observe[]` 新增 `run_id` 过滤，**默认仍是账户粒度**（向后兼容，未加版本、未改既有字段语义）；
  字段清单与 `docs/deployment.md` 由真机冒烟双向核对，文档与实现不得各说各话。
- **数据面（W11）**：legacy 老库迁移改由仓内夹具真机验（`tests/test_n46_r9_legacy_migration.py`），
  向量档与词法档**迁移终点必须一致**上闸；导出包层面只有 `transfer.SCHEMA_VERSION` 这一个版本号，
  跨版本导入按"拒绝＋可照着做的文案"处理（库内不加版本列＝不动 accounts schema 的硬边界）。
- **仓外同源面（W12）**：对外简历产物的官方分句与 `docs/benchmark.md` 同源（`74.2%（Wilson 70.2–77.8，n=500）`
  ＋ `v0.4.0` ＋口径三件套＋judge n=70"结论未定"限定）；70.5% 只许作"历史行（n=200 抽样批）"出现。
  注意边界：这条同源**不受**本文件"数字改一处＝全处同日改"约束到"改 v0.4.0 这个测量版本标签"——
  分数系 v0.4.0 下测得，升版到 v0.5.0 **不回改**简历里的版本标注，否则就是把行为变更版冒充为测量版。
- **评测口径（W13）**：难档 `longmemeval_s` 只补**零花费**可测部分（离线规则档，`protocol.model_arm` 记"未跑"作留痕）；
  需付费的两案（第二 judge ≥100、cat5 两候选）**只成表不执行**，已发布分数一字未动。
- **治理面（W14）**：六项挂"待"字的条目全部写终态（做／不做＋理由＋可检验的翻案条件），
  去处见 `docs/chroma-client-sharing.md`、`docs/dependency-audit.md`、`docs/security.md` §③、`docs/naming.md` 末节。
- **发版（W15）**：`v0.5.0`＝`pyproject`／`__version__`／安装元数据／`app.version`／`uv.lock`／CHANGELOG 顶部段
  **六处同日同步**；README 双语测试计数同日跟到 **606**（由收集真值闸逼出，不手写）；
  Release 正文同样要过 `--git-text` 那条脱敏口径（不含仓外路径与行号指针）。

### 五轮（2026-09-18 收口）已同步项

- T6 增量 upsert：README 写入行 632.8 ms → **41.98 ms**（**批 D**＝2026-09-18 五轮 T6 实测，1154 记忆规模，结果文档 §三 同值；
  与 `docs/roadmap.md` §6.1 的**批 C**（09-17，748.6 ms → 42.0 ms）分列，**不得跨批配对**——九轮 W7 口径）。
- T7 ±1 轮邻居：LoCoMo 全量证据命中 45.7% → **63.65%**（`--neighbors`，tokens 1354→1767）；
  官方联动批次结果见结果文档 §二·五（若达决策门，README 表格行同步）。
- T5 LRU：LME 神经档 200 账户峰值常驻内存 **~4GB → 实测值见结果文档 §九 E7 回填**，证据命中仍 ≥98%。