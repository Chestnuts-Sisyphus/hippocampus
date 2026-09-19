# 测试与工具里的时间预算台账（八轮 V13）

> 为什么要这张表：`e6cb79c` 那批 CI 红点（windows-3.11 单 job）逐行读码后判为**测试假红**——
> `test_locks_kept_when_held_during_eviction` 让持有线程 `release.wait(timeout=5)` 等主线程跑完数次真 I/O，
> 慢 runner 上超时 → 线程自行放锁 → LRU 把"正在使用"的会话逐出 → 断言红。**产品逻辑没错，预算错了**。
> 同类写法当时全仓没有清单，所以本轮逐条过一遍并登记在此。
>
> 判据（三类，处置不同）：
> - **功能性预算**＝"预计 N 秒内该做完了，没做完就算失败" → **禁止存在**，一律改事件驱动
>   （被等的一方置位 `Event`）或改成**纯死锁兜底**（值远大于最坏耗时，本仓统一 300 秒）；
> - **死锁兜底**＝真事件已经由代码保证完成，这个值只在死锁时防挂 → 保留，但必须就地注释说明；
> - **请求超时**＝对端是网络/进程，必须有界 → 保留原值。
>
> 明确不做的事：**不加 retry／flaky 插件**（那是把假红换成假绿），**不为了绿删断言**。

## 一、已改（本轮）

| 位置 | 等什么 | 原阈值 | 现处置 |
|---|---|---|---|
| `tests/test_n25_t5_session_lru.py` 持有线程进入锁 | `entered` 事件 | 3 秒（功能性） | 300 秒纯死锁兜底＋注释；`release.wait()` 无超时（主线程 finally 必置位） |
| `tests/test_n25_t5_session_lru.py` teardown 等线程退出 | 线程结束 | 5 秒 | 300 秒死锁兜底＋注释 |
| `tests/test_n15_proxy_tools.py`／`tests/test_proxy_formats.py`／`tests/test_t6_real_server.py`／`tests/test_n31_r7_serve.py` 的 `_Server.__enter__` | uvicorn 真起（轮询 `server.started`） | 20 秒 | 300 秒死锁兜底＋注释（慢 runner 上开 chroma 集合可远超 20 秒） |
| 上述四个文件的 `_Server.__exit__` | 服务线程收尾 | 10 秒 → 30 秒 | 300 秒死锁兜底＋**超时后断言线程已退出**（原判"30 秒纯属兜底"是错的，见下方二次定性） |
| `scripts/live_management_smoke.py` 起 CLI 子进程 | `/health` 开始应答（轮询） | 120 秒 | 300 秒死锁兜底（常量 `STARTUP_DEADLINE_S`，注释已标） |
| `tests/test_n28_r6_engineering.py` 两处等待 | 进锁／teardown | 30 秒／300 秒 | 已是兜底量级，本轮只登记不动码（30 秒远大于"进锁"最坏耗时） |

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

## 二、保留原值（分类登记，不是漏看）

| 类别 | 位置 | 说明 |
|---|---|---|
| 请求超时 | 上述 `_Server` 系列测试里的 `httpx.Client(timeout=15)`、`tests/test_n31_r7_serve.py` 各请求（10／20 秒）、`scripts/live_management_smoke.py` 各请求（15／30／60 秒） | 对端是真实 HTTP 服务，必须有界；慢的是网络语义不是断言预算 |
| 子进程兜底 | `tests/test_n32_r7_docs_drift.py` 收集子进程 `timeout=300`；`scripts/live_management_smoke.py` 的 seed `timeout=180`、收尾 `wait(timeout=15)` | 已是兜底量级 |
| 测试内的假网络 | `tests/test_n20_security.py` 三处 `timeout=1` | 打的是桩（伪造响应），不涉及真机耗时 |
| 测量脚本的等待 | `scripts/bench_multi_account.py`（`sleep(0.1)`／`join(timeout=3)`）、`scripts/demo_flow.py`（就绪轮询 `sleep(0.25)`）、`scripts/live_supersede_probe.py` 的 `--settle`（默认 0.8 秒，等向量索引落盘） | 不在 CI 断言链上（`live_supersede_probe --selfcheck` 把 settle 归零）；`--settle` 属**测量口径**参数，改动会换测量条件，故不动 |
| 工具自身 | `scripts/scan_public_leak.py` 取 tracked 清单 `timeout=60` | 纯防挂 |

## 三、复核方式（可复跑）

```bash
cd /d/AI/Hippocampus
grep -rn "timeout=\|sleep(\|\.wait(" tests/ scripts/     # 重新出清单，与本表逐条对齐
uv run python -m pytest tests/ -q --basetemp=D:/tmp/pt   # 本地全量
```

CI 侧要求：改动后**连续两批六 job 全绿（含 windows）**，且新出现的红点必须单独定性交账，
不许被后续绿覆盖。
