# 测试与工具里的时间预算台账（八轮 V13 立表，九轮 W9 补全并上闸）

> 为什么要这张表：`e6cb79c` 那批 CI 红点（windows-3.11 单 job）逐行读码后判为**测试假红**——
> `test_locks_kept_when_held_during_eviction` 让持有线程 `release.wait(timeout=5)` 等主线程跑完数次真 I/O，
> 慢 runner 上超时 → 线程自行放锁 → LRU 把"正在使用"的会话逐出 → 断言红。**产品逻辑没错，预算错了**。
> 同类写法当时全仓没有清单，所以本轮逐条过一遍并登记在此。
>
> 判据（四类，处置不同）：
> - **功能性预算**＝"预计 N 秒内该做完了，没做完就算失败" → **禁止存在**，一律改事件驱动
>   （被等的一方置位 `Event`）或改成**纯死锁兜底**（值远大于最坏耗时，本仓统一 300 秒）；
> - **死锁兜底**＝真事件已经由代码保证完成，这个值只在死锁时防挂 → 保留，但必须就地注释说明；
> - **请求超时**＝对端是网络/进程，必须有界 → 保留原值。
> - **运行时参数**（第四类，九轮 W9 补）＝值由命令行/配置决定、静态读不出秒数（如 `scripts/live_supersede_probe.py`
>   的 `time.sleep(args.settle)`）→ 保留，但台账必须**按原式**登记一行，说明"这个秒数由谁决定"。
>   台账闸对这类点不猜数值，只要求原式出现在同一行——否则"读不出"就等于"不看"，闸会有洞。
>
> 明确不做的事：**不加 retry／flaky 插件**（那是把假红换成假绿），**不为了绿删断言**。

## 一、已改（八轮 V13）

| 位置 | 等什么 | 原阈值 | 现处置 |
|---|---|---|---|
| `tests/test_n25_t5_session_lru.py` 持有线程进入锁 | `entered` 事件 | 3 秒（功能性） | 300 秒纯死锁兜底＋注释；`release.wait()` 无超时（主线程 finally 必置位） |
| `tests/test_n25_t5_session_lru.py` teardown 等线程退出 | 线程结束 | 5 秒 | 300 秒死锁兜底＋注释 |
| `tests/test_n15_proxy_tools.py`／`tests/test_proxy_formats.py`／`tests/test_t6_real_server.py`／`tests/test_n31_r7_serve.py` 的 `_Server.__enter__` | uvicorn 真起（轮询 `server.started`） | 20 秒 | 300 秒死锁兜底＋注释（慢 runner 上开 chroma 集合可远超 20 秒） |
| 上述四个文件的 `_Server.__exit__` | 服务线程收尾 | 10 秒 → 30 秒 | 300 秒死锁兜底＋**超时后断言线程已退出**（原判"30 秒纯属兜底"是错的，见下方二次定性） |
| `scripts/live_management_smoke.py` 起 CLI 子进程 | `/health` 开始应答（轮询） | 120 秒 | 300 秒死锁兜底（常量 `STARTUP_DEADLINE_S`，注释已标） |
| `tests/test_n28_r6_engineering.py` 两处等待 | 进锁／teardown | 30 秒／300 秒 | **八轮首版判错了，九轮 W9 改判**：30 秒是功能性预算（不是兜底量级），且 `join` 后无存活断言会重演本节下方"二次定性"那条原生崩溃链条 → 两处已提 300 秒＋`assert not t.is_alive()` |

### 二次定性：`062c3cf` 批 windows-3.11 红点（exit 139 原生崩溃）

首版台账把 `_Server.__exit__` 的 30 秒判成"纯死锁兜底"，**这个判断错了**，同一批 CI 就打了脸：
`full (windows-latest, 3.11)` 单 job 崩在 `Windows fatal exception: access violation`，退出码 139。
崩溃栈两条凑在一起说明得很清楚——

- 服务线程（崩的一方）：`proxy/app.py chat_completions → _handle → core.consolidate → _run_turn_guards → missed_extract.scan_and_fix`
- 主线程（同期在跑）：`tests/conftest.py 的 core 夹具 teardown → MemoryCore.close → memory_bridge.close → chroma 客户端 close`

链条：慢 runner 上一条 in-flight 请求（走完整 consolidate）能跑超 30 秒 → `join(timeout=30)` **超时静默返回** →
测试体结束、`core` 夹具把 chroma 客户端关掉 → 服务线程还在用已释放的原生对象 → use-after-close 崩进程。
所以 30 秒是**功能性预算**（"预计 30 秒内收得尾"），正是本表第一类禁止存在的写法；产品逻辑仍然没错，
错在收尾等待用了个会静默放手的阈值。

处置（四个真服务测试同改）：阈值提到 300 秒量级，并且 join 后 `assert not self.thread.is_alive()`——
真卡住时判成一条可读的红测试，而不是让 teardown 去撞原生崩溃。**不加 retry、不删断言、不给线程"没退也继续"**。

```bash
cd /d/AI/Hippocampus
grep -n "thread.join\|is_alive" tests/test_t6_real_server.py tests/test_proxy_formats.py \
  tests/test_n15_proxy_tools.py tests/test_n31_r7_serve.py
gh run view 35425865217 -R Chestnuts-Sisyphus/hippocampus --log-failed   # 本节的原始栈出处
```

### 一·五、九轮 W9：四处新定性与一处改判

| 位置 | 原写法 | 定性 | 现处置 |
|---|---|---|---|
| `tests/test_n28_r6_engineering.py` 进锁与 teardown 的 30 秒 | `entered.wait(30)`／`t.join(timeout=30)`，join 后无存活断言 | **功能性预算**（八轮首版误判为"兜底量级"） | 提 300 秒死锁兜底＋`assert not t.is_alive()`（不带着活线程去 `core.close()`） |
| `tests/test_n31_r7_serve.py` `/run` 的 `timeout=20` | 单个请求读超时 | **功能性预算**（把"固化全链路该多快"写进了读超时，慢 runner 上必假红） | 提成 `RUN_TIMEOUT_S = 300`（仍是请求超时，只是量级取兜底），并把"审计是否落库"改由 `_wait_audit` 按 `run_id` **轮询完成标记**（`AUDIT_DEADLINE_S = 300`，`AUDIT_POLL_INTERVAL_S = 0.1`） |
| `tests/test_n31_r7_serve.py` `/health` 与 400/401/404 各请求的 `timeout=10` | 字面量 | 请求超时 | 保留原值，提成 `REQUEST_TIMEOUT_S = 10`（即时应答端点，对端是本地服务） |
| `scripts/demo_flow.py` 就绪等待 `for _ in range(40)` ＋ `sleep(0.25)`，"8 秒起不来即报错" | 次数×间隔凑出的功能性预算 | **功能性预算** | 改 `READY_DEADLINE_S = 300` 兜底内的完成标记轮询；顺带修掉"探到非 200 时不 sleep 会热转"（原来只有异常分支才 sleep） |
| `scripts/demo_flow.py` `t.join(timeout=10)` | 超时静默放手 | 死锁兜底（阈值不够）＋缺存活检查 | `THREAD_JOIN_DEADLINE_S = 300`，未退出即抛错 |
| `scripts/live_management_smoke.py` 起服务轮询里的 `timeout=2.0` 与 `sleep(0.2)` | 无定性 | 请求超时／轮询间隔（真等待靠 `/health` 应答，`STARTUP_DEADLINE_S = 300` 只防挂） | 保留原值，就地登记 |
| `scripts/live_proxy_smoke.py`（九轮 W6 新增）同一组值 | 无定性 | 请求超时／轮询间隔／死锁兜底 | 保留原值，就地登记 |

## 二、全量登记（逐文件；闸按这张表逐条核对）

> 一文件一行，行内把该文件**所有**等待值与定性写全。`tests/test_n44_r9_time_budget_ledger.py`
> 扫 `tests/`＋`scripts/` 的等待点后，要求"文件名与该数值出现在同一行"——**缺一个数就红**。

| 文件 | 等待值（秒）与定性 |
|---|---|
| `tests/test_n15_proxy_tools.py` | 0.05（就绪轮询间隔）／15（`httpx.Client` 请求超时）／300（起服务死线、线程 join、子进程收集三处兜底） |
| `tests/test_proxy_formats.py` | 0.05（就绪轮询间隔）／15（`httpx.Client` 请求超时）／300（起服务死线与 join 兜底） |
| `tests/test_t6_real_server.py` | 0.05（就绪轮询间隔）／15（`httpx.Client` 请求超时）／300（起服务死线与 join 兜底） |
| `tests/test_n31_r7_serve.py` | 0.05（就绪轮询间隔）／0.1（审计轮询间隔）／10（即时端点请求超时）／300（起服务死线、join、`/run` 与审计轮询兜底） |
| `tests/test_n25_t5_session_lru.py` | 300（进锁等待与 join 的死锁兜底；`release.wait()` 本身不带超时） |
| `tests/test_n28_r6_engineering.py` | 300（进锁等待与 join 的死锁兜底，九轮 W9 起） |
| `tests/test_n20_security.py` | 1（打桩网络请求的超时，对端是假响应，不涉及真机耗时） |
| `tests/test_n32_r7_docs_drift.py` | 300（收集子进程的死锁兜底） |
| `tests/test_n39_r9_version_single_source.py` | 300（跑 `--version` 子进程的死锁兜底） |
| `tests/test_n41_r9_ci_honesty.py` | 300（跑 pytest 子进程的死锁兜底） |
| `tests/test_n43_r9_doc_reference_hygiene.py` | 60（取 tracked 清单的子进程超时，纯防挂） |
| `tests/test_n49_cli_assertions.py` | 原式 `timeout`＝`run_hippo(args, timeout=30)` 的默认参数决定（每条 CLI 子命令 30 秒请求超时，超时即判红，十轮 X5） |
| `tests/test_n50_learning_enabled.py` | 原式 `timeout`＝`run_hippo(args, timeout=30)` 的默认参数决定（同上口径，十轮 X6） |
| `scripts/bench_multi_account.py` | 0.1（并发轮询间隔）／3（线程 join；不在 CI 断言链上） |
| `scripts/demo_flow.py` | 0.25（就绪轮询间隔）／1（单探针请求超时）／15（代理一轮请求超时）／300（就绪死线与 join 兜底） |
| `scripts/live_management_smoke.py` | 0.2（就绪轮询间隔）／2（单探针请求超时）／15／30／60（各端点请求超时）／180（seed 子进程兜底）／300（起服务死线 `STARTUP_DEADLINE_S`） |
| `scripts/live_proxy_smoke.py` | 0.2（就绪轮询间隔）／2（单探针请求超时）／15（`/health`、`/v1/models`、子进程收尾兜底）／60（三向入站请求超时）／120（起 CLI 子进程收集）／300（起服务死线 `STARTUP_DEADLINE_S`） |
| `scripts/live_supersede_probe.py` | 运行时参数：`time.sleep(args.settle)` 的秒数由 `--settle` 决定（默认 0.8 秒，测量口径参数；改动会换测量条件，故不动；`--selfcheck` 归零） |
| `scripts/scan_public_leak.py` | 60（取 tracked 清单）／120（读 git log 与 `gh release view`）——纯防挂 |
| `scripts/gen_demo_screenshot.py` | 180（无窗口 Edge 截图子进程兜底；正常 2–5 秒）／900（真跑 demo 子进程兜底：含 10 题评测与三形态演示，慢机留足余量，不属断言链）——两者都只是防挂，不表达"应该多快" |

## 三、机器闸（九轮 W9）：台账不再靠人手抄

八轮 V13 只有表、没有闸，所以这张表**天然会腐烂**：新加一个 `time.sleep(5)`、把 300 秒改成 5 秒、
把秒数塞进外部常量——三种情形都不会有任何东西变红。本轮把它反过来对代码校验：

- 闸：`tests/test_n44_r9_time_budget_ledger.py`（进 CI 的全量 pytest，无需单独步骤）；
- 扫描范围：`tests/` 与 `scripts/` 的 `*.py`，字符串与注释先抹掉再扫（否则本文件的示例文字会冒充等待点）；
- 判据：扫描到的每个"文件＋秒数"必须在**同一行**台账里出现；秒数解析不出来（外部常量）时，
  台账必须写明该**原式**及其由谁决定；
- 对照测试两条：植一个新数值的等待点 → 红；把等待值塞进外部常量 → 红；
- 实现中发现并修掉的**假阴性**：抹 token 时若"抹到行尾"，会把同一行 f-string 之后的 `timeout=` 一起抹没
  ——这类洞比漏登记更糟，已在闸里写死"只抹该 token 自身跨度"。

`src/` 不在此闸范围内：产品代码里的等待值是运行时配置（含护栏），属另一类问题，不混进"测试假红"这张表。

## 四、复核方式（可复跑）

```bash
cd /d/AI/Hippocampus
uv run python -m pytest tests/test_n44_r9_time_budget_ledger.py -q --basetemp=D:/tmp/pt  # 台账闸
grep -rn "timeout=\|sleep(\|\.wait(\|join(" tests/ scripts/    # 人工复核：与本表逐条对齐
uv run python -m pytest tests/ -q --basetemp=D:/tmp/pt         # 本地全量
```

CI 侧要求：改动后**连续两批六 job 全绿（含 windows）**，且新出现的红点必须单独定性交账，
不许被后续绿覆盖。
