# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)；
`MemoryCore` 接口自 v1 起**只允许追加字段**（见 `docs/memory-core-v1.md`）。

## [0.5.0] - 2026-09-19（九轮 安全面补齐与门禁扩容）

> **含两处默认对外行为变更**（非环回部署会受影响），故按 SemVer 取 minor。
> 口径三件套（对外引用本版本分数必带）：top-8 记忆注入（非全文）／DeepSeek 判分／LongMemEval-oracle 全量 n=500。
> **版本六处一致**（本段即发版段，六个面见本节末条）：`pyproject`／`__version__`／安装元数据／
> FastAPI `app.version`／`uv.lock` 根包／CHANGELOG 顶部段，由 `tests/test_n39_r9_version_single_source.py` 机器核对。

安全面：

- **W1 模型口（代理形态）鉴权补齐**：`serve()` 与管理口同口径——非环回必须显式 `--allow-remote`
  **且**有实例令牌，否则拒起（退出码 2）；非环回时 `/health` 与 `/v1/models` 也要令牌（新增
  `models_requires_auth` 形参）。环回档行为一字未改。`hippocampus proxy` 新增 `--allow-remote`。
  钉：`tests/test_n38_r9_proxy_auth.py`（接线＋行为＋CLI 透参）；口径见 `docs/deployment.md` §一与 §二·五。
- **W3 出站口径归一**：`validate_outbound_url`（数据/模型提供的 URL）**默认仅 https**，明文公网出口需
  显式 `HIPPOCAMPUS_ALLOW_PLAINTEXT_OUTBOUND=1`（默认关）；本地模型端点走 `validate_endpoint_url`
  （允许环回 http，行为不变）。旧测试里"公网 http 放行"的反向断言已改判（`test_a40_a41_safety.py`）。
  文档／代码／测试三处一致由 `tests/test_n40_r9_outbound_policy.py` 核对。

版本与分发：

- **W2 版本单源**：`hippocampus.__version__` 改读安装元数据（此前长期停在 0.2.1，`--version` 打印旧值）；
  FastAPI `app.version` 同源（此前写死 0.1.0）。新增六处一致闸（`pyproject`／`__version__`／
  `importlib.metadata`／`app.version`／`uv.lock` 根包／CHANGELOG 顶部段）＋"改一处必红"对照，
  纪律文本"四处／五处"归一为"六处"。钉：`tests/test_n39_r9_version_single_source.py`。

真机验证：

- **W6 模型口真机冒烟进 CI**：新增 `scripts/live_proxy_smoke.py`——真起 `hippocampus proxy` 子进程
  （随机空闲端口、独立数据根、服务端输出**落文件不接 PIPE**、上游是**环回 http 桩**、零出站零真凭据），
  断言三向入站（chat／responses／messages）200 且有内容、环回 `/health` 与 `/v1/models` 200、
  缺令牌与错令牌 401、`--host 0.0.0.0` 不带 `--allow-remote` 真进程拒起（rc=2 且端口未占），
  收尾 psutil 查残留。本机实测 **rc=0、五类语义全命中、残留 0**（产物 `D:/tmp/hc9/smoke1/out.json`）。
  **冒烟当场逮到一处既有缺陷（本轮只登记不修，见 `docs/roadmap.md` §二）**：上游回体不是合法 JSON 时，
  模型口的轮末固化抛 `ValueError` → ASGI **裸 500**（`Internal Server Error`，不带 stage 信息）；
  管理口在七轮已兜成 **502＋回带已完成注入段**，两形态此处口径不一致。
  复现：把 `hippocampus proxy` 的上游指向一个回纯文本的端点 → `POST /v1/chat/completions` → 500。

门禁与假绿：

- **W4 假绿清剿**：①CI 用仓内合成夹具 `tests/fixtures/gap_ledger_synthetic.md` 注入
  `HIPPOCAMPUS_GAP_LEDGER`，让正本漂移守护**真跑**（此前每次 CI 都 skip＝绿得没意义）；
  ②`xfail_strict=true`＋`--strict-markers`，随迁三条 xfail 转 strict（xpass 不再算绿）；
  ③core-only job 补最小 pytest 子集＋用例数下界＋"chromadb 必须缺席／降级档有名有姓"显式断言；
  ④删掉三个从未被引用的 marker 声明。钉：`tests/test_n41_r9_ci_honesty.py`（每项带"植入即红"）。
- **W5 公开面扩到三个出口**：`scan_public_leak.py --git-text` 加扫提交信息／标签注解／Release 正文；
  公开仓历史不改写（八轮 V2），历史既有命中记为基线 `GIT_TEXT_BASELINE = 2`（实测出自 `2419926`
  那条 message），**只减不增**——新写一条线索即红。CI 步骤已改用 `--git-text`；
  出口清单见 `docs/security.md` §⑦·五。
- **W7 数字闸扩容到对照表**：规则从"同一指标跨文档必须同值"升级为
  **"多值指标每个值必须带（批次／规模／臂）标注"**，纳入 bge 档对照表与写入"修复前／后"成对基线；
  上闸前先订正三处归属错（`47.8/46.5` 串到 instr 臂、`46.3%` 无出处、`749/632.8` 未配批次），
  全部记在 `docs/benchmark.md` 与 `docs/embedding.md`。钉：`tests/test_n42_r9_number_table_annotations.py`
  （含"植入 `bge-base = 50.0%` 必红"对照）；`roadmap.md` 仍**不参与**头条分比对（旧口径不混写）。
- **W8 文档互斥与引用卫生**：订正 `docs/benchmark.md` 里"双向人工对齐未做"与"人工抽判已做"自相矛盾的
  两段、`docs/release-sync.md` §三 停在五轮、以及三处章节指错（`§2.3` → `§二·三`）；
  `STATUS.md`／`AGENTS.md` 把待拍板正本写成"P1–P5"，**正本实编号是 B1–B10**（P1–P5 是 v0.3.0
  发版任务序，同名不同物）→ 两处就地改名并注明两套编号并存。钉：`tests/test_n43_r9_doc_reference_hygiene.py`
  （仓内 `§` 引用必须命中，跨仓只校验文件存在）。
- **W9 时间预算台账补全＋台账闸**：`docs/ci-time-budgets.md` 从"手写清单"变成**被代码反向校验的表**——
  新增 `tests/test_n44_r9_time_budget_ledger.py` 扫 `tests/`＋`scripts/` 的每个等待点，
  要求"文件名与该秒数出现在台账同一行"，缺行即红（植新等待点、把值塞进外部常量，两类都判红）。
  按判据改判三处功能性预算：`test_n28` 的 30 秒（**八轮首版判成"兜底量级"是错的**，已在
  `docs/ci-time-budgets.md` §一 该行公开更正）、
  `test_n31` `/run` 的 20 秒（改 `RUN_TIMEOUT_S`＋审计按 `run_id` 轮询完成标记）、`demo_flow` 的"8 秒起不来即报错"
  （改 300 秒兜底内的就绪轮询，顺带修掉"探到非 200 不 sleep 会热转"）；两处线程 join 补"超时即判红"存活断言。
- **W10 `/trace` 的 `observe[]` 粒度收口**：八轮 V9 只把"`observe` 其实是**账户级**"这条事实写进文档，
  没有可用收窄手段（三类观察事件都不写 `run_id`，写它们在记忆层）。本轮落地过滤：
  `/trace?observe=account`（默认，行为一字未改）／`observe=run`＝在该轮审计时间戳（拿不到则退回
  `run_id` 前缀的毫秒）前后 50 毫秒内**再收窄**，响应回显 `observe_granularity` 让调用方看得见粒度；
  非法值在边界上直接 400。钉：`tests/test_n45_r9_trace_granularity.py`（手工落"窗口内／一小时前"两条事件，
  证明过滤真的生效）＋真机冒烟五条新语义（`trace_default_granularity`／`trace_observe_run_200`／
  `trace_observe_run_echo`／`trace_observe_run_narrowed`／`trace_400_observe`）。
  **第一版实现被真机冒烟当场判红**：它把 `injection` 事件也放进 run 粒度，结果"收窄"反而比默认更宽 →
  改成"在默认结果之上再筛，只会更少"；口径与近似性（并发同窗口仍会混入）写在 `docs/deployment.md` §二·五。
- **W11 老库迁移真机测试（顺带修掉一个真缺陷）**：新增 `tests/test_n46_r9_legacy_migration.py`，legacy 夹具
  由**现行** `database.SCHEMA` 当场减去"后加列／后建表"派生（不放二进制老库——那玩意儿正本一改就悄悄过期；
  也不落任何本机绝对路径），断言链：缺列缺表 → 一打开就补齐且老行吃到新列默认值 → 二次打开结构一字不差
  （幂等、老行不翻倍、快照不重种）→ 老库落在 `home/accounts/<safe_id>/memory.db` 后经 `MemoryCore` 真读真写。
  **真机测试一上就当场逮到既有缺陷并已修**：`core/backend.py` 的词法档 `lexical_session` 手抄了一份**停在
  `ensure_security_schema` 的过时序列**，漏了七轮求证四列与 `pending_blocks` 表——老库经词法档（无 chromadb
  环境即 CI core-only 的默认档）打开后**一写就 `no column named verification_status`**；改为与 `MemorySession`
  同走 `apply_migrations` 这唯一入口（`database.py` 注释里"以后新增 ensure_* 只改这一处"说的就是这场漂移），
  并加"**两档迁移终点必须一致**"闸防再各抄一份。`transfer` 的跨版本拒绝文案钉死（必须含两个版本号＋指向
  CHANGELOG，且拒绝发生在落盘之前），配"同版本必须放行"对照防闸变常闭。
  **验收口径订正（公开改判）**：任务书写"schema_version 前后断言可见"——库内**没有**版本号字段
  （`PRAGMA user_version` 全程未使用；补它＝动 accounts schema，撞硬边界），故"前后"用**可见的结构差异**断言，
  真正的版本号在导出包 manifest（`transfer.SCHEMA_VERSION = 1`）。
- **W13 评测可信度：零花费部分已实测，付费部分只成表**：难档 `longmemeval_s`（277 MB，每题
  haystack ≈53 会话）用**离线规则档**（`--model-arm` 不开、无凭据、零出站）实测 n=10 抽样：
  证据在文内 100.0%（10/10）／答在文内 60.0%／上下文 tokens 均值 911.6（批＝九轮 W13 零花费批，
  臂＝规则档，嵌入档 bge-small-en-v1.5，邻居关）。**如实标注不可与 oracle 易档并列**（这 10 题
  恰好全是 `single-session-user` 类，题型构成不同）。需付费的官方准确率单列登记为"未跑"，
  报告 JSON 的 `protocol.model_arm` 字段本身就是证据。付费侧只出两张待拍板表：**第二 judge ≥100 题**
  （复用既有 prediction 只换判分模型，费用估算 ¥0.06–0.22、建议 `--budget-yuan 1.0`，硬停 30 不放宽，
  由栗子拍板）与 **cat5 邻居负效应**（A 维持现状承担 −3.36pp／B 按类别裁剪收回 cat5 但整体 −0.8pp，
  两者本轮都不做）。e5 前缀型明确**不做、不排期**，并写死翻案条件。全部见 `docs/benchmark.md` §二·九。
  **零花费、零调参、未改任何已发布分数。**
- **W14 治理逐项终态（六项，不留悬空"待"字）**：①共享 chroma client＝**不做**（六轮否决策复核维持，
  翻案触发条件写成可判定事件）；②安全 seal 正本＝**本仓 `pip-audit` 复扫四件套**，Mimosa 不在交付链里
  （将来若可独立调用则叠加为第二 seal）；③实例令牌**永不过期／无吊销列表＝设计边界**，撤销只有
  "删文件重启"，并给出对外统一措辞模板；④PyPI＝**不发**，三条理由＋真要发的三步清单（版本自证 →
  干净环境安装验证 → 扫描全绿后用 scoped token）；⑤`tests/test_zz_dbg.py` 无答复 → **只加文件头注记、
  文件名与内容不动**，改名时机绑定下次实质修改；⑥八轮"移动 tag"代拍＝**接受**（移动只发生在未发布
  tag、Release 从未指向旧 tag，且 W15 的 v0.5.0 使回补 v0.4.1 成为噪音）。
- **CI 实测后的四处补记（批次 A／B／C 的 CI 红点，逐 job 交账后单独修）**：
  ①`test_n18_release.py` 的"CI 不得依赖常驻代理进程"用**全文文本**匹配 `hippocampus proxy`，
  被 W6 那条**英文注释**撞红（本地全绿、CI 五个 job 全红）→ 判据收窄到"只认 `run:` 命令行"，
  并加对照测试：注释里提不算红、真有一条 `run: hippocampus proxy ...` 必须红。
  ②`test_n41_r9_ci_honesty.py` 的植桩对照用 `len(hits) > GIT_TEXT_BASELINE` 判定，
  而 CI 的 checkout 是浅历史（只有 HEAD）→ 真实基线为 0、植一条只到 1，`1 > 2` 判红：
  这是**把"本地有完整历史"当前提**混进了产品闸。判据改成与历史深度无关的
  "恰好多一条 **且** 命中的来源可指认成植进去的那条"；同时把 `full` job 的 checkout
  改成 `fetch-depth: 0`，让"git 对象"这个出口在 CI 里真的覆盖历史而不只是名义扫一遍。
  ③（批次 B＋C，四个 `full` job 全红）`HIPPOCAMPUS_GAP_LEDGER` 原先挂在"测试"**那一步**的 step env，
  而"守护到底跑没跑"是**另一步**（no-phantom-skip）——它拿不到变量，守护照旧 skip，于是**检查本身**
  按设计把 CI 判红。修法：变量上提到 **job 级 `env:`**（全步骤共用），并把 `test_n41` 的判据收严为
  "必须出现在 `steps:` 之前"——同类"只给一步配了环境"的回归今后在本机就红，不用等 CI 两小时。
  ④（批次 B＋C，`core-only` job 红）同一 job 里的"降级档必须有名有姓"步骤断言 `isinstance(tier, str)`，
  而 `MemoryCore.embedding_tier()` 返回的是 mapping（`model`／`tier`／`needs_download`／`enabled`）——
  **是这条闸写错了，不是产品缺陷**（本机装了 chromadb 所以看不见）。改为断言返回 dict 且 `tier` 字段
  是非空字符串。两处红点均**未被后续绿覆盖**，各自在批次 E 交账。

复跑：

```bash
uv run python -m pytest tests/ -q --basetemp=D:/tmp/pt
uv run ruff check src tests scripts
uv run python scripts/scan_public_leak.py --git-text
```

## [0.4.0] — 2026-09-19（八轮 安全面与发布收口 ＋ 七轮工程完善）

> **版本六处一致**（九轮 W2 归一口径，机器闸 `tests/test_n39_r9_version_single_source.py` 核对）：
> `pyproject.toml` = `hippocampus.__version__`（改读安装元数据）= `importlib.metadata`
> = FastAPI `app.version` = `uv.lock` 根包 = 本段，另有两个发布物 tag `v0.4.0`／GitHub Release `v0.4.0`
> （纪律见 `docs/release-sync.md`；`v0.3.0`→`5f154a5` 不移动）。
> **SemVer 取 minor**：可求证（L1/L2/L3）与服务形态（`/run` `/trace` `/health`）两个新能力已落地一个版本周期，
> 本期以安全面与发布治理收口；`MemoryCore` 接口**仍为 v1**（只追加，无契约变更）。评测批次仍冻结（零新花费）。
> 下一段（七轮工程完善）随本 tag 一并发布，故降为本段子节。

治理与暴露面：

- **V3 公开面泄露闸** `scripts/scan_public_leak.py`：扫 tracked 的 `*.md`／`*.yml`／`*.toml`／**`*.py`**，
  命中"凭据定位线索"形态（登记簿目录与文件名、行号指针、仓外正本目录、本机用户目录与账号名）即退出非零，
  已进 CI。两条实现口径：命中**只报文件行号与形态名、不回显文本**（CI 日志同为公开面）；
  规则由片段运行时拼装，故**扫描器自身与它的测试都在扫描范围内、零豁免**。
  对照测试 `tests/test_n34_public_leak_gate.py`（植入→命中→删掉→零命中；`*.py` 探针单独钉，
  防的正是本轮第一批脱敏"只扫文档漏了 tests/"那个错）。新约定入 `docs/security.md` §⑦：
  **仓外正本路径一律环境变量注入，未配置即 skip**，源码/测试/文档不得硬编码本机绝对路径；
- **V8 健康口在非环回下也鉴权**：`build_app(..., health_requires_auth=True)`，由 `serve_management`
  在绑到非环回（`--allow-remote`）时置真 → 缺令牌 **401**；绑环回时维持 0.1.0 以来的免鉴权。
  两态回归 `tests/test_n35_r8_health_auth.py`；口径同步 `docs/deployment.md` §二·五第 2 条与 `docs/roadmap.md` §一。

防复发与覆盖：

- **V4 数字同源闸** `tests/test_n36_r8_number_hygiene.py`：四份带官方判分口径的文档
  （README 双语／`docs/benchmark.md`／`benchmark.en.md`）里每个指标位置解析出的分值必须落在合法集合内
  （当前值与历史值并允许，冒出第三个口径外的数＝红）；中英两版**结果表行集**逐行比对（防"只改一版"）；
  README 声明的 xfailed 数 == 实打的 `@pytest.mark.xfail` 装饰器数。第一轮实跑就把三处口径不清
  （发布门槛 `≥50%`／`≥15%` 被当实测分、检索率被当官方分、装配与写入延迟混为一谈）逼出来并逐条收紧，
  另加一条"植入矛盾必被拦"的对照测试防闸被改松后静默假绿；
- **V5 真机冒烟进 CI 常驻**：新增 `scripts/live_management_smoke.py` —— 真起 `hippocampus serve` **子进程**
  （随机空闲端口、隔离 home、输出落文件不接 PIPE、收尾必杀），断言 200／401／400／404／502 五类语义，
  零出站零凭据（502 那台用必然被出站 URL 校验拒绝的环回端点触发）。CI 新增两步；
  `live_supersede_probe.py` 与 `bench_cat_attrib.py` 各加 `--selfcheck` 干跑档一并进 CI
  （前者 1 对变更句＋临时 fresh home，后者仓内 fixture＋合成分，不动任何判据与分数）；
- **V9 `/trace` 与 `/health` 返回字段文档化**：`docs/deployment.md` §二·五 新增"返回字段"小节，
  字段以**真机响应**与 `MemoryCore.trace_run()` 实现为准（含一条容易被直觉误解的实现事实：
  `observe[]` 会把该账户所有 verification／confirmation 事件一并带出，不只本轮）；
  并由 `tests/test_n37_r8_live_smoke.py` 把文档表格第一列与真响应字段集合**双向比对**——
  实现加字段而文档没补、或文档写了实现没有的字段，都会红；
- **V13 测试时间预算台账** `docs/ci-time-budgets.md`：`grep` 全量清点 `tests/`＋`scripts/` 的等待点并逐条分类
  （功能性预算＝禁止／死锁兜底＝保留但必须注释／请求超时＝保留）。本轮清掉残留的两处秒级功能预算
  （`entered.wait(3)`、teardown `join(5)`）并把四个 `_Server` 的 20 秒起服务预算提到 300 秒纯兜底；
  **没有**引入 retry 或 flaky 插件，也没有为绿删断言。
- **V13 二次定性（同批 CI 打脸后自纠）**：首版把四个 `_Server` 的收尾 `join(timeout=30)` 判成"纯兜底"是**错的**——
  `062c3cf` 批 `full (windows-latest, 3.11)` 崩在 `access violation`（exit 139）：慢 runner 上一条 in-flight 请求
  跑完整个 `consolidate` 超过 30 秒 → join 超时静默返回 → `core` 夹具先关 chroma 客户端 → 服务线程仍在
  `missed_extract.scan_and_fix` 里用已释放对象。四处同改：阈值 300 秒 ＋ **超时后断言服务线程已退出**
  （真卡住时是一条可读的红测试，而不是原生崩溃）。台账见 `docs/ci-time-budgets.md`「二次定性」节。

标定（V6，英文档缺口消解）：

- `scripts/calibrate.py` 加 `--lang zh|en`：英文档用**主题一一对应的英文合成标定集**（原缺口的成因就是"标定集只有中文，量英文档没意义"）；
  2026-09-19 本机实测 `onnx:Xenova/bge-small-en-v1.5`：正样本 0.728–0.8371（6/6 命中，中位 0.7651）／
  负样本 0.4452–0.5705（中位 0.552）→ **两分布可分**，`answer_floor` 取中点 **0.649**；
  模型经镜像下载＋sha256 校验，**零 API 花费、零评测批次**；
- 档位表补该键（`cliff`／`restate`／`semantic_dup` 本轮**未量**，沿用中文神经档值并在代码注释与
  `docs/embedding.md` 里明写"是沿用不是实测"），`UNCALIBRATED_TIERS` 清空但**机制保留**；
  原先"遍历该集合断言告警"的守护测试因此会空转 → 改成 `monkeypatch` 临时登记假档，
  继续钉住"登记为缺口就必须出声"这条机制（`tests/test_n29_r7_model_tier.py`）；
- **不为提分调参**：0.649 由正负样本中点规则算出，与既有档位同一取法；未改任何判据、未动检索效果结论。

验证与评测可信度（V10／V11）：

- **V10 L3 外站探测首次真机验证**（`scripts/live_l3_probe.py`，人工真机件、不进 CI）：显式开
  `verification_external` 后跑通 **verified／refuted／未取证** 三态＋缓存命中不重复出站（TTL 600 秒），
  并钉住"默认档 `external=False` 时探测发生 **0** 次"。**真机逮到一个真缺陷**：`_probe_url` 用宽泛
  `except Exception → None` 兜底，而 `urlopen` 对 4xx/5xx 抛的是 `HTTPError` → "死链判假"分支**根本不可达**，
  所有 4xx 都被误记成"未取证"。修复后三条语义各配离线回归（`tests/test_n30_r7_verification.py`，桩掉 `urlopen`，
  CI 里零出站）：HTTP 状态码＝取到证可判假；传输层异常＝未取证**绝不判假**；HEAD 被挡（400/401/403/405/406/429/501）
  退回 GET 复核再定性（这条原本只写在 docstring 里没实现）。默认仍为关，实测记录入 `docs/verification-design.md` §五·五；
- **V11 judge 一致性零花费扩样**（`scripts/bench_judge_audit.py`，只读既有批次产物、不调任何端点）：
  ① 判分实现层逐字重推标签 → 该批 **500/500 一致**（两批合计 1000 次判分 0 处分歧）；② 跨批复判**不可用**并写明原因
  （两批全量报告 prediction 相同题数为 0、一批还有 `label` 未判成；顺带钉掉"`bool(label)` 会把 `"False"` 判成对"这个坑）；
  ③ **人工抽判 20 题**（取 judge 判否的前 20 题逐题重判）→ 与 judge 不一致 **2/20＝10.0%**，
  两条都是"judge 偏严、人工可放宽"→ 官方分更可能是保守下界。合并样本 n=70 仍 **< 100**，
  故在 `docs/benchmark.md` 明写**结论未定**、引用 74.2% 必须带这条限定；判分口径与所有分数**一律未动**（付费批次维持冻结）。

### 七轮工程完善（2026-09-19）—— 随 v0.4.0 一并发布

> 本段原为独立的 `[Unreleased]` 段；`v0.3.0`（→`5f154a5`）不移动，其内容随 **v0.4.0** 发布。本轮**评测批次冻结**（零新花费）。

模型层（T1 换型 · 复测后关闭）：

- 按拍板执行换型前置流程：`scripts/calibrate.py` 重标定中文档 → 正样本 0.5255–0.6293／
  负样本 0.2568–0.4043（**两分布可分**，建议 `answer_floor` 0.465，旧表值 0.55 系前身报告、
  高于实测正样本最小值已替换）；
- 真机回归（CI 同口径阈值断言）：中文档 demo 10 题＝记忆开 **8/10**（内置档 10/10），
  **未过 ≥9/10 闸 → 按护栏不换型**，负面实测如实入 `docs/embedding.md` §七轮换型实测；
- **修掉一处会让"换型"整体失效的漂移**：文档与档位表都写裸名 `onnx:bge-small-zh-v1.5`
  （合法仓库名带 `Xenova/` 前缀）→ 下载 401 **静默回退 MiniLM**，且 `apply_tier_params()`
  查不到表键 → **整档参数不生效**。表键改合法仓库名、文档改口径、未标定档显式登记
  `calibration.UNCALIBRATED_TIERS` 并加 stderr 告警，配守护测试
  `tests/test_n29_r7_model_tier.py`。

记忆层（T2 可求证机制，正本 §三-4 施工）：

- 新模块 `memory/verification.py`：L1 存在性·自洽（路径／URL／日历日／百分数）＋
  L2 库内一致性 ＋ L3 外站探测（**默认关**，开了才出站）；三态处置——`verified` 直存留证据、
  `refuted` 用户来源挂起询问不静默丢／模型来源丢弃＋观察日志留痕、`unverifiable` 直存不打可疑；
- `memories` 追加四列（全带默认值，`check_interface.py` 的 v1 只追加契约成立）；
  开关 `verification_enabled`（默认开）／`verification_external`（默认关）；
- 观察轨那条前身幻觉判据收敛到同一入口；
- 附带修掉一处建库漂移：账户库的迁移序列在 `memory_bridge` 里另抄了一份，新增列/参数
  只在一处生效 → 统一为 `database.apply_migrations()` 单一入口。
- 验收锚点 A42–A45：`tests/test_n30_r7_verification.py`。

形态层（T3 服务化 E1）：

- `hippocampus serve` 管理口三端点：`POST /run`（跑一轮注入＋固化，回 `run_id`）／
  `GET /trace?run_id=`（取该次注入的全链路审计＋观察事件）／`GET /health`（补索引健康）；
- 默认只绑 127.0.0.1＋实例令牌，非环回必须 `--allow-remote` 且强制有令牌。
  **出站口径（09-19 真机实测订正）**：管理口**不把这一轮话转发给上游作答**，但 `/run` 的固化阶段
  会按当前配置调用模型端点做记忆抽取（可用即出站、仅 https；配不到走规则档，CI 即靠这条）。
  验收：`tests/test_n31_r7_serve.py`（真 uvicorn）。
- **收口后真机冒烟修掉一处**：机器上存着失效模型凭据时，`/run` 的抽取异常会直穿 ASGI 变**裸 500**
  （调用方看不到断在哪一段）→ 现兜底为 **502** 并回带已完成的注入结果（`run_id`／`injected`／
  `dropped`／`note`），回归测试 `test_run_degrades_to_502_when_consolidation_fails`；
  这条是 pytest 全绿状态下由"真起 uvicorn＋真发 HTTP"才暴露的（测试数 515→**516**）。
  同轮冒烟另测得：`/run`／`/trace` 缺令牌确为 401，但 **`/health` 自 0.1.0 起是免鉴权探针位**
  （不带令牌回 200）→ 文档按"两档"如实改写，**是否收紧登记为待拍板项**（`docs/roadmap.md` §一）。

治理（T5 真机验收）：

- 新增 `scripts/live_supersede_probe.py`：真机变更句实测（不钉桩、按对隔离账户、
  覆盖 supersede／admit／挂起确认三条去路，并断言"改口后注入取到新值"）；
  三档实测对照记入 `docs/embedding.md`。

文档一致性与仓库卫生（T7／T8／T11）：

- README 双语测试数按实测改 **476 → 511**（本轮新增 35 条用例），并加**守护测试**：
  `tests/test_n32_r7_docs_drift.py` 钉住"README 声明数 == `pytest --collect-only` 真值"
  （**双语两处都查**）、双语头牌数字同源、roadmap 旧口径表必须带标注；
- `docs/roadmap.md` §6.1 性能表加**「旧口径：全量 re-sync，修复前」**题注并指向现行
  增量口径（42.0 ms）；§五 那行"增量 upsert 本轮未做"订正为已解决（与 [0.3.0] 一致）；
- **中文 README 三处过期口径订正**（v0.3.0 只改了英文版）：性能数字改成发布口径
  （检索 48 ms／注入 109 ms／写入 42 ms／峰值 ~1.1 GB）、基准表补 `--neighbors` 的
  38.68% 行、"已知限制"里"无管理口／缓存无上界／写入全量同步"三条改为与实现一致；
- 去掉文档里会随提交漂移的硬编码扫描文件数（README／`docs/security.md` 改记"tracked 树零命中"）；
- **数字实时化（收口当日追加，按正本"写简历/答辩用 74.2%（n=500）"口径）**：README 双语、
  `docs/benchmark.md`／`docs/benchmark.en.md`、`docs/roadmap.md` **五处同源**改为
  LongMemEval-oracle 头条 **74.2%（70.2%–77.8%，全量 500，0 失败 0 跳过）**，
  抽 200 的 70.5% 保留为**口径历史行**（两批检索指标 99.6%／40.0% 与 100.0%／48.5% 分列、
  明确"不得跨批混用"）；顺带补上此前 `docs/benchmark.md` **缺失的官方判分结果表**
  （含 LME 分型与 judge 一致性 8.0% 分歧率 → 标注"deepseek-judge 口径"）；
- **自挖两处文档缺陷**：`docs/benchmark.md` 小节序号乱序（「二·七 多账户压测」排在「二·六 批次漂移」之前）
  → 互换为递增；`docs/forms-parity.md` 误称 `/health` 输出 `pending/pending_blocks/suspicious`
  （真机实测其字段只有 ok／offline／port／formats／upstream_endpoint／embedding／stats／index／lock）
  → 改为「CLI `memory pending`／`memory review --suspicious` ＋ `/run` 响应带 `pending`」；
- 管理口鉴权口径裁定（B8）：`/health` 维持**免鉴权探针位**（自 0.1.0 如此、默认环回，
  收紧会动到 `scripts/demo_flow.py` 与三处既有测试的探活用法），文档按"两档"如实写明。
- `.gitignore` 收编 `.qoder/` 与 `AGENTS.md`（纯追加）：本仓为**公开仓**，这两处装的是本机路径、
  凭据位置登记与内部待拍板项，不应有经 `git add -A` 被推出去的路径。
- **清除公开面的本机定位线索**（收口当日追加，两批）：`CHANGELOG.md`／`docs/release-sync.md` 里的
  凭据登记簿路径与行号改写为不定位表述；随后复核发现**第一批漏扫 `tests/`**——
  `tests/test_n23_t3_small_fixes.py` 硬编码了本机个人目录绝对路径，改为环境变量
  `HIPPOCAMPUS_GAP_LEDGER` 注入（未配置即 skip，CI 行为不变、本机配好后仍真判）。
  登记此自纠的理由：该测试守护的是**仓库外正本**的状态漂移，机器闸管不到，故把路径外置而非删测试。
- **cat3 低分归因（§二·八，零新花费）**：只读既有官方判分 JSON 配对分析——
  基线臂 13.91%／邻居臂 12.25%，两臂都不过 83/96，**空作答 0 例**；
  机理＝开放域推断题的短判定词金标 × 词面 token-F1 判据的口径错配，不是记忆层召回失效；
  顺带订正 [0.3.0] 段"cat3 12.25%"未标臂的引用；
- `uv.lock` 入库（可复现安装；实测无个人绝对路径／无凭据字面量，CI 仍走 `pip install -e`，不受影响）。

安全面（T6 六轮 D5 欠账）：

- **Mimosa 正式 seal 本机仍不可跑**（命令面无该工具，它是 ZCode 侧 PreToolUse 钩子），
  如实维持**替代 seal**并重新量一遍：`pip-audit` 联网 OSV 复扫 132 包（命中仍是 `chromadb` 5 条
  ＋ `nltk` 1 条，与 09-18 同批、使用面不可达）＋ tracked 树凭据零命中 ＋ CI 全绿，
  记入 `docs/dependency-audit.md` §六（含"Mimosa 可用时必须复扫替换本节"的待办口径）。

凭据卫生（T9）：

- 本仓之外的一份交接文档（路径与行号不入公开文档）经复核是**扫描器误报**（命中的是普通词
  `task-overlay` 里的子串），没有真凭据可打码 → 修的是判据：`scan_credentials.py` 短密钥形状规则补词首边界 `\b`，
  并补两侧对照测试 `tests/test_n33_r7_scan_credentials.py`（真 key 形态必命中／普通词与占位行不误报／
  整仓 tracked 零命中）；
- 本机 GitHub 凭据登记簿（**存放在本仓之外，路径与行号不写入任何公开文档**）以文末登记表方式逐把补注
  scope 能力面；既有行一字未改，按行号取值的推送垫片不受影响；
- 顺带测得一条硬边界：**在册各把凭据（含 gh 自身 keyring 登录）逐把真删实测，没有一把拿到删除授权**
  （缺 `delete_repo` scope，或该仓不在其授权范围内；对目标仓 `permissions.admin=true` 也**不等于可删**）
  → 删除动作未执行，待 owner 侧补授权或网页端操作；
  T4 **安全闸本身已过且口径拉到最强**：`git fsck --full` 干净、本地 `git ls-tree -r main` 与远端 tree
  **各 77 个 blob、SHA+路径逐字一致（diff 0 行）**、远端无 issues/releases/fork/额外 ref（证据表见沉淀文档附录 C）。



## [0.3.0] — 2026-09-19

第五与第六轮（T2–T14 / G4–G10）的集中发版，共 18 个提交：性能、评测可信度、
工程韧性与发布治理一次收口。**双形态（代理 + Agent）共用一份记忆核心的定位不变，
`MemoryCore` 接口仍为 v1（只追加，本轮无契约变更）**。

性能（本机实测，fresh 库首跑）：

- 单条写入 p50 **632.8 ms → 42.0 ms**（增量索引同步，T6）；
- 会话缓存 LRU 上限可配（T5）：LongMemEval 神经档 200 账户峰值 **~4 GB → ~1.1 GB（−72%）**，
  证据命中仍 100%；
- 四通道检索 p50 48 ms、注入组装 p50 109 ms（README 性能表同源）。

评测（公开基准 + 官方判分口径）：

- LoCoMo-10 全量 1986 官方 F1 **32.55%（CI 30.8–34.4）→ ±1 轮邻居扩展后 38.68%
  （CI 36.8–40.5，+6.13pp，`--neighbors` 显式开）**；口径三件套：deepseek-chat 判分、
  temperature=0、输入＝记忆层 top-8 注入非全文；
- LongMemEval-oracle 判分准确率 **70.5%（Wilson 63.8–76.4，n=200 抽样）**维持既有口径；
- cat5 对抗邻居负效应（51.4→48.0）完成逐题归因：**错位锚定**（邻居轮的具体事实诱导
  对抗题放弃正确拒答），46 题 1→0 / 31 题 0→1，机理注记入 `docs/benchmark.md` §二·四；
- 英文实体抽取 A/B 否定结论（−0.45pp）、四通道消融单通道非瓶颈、多账户常态压测
  （200 账户 100% / 365.8 MB / 45 s）入档；候选集哨兵接入 CI（词法档阈值 25，红测已证）。

工程与韧性：

- `ingest_history` 精确去重（重复导入不再滚大库）、LRU 逐出连带清 `_locks`；
- `scripts/demo_flow.py` 跨会话三形态演示（4 断言）；judge 双判分歧率脚本 `bench_judge_cross.py`；
- CLI 帮助行漂移修复、psutil 进 dev extras＋缺装降级；
- 共享 chroma client 完成一页方案并以**否决策**收口（`docs/chroma-client-sharing.md`）；
- e5 前缀型模型登记为已知限制（`docs/embedding.md` E4/G9）。

文档与治理：

- README 双语测试数订正为 **476（3 xfailed）**；新增 `docs/benchmark.en.md`、
  `docs/deployment.md`、`docs/release-sync.md`（F1/F2 同步纪律）；
- 版本三方不一致（pyproject 0.2.1 / tag v0.2.2 / CHANGELOG）随本段归一为 **0.3.0**。

诚实限制：cat3 开放域（邻居臂 12.25%／基线臂 13.91%，口径见 `docs/benchmark.md` §二·八）与多跳仍是短板；官方分为抽样/单机口径，不作普适承诺；
`bge-small-zh` 中文端到端实测不优于默认档（未过 CI 开 ≥9/10 阈值）；评估数据"优化"暂停纪律继续有效。

## [0.2.2] — 2026-09-18

第四个版本：**官方判分臂**。README 转英文（GitHub 主页对齐 GitTok/Infinigrow 包装方式）；
公开基准的**官方口径**（模型作答 + LLM 判分）落地为显式开关 `bench --model-arm`，
两个基准的官方分已实测（LongMemEval-oracle 抽 200：判分准确率 **70.5%**，
CI 63.8–76.4%；LoCoMo-10 全量 1986：官方 F1 **32.55%**，CI 30.8–34.4%）。
**接口仍为 v1（只追加）**。

### 新增

- **官方判分臂**（`src/hippocampus/eval/model_arm.py`）：模型作答（deepseek-chat, temperature 0）
  基于记忆层注入上下文（top-8，非全文）＋ 官方判分——LongMemEval 官方 judge prompt
  （`xiaowu0162/LongMemEval @9e0b455`）、LoCoMo 官方判分算法（`snap-research/locomo @3eb6f2c`，
  与官方 evaluation.py 逐题对拍一致）；显式 `--model-arm` 才出站，离线默认零变化。
- **调用链护栏**：并发 16、失败重试 2、单调用超时 120 s、按 usage 估算花费累计 **¥30 预算
  硬停**、跑前/跑后各查一次余额（零成本）；每次调用计数 + 估算花费打印。
- **CLI 参数**：`bench --model-arm`、`--budget-yuan`、`--concurrency`、`--retries`、`--timeout-s`；
  修 `bench --online` 未把 `allow_online` 传给运行层的旧 bug。
- **测试**：模型臂 21 条用例（判分保真/护栏/预算硬停/失败如实上报/离线零变化），
  全量 421 passed。

### 资料

- 基准结果正本（数字 + CI + 花费 + 复跑命令）：仓库外的本地结果文档（v2，路径不入本仓）；
  官方判分协议：`docs/benchmark.md` §二·三。

### 已知限制（新增部分，完整清单见 docs/roadmap.md）

- 官方分是**单模型单次测量**（deepseek-chat）；答辩引用必须带"top-8 注入、非全文"脚注。
- LoCoMo 官方 F1 32.55% 与全文基线（GPT-3.5-16k 37.8）同表比时必须带口径注记。

## [0.2.1] — 2026-09-17

第三个版本：**二轮缺口清单 N11–N20**。主题＝"承诺与实现对齐"——把正本新口径落到实现与文档，
把两条稳定性长尾（hnsw 索引读失败、A30-3 间歇失败）修到根上，并给评测补齐公开基准与压测。
**接口仍为 v1（只追加：`TurnResult.observed_ids`、`MemoryCore.ingest_history/active_params/rebuild_index`）。**

### 修复

- **向量索引间歇读失败（`Error creating hnsw segment reader: Nothing found on disk`）**：
  根因＝会话初始化时手工 `DELETE FROM chroma.embeddings_queue`（改内部表，会把"还没被
  compactor 落进段"的写入一起抹掉）＋ 段 reader 建不起来时只重试不修。现改为：只写配置
  （`automatically_purge`，回收交给 chroma）、打开会话**预热探测**、读失败→**从 memory.db
  重建向量池**（实测：重试与再 upsert 都无效，重建有效）、新增 `hippocampus index rebuild`。
  这同时消掉了 `test_a30_3`（约 1/6 概率）的间歇失败——它的失败原文就是语义通道读失败。
- **Agent 形态漏执行用户意图**：检索无命中时直接收口，`记住 X`／`列出偏好`／`写成文件`
  什么都不做还报 `completed`（假完成）；现检索后一律回 think 执行意图动作，检索类答复如实
  报命中条数（空命中按"没依据"走三出口）。
- **观察轨在生产路径上没有调用点**：`extract_response`（模型输出→`shadow=1`）只有随迁测试在调；
  现接进 `consolidate` 的 `assistant_text` 分支（两形态共用），`TurnResult.observed_ids` 留证。
- **神经嵌入档在"多账户"下把进程打爆**：`_resolve_embedding_function` 每次调用都新建一份
  ONNX `InferenceSession`（每份数十 MB 常驻），而 `MemorySession.__init__` 每账户调一次——
  LongMemEval 一题一账户（200 个）跑到中途被 onnxruntime 的 Rust 侧
  `memory allocation of 2097152 bytes failed` 直接杀进程（异常 catch 不到）。
  现按模型名进程级 memo（切换配置走 `invalidate_embedding_cache` 清空），200 个账户只加载一份模型。

### 新增

- **Agent 形态不阉割**（正本 §一）：agent 轮末走 `consolidate`（对话固化＋守卫＋确认块）、
  `think` 轮首消费 `confirm`（`确认 n`／`否决`）；覆盖表 `docs/forms-parity.md`。
- **公开基准评测**：LoCoMo-10 与 LongMemEval 适配层（`hippocampus bench …`），报
  "答案/证据是否进上下文＋tokens＋延迟"，**默认钉离线档**（不发任何出站请求），
  `--online` 才可走模型臂；协议与哈希见 `docs/benchmark.md`。
- **性能压测脚本** `scripts/bench_scale.py`：自建真实运行库同规模合成库
  （1154 记忆／2406 实体／6035 关系／497 事件）测检索/注入/固化/写入/审计与守卫开销。
- **审计/观察文件轮转**：`audit.jsonl`／`observe.jsonl` 到上限滚动（保留份数可配，
  默认 8 MiB×3 份），`explain` 可读滚动份。
- **代理线收口**：带 `tools` 的 chat／anthropic／responses 三向真服务端到端；
  `cache_control_passthrough` 从活跃参数生效；`/v1/models` 回配置的模型名；
  `proxy.session_bucketing`＝day／hour／none 可配。
- **离线档守卫边界**：补实体走**规则抽取**（不再空转），消歧仍软失败跳过；`docs/offline.md` 写死。
- **CI 加两条闸**：形态路径（真服务／流式／鉴权／tools）先断言 proxy extra 在再跑；
  demo 加**阈值断言**（记忆开 ≥9/10、记忆关 ≤6/10）。
- **选档与归因工具**：`scripts/bench_ab.py`（A/B 对照：同一批题多档并跑，每档独立数据根、
  可复用已有导入）、`scripts/bench_ablation.py --reuse-import`（条数消融省掉重复导入）、
  `scripts/bench_diag.py`（失败归因：证据没进上下文时，是"邻居也没进来"还是"整个会话都没进"）。
- **池化按模型官方配置**（`_pooling_for`）：bge 系＝CLS、gte 系＝mean。此前一律 CLS——
  拿 gte 去测会得到"越换越差"的**假结论**（向量打偏，不是模型差）。
- **公开基准实测（神经档补齐）**：LongMemEval-oracle 抽 200 题 **100.0%** 证据命中
  （默认档 98.0%）；LoCoMo-10 全量 **47.8%**（`bge-base-en-v1.5`）／45.7%（`bge-small-en-v1.5`）／
  36.7%（默认词法档）。选档 A/B 表与两条**否定结论**（官方英文查询指令无收益、gte-small 更差）见
  `docs/benchmark.md`。
- **文档**：`docs/benchmark.md`（评测协议）、`docs/forms-parity.md`（两形态覆盖表）、
  `docs/framework-ammo.md`（框架弹药卡）、`docs/verification-design.md`（可求证机制设计稿，待过目）。

### 安全（Mimosa 审计 9 项处置）

- SQL 标识符白名单：`PRAGMA/ALTER` 拼串前校验（表名／列名／DDL 片段）；
  BM25 建索引的表名走白名单（只允许 `memories`／`episodes`）。
- `embedding_models` 下载链：仓库名白名单（`org/name` 两段）＋ 出站 URL 校验
  （只 http/https、**主机必须等于配置端点**）＋ 可注入抓取器（`set_http_fetcher`）。
- `llm_proxy.call_upstream`（随迁备用路径）：出站前过 `validate_endpoint_url`。

## [0.2.0] — 2026-09-17

第二个版本：把"移植了但没接线"的守卫接进生产路径，补齐流式／鉴权／审计／评测四块，
并给仓库打上第一个版本标签。**接口仍为 v1（只追加：`Injection.note`）。**

### 新增

- **轮末与维护守卫接线**：`retrieval_guard`（语义命中但实体未命中 → 轮末补实体、
  只加不删）、`missed_extract`（孤立经历补实体，≤3 次，第 3 次仍空标
  `no_entity_confirmed`）接进 `MemoryCore.consolidate` 轮末；`disambiguate`
  （实体消歧第二层）同路径软失败降级；`hub_guard`（mega-hub 标记）接进维护扫描。
  三类新测试：补实体生效／孤立经历补实体／守卫失败不阻断。
- **索引健康可诊断**：`doctor` 增"索引健康"行（chroma 目录可写性＋集合条数 vs
  库内 active 条数＋索引积压队列＋上次同步错误）；索引同步/检索失败不再静默——
  `inject_finalize` 的 `note` 携带告警（v1 追加字段），chroma 不可写时明确报出。
- **AuditSink 旁路审计**（A18 完整形态）：每次注入旁路记录 **top-N=50 候选全集**
  （doc_id／通道／分数／已注入／被剔理由）到账户目录 `audit.jsonl`，超限只记计数；
  开关 `audit_enabled`（默认开）。`explain` 升级：能答"那条为什么没进"（含超出
  检索 top-k 的候选），并把 `observe.jsonl`（注入/确认事件）与轨迹合并成一份 run 视图。
- **pending TTL（A35）**：未决确认块默认 **7 天** TTL，超时标记"未决冲突"落库、
  旧值保持生效、新候选保持候选态；超时块不再回到 pending 队列。
- **`memory review`**：`--pending`（未决块＋TTL 剩余）／`--candidates`（可疑候选）／
  `--suspicious`（安全标记＋候选＋TTL 未决冲突）三个视图。
- **真·流式转发（A1）**：客户端 `stream:true` 时逐行转发上游 SSE（chat／anthropic／
  responses 三种上游格式各一条测试），每个文本增量一个 delta 事件；**流结束后仍完成
  固化**；确认块作为**末尾 delta** 追加。
- **代理鉴权（A2）**：实例令牌首次启动生成并落盘（`<数据根>/instance_token`），
  请求需带 `Authorization: Bearer <令牌>`，否则 401；`doctor` 只显示前 8 位。
- **上游错误透传（A3）**：上游非 2xx **原样透传状态码与错误体**（不再统一包成 502）。
- **评测升级（C1–C4）**：题集扩到 **20 题**（14 问 6 动作；含 real-jd 真实岗位 JD
  派生场景，**脱敏**）；**pass^k**（`--pass-k`，每题重复 k 次全过才算过）；
  **模型臂**（`--model`，有端点时同一套题走模型作答器）；**关键词基线对照**（`--baseline`，
  仅 BM25 直查库）；报告分列 synthetic／real-jd，边界声明含 k 与模型名。
- **一键演示脚本**：`scripts/demo.sh`＋`scripts/demo.ps1`（起代理 → 灌数据 →
  三格式请求 → 跑评测 → 出结果表；离线可跑、无弹窗）。

### 修复

- **`MemoryCore.close()` 泄漏索引句柄**：此前只关 SQLite 连接、不关向量库客户端，
  长进程（或一条测试套件）跑几百个实例后触发 `OSError: Too many open files`
  （实测拖垮真实服务器测试）。现在连向量库客户端一起关。
- **离线档 responses 入站回执丢正文**：曾只回 `{"model": ...}`；现在按响应适配器
  构造完整回执（含 `output[].content[].text`）。
- **请求体非法 JSON 回 500**：现在明确回 400（含编码问题提示）。

### 变更（口径）

- README 的"可插拔三点（含 MCP）"改为如实表述：**未做 MCP 等第三方工具接入，
  不承诺"含 MCP"**（MCP 接入列为明确不做，见 `docs/roadmap.md` §六）。
- `docs/roadmap.md` 同步本轮完成项与取消项。

### 已知限制

- MCP 工具接入：不做（已从承诺移除）。
- 记忆后端仍写死 SQLite＋Chroma（可替换后端列入后续版本）。
- 评测效果结论只对合成/脱敏样本成立，不作普适承诺。

## [0.1.0] — 2026-09-17

首个可运行版本（alpha）。记忆核心取自既有实现的抽取式移植（机制未简化），
本版本的工作集中在**接口化、去耦合与可验证**。

### 新增

- **`MemoryCore` v1 冻结接口**：`write` / `search` / `inject_finalize` / `confirm` /
  `consolidate`（scope 为第一参数；不含任何 HTTP 概念；只允许追加字段）。
  自动检查：`scripts/check_interface.py`。
- **代理形态：三种入站格式**（默认 `127.0.0.1:8765`）——OpenAI Chat Completions、
  OpenAI Responses、Anthropic Messages，按请求体形状判定，回复按客户端自己那套协议返回；
  上游格式由配置 `llm.api_mode` 决定（chat／anthropic／responses）。确认块由**记忆层生成、
  代理追加**（可关，`确认 n`／`否决` 在入口消费）。不支持的组合如实回 501。
  格式矩阵与已知边界见 `docs/proxy.md`。
- **Agent 形态**：LangGraph 三节点（think / act / answer）＋ LangChain 工具接入；
  三出口（完成／无法完成／需人工升级）；轨迹可复演（`replay` 两次指纹一致）。
- **命令行**：`doctor` / `seed` / `demo` / `proxy` / `chat` / `replay` / `explain` /
  `memory list|pending|candidates|update|delete|off|on|scopes` / `learning off|on`。
- **离线档**：无 key／无网可跑记忆纪律；新增**句式规则抽取**与**规则冲突检测**
  （原先两者都依赖模型，离线会断链）。
- **内置嵌入档 `builtin-hash`**（默认，零下载、确定性）＋ 推荐档 `onnx:bge-small-zh-v1.5`
  可选；新增按档标定表 `memory/calibration.py` 与标定脚本 `scripts/calibrate.py`。
- **单写者锁**（库目录级，含僵尸锁回收）与 `doctor --unlock`。
- **出站 URL 校验**（仅 http/https；拒环回／私有／保留／元数据地址，含 IPv6 与
  十进制／十六进制写法）。
- **评测最小版**：10 题（7 问 + 3 动作）＋ 记忆开/关对照 ＋ 标签双来源一致率 ＋ 边界声明。

### 修复

- **P1（数据丢失级）**：语义去重命中后，机械判据拿不准时会**静默丢弃**新句（用户改口不生效）。
  现在三条路——`supersede`（确认值变更）／`admit`（同构换值，两条都留）／
  **挂起确认**（判据不确定时新条以候选入库，旧值继续生效）——**任何一条都不丢数据**。
- **离线断链**：无模型时"改口 → 挂起确认"整条链曾不可用，现由规则冲突检测承接。
- **生命周期与固化**的"当前时间"参数化（演示可用压缩时间跑同一代码路径）。
- **Windows 控制台编码**：脚本与 CLI 在 stdout/stderr 上强制 UTF-8（`errors="replace"`）。
  此前在 Windows CI 上打印 `✓`／中文会抛 `UnicodeEncodeError`，让检查脚本整条失败。

- **显式写入不再挂起确认**：`write(..., explicit=True)`（用户直接编辑记忆、或显式"记住 X"）
  跳过冲突挂起——用户自己下的决定不需要再问一遍；冲突挂起留给"对话里冒出来的新说法"。
- **对话改口在离线档也能挂起**：`consolidate()` 补规则冲突检测（此前这条链是纯 LLM，无 key 就断）。

### 变更（相对移植来源）

- 去代理形态耦合：记忆语义入口收进 `MemoryCore`，HTTP 形状只留在代理层。
- 去 Windows 专有：删除 DPAPI 加密；凭据只从环境变量／密钥服务读取（**不落盘**）。
- 去个人数据：不再有账号体系；示例数据由 `hippocampus seed` 生成（合成、含已知真值）。
- 数据根与 scope 解析改为显式注入（前身为模块级全局单例，多 scope 并行会串库）。

### 已知限制

- 代理流的转发是**单 delta**（协议正确、无逐字效果）；上游逐行流式转发尚未实现。
- 代理**未做鉴权**（默认只监听 127.0.0.1）；实例令牌尚未移植。

- **并发写不保证**：同一记忆库目录同时只有一个写者（第二个排队或被告知）。
- 内置嵌入档是**词法级**表征，同义改写召回弱于神经档；边界写在 `docs/embedding.md`。
- 评测为最小版（10 题／合成数据），只证方法可复现，不构成效果结论。
- 尚未发布到 PyPI（安装以源码／GitHub 直装为准）。
