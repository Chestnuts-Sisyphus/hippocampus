"""跨会话长任务一键演示（六轮 G7/C4）：三形态走通 + 跨会话记忆生效断言。

用法：
    python scripts/demo_flow.py                # 默认数据根 D:/tmp/hc-demo-flow，端口 8765
    python scripts/demo_flow.py --home D:/tmp/xxx --port 8787

三形态（同一账户 "demo-flow"，不同会话名——记忆跨会话生效的证明）：
    1. 记忆核心：MemoryCore 直接写入/检索/注入（离线、确定性）
    2. Agent 形态：run_task 离线规则策略作答（零模型、确定性）——同一账户另一会话
    3. 代理形态：起 127.0.0.1:<port> 服务，三格式各发一发（离线回执会列出注入的记忆）
       （fastapi/uvicorn 未装时该形态降级为"跳过+提示"，其余两形态照跑）

关键断言（全部过 → 退出码 0）：
    A1 跨会话注入：会话 chat-2 能注入到会话 chat-1 写入的事实（记忆属于账户，不属于会话）
    A2 Agent 跨会话作答：agent 会话的回答复述 chat-1 的事实
    A3 代理跨会话注入：代理会话的离线回执列出 chat-1 的事实
    A4 代理服务健康：/health 200

纪律：离线（HIPPOCAMPUS_OFFLINE=1）、零出站、零凭据、写 D:/tmp 默认、不弹窗。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("HIPPOCAMPUS_OFFLINE", "1")
for _k in ("HIPPOCAMPUS_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

ACCOUNT = "demo-flow"
FACT = "我的期望城市是杭州"

# 三个等待值的定性（九轮 W9，逐条登记在 docs/ci-time-budgets.md）：
# 就绪轮询真靠「/health 开始应答」这个事件，兜底值只在 uvicorn 永不启动时防挂；
# 旧写法是「40 次 × 0.25 秒 = 8 秒起不来就报错」——功能性预算，慢机上会假失败。
READY_DEADLINE_S = 300.0
READY_POLL_INTERVAL_S = 0.25
HEALTH_PROBE_TIMEOUT_S = 1
# 请求超时（对端是本地 HTTP 服务，必须有界；值本身不是断言对象）
CHAT_REQUEST_TIMEOUT_S = 15
# 收尾线程 join 的死锁兜底：should_exit 已置位，正常路径 <1 秒；超时即判失败（见下方 is_alive 断言）
THREAD_JOIN_DEADLINE_S = 300.0


def step(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def main(argv: list[str] | None = None) -> int:
    # Windows 默认 GBK 控制台打不出 ✓/✗ 会 UnicodeEncodeError（六轮 G7 交付缺陷，09-19 补）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="跨会话长任务一键演示（三形态 + 断言）")
    ap.add_argument("--home", default="D:/tmp/hc-demo-flow", help="数据根（默认 D:/tmp/hc-demo-flow）")
    ap.add_argument("--port", type=int, default=8765, help="代理形态端口（默认 8765）")
    args = ap.parse_args(argv)

    import shutil

    home = Path(args.home)
    if home.exists():
        shutil.rmtree(home)  # 每次演示从 fresh home 出发（测量纪律）

    from hippocampus.agent.runner import run_task
    from hippocampus.core import MemoryCore, Scope

    results: list[bool] = []
    core = MemoryCore(home=home)
    try:
        print("== Hippocampus 跨会话长任务演示（三形态）==")
        print(f"数据根: {home}   账户: {ACCOUNT}   端口: {args.port}")

        # ---------- 形态 1：记忆核心（写入在会话 chat-1） ----------
        print("\n-- [1/3] 记忆核心形态：会话 chat-1 写入事实")
        s1 = Scope(account=ACCOUNT, session="chat-1", source="user")
        core.write(s1, FACT, kind="fact", source_quote="用户原话")

        # ---------- 断言 A1：跨会话注入 ----------
        s2 = Scope(account=ACCOUNT, session="chat-2", source="user")
        injection = core.inject_finalize(s2, "我的期望城市是哪里？")
        hit = any(FACT in item.content for item in injection.items)
        results.append(step("A1 跨会话注入（chat-2 注入到 chat-1 的事实）", hit,
                            f"注入 {len(injection.items)} 条"))

        # ---------- 形态 2：Agent 形态（离线规则策略） ----------
        print("\n-- [2/3] Agent 形态：run_task（离线规则策略，零模型）")
        agent_scope = Scope(account=ACCOUNT, session="agent-1", source="user")
        run = run_task(core, agent_scope, "我的期望城市是哪里？", offline=True, max_steps=4)
        print(f"   exit={run.exit}  steps={run.steps}  answer={run.answer[:120]!r}")
        results.append(step("A2 Agent 跨会话作答（复述 chat-1 的事实）",
                            run.exit == "completed" and FACT in run.answer))

        # ---------- 形态 3：代理形态（本进程起服务） ----------
        print(f"\n-- [3/3] 代理形态：127.0.0.1:{args.port}（离线回执列出注入记忆）")
        try:
            import httpx
            import uvicorn

            from hippocampus.proxy.app import build_app
            from hippocampus.settings import instance_token
        except ImportError as e:  # proxy extras 未装：降级跳过，其余形态不受影响
            print(f"   代理形态跳过（缺依赖：{e}；pip install 'hippocampus-agent[proxy]' 后可跑）")
            results.append(step("A3/A4 代理形态", False, "缺依赖，跳过"))
        else:
            token = instance_token(create=True)
            app = build_app(core, confirm_block=True, offline=True, upstream=None,
                            auth_token=token or None)

            config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
            server = uvicorn.Server(config)
            t = threading.Thread(target=server.run, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{args.port}"
                # 代理按头解析 scope：account 必须显式给（缺省=default，会查空库）；
                # session 由代理按日分桶（与 chat-1 不同 → 仍是跨会话证明）。
                headers = {
                    "content-type": "application/json",
                    "X-Hippocampus-Account": ACCOUNT,
                }
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                ready_deadline = time.monotonic() + READY_DEADLINE_S
                while True:
                    if time.monotonic() >= ready_deadline:
                        raise RuntimeError(f"代理服务在 {READY_DEADLINE_S:.0f} 秒死锁兜底内未就绪")
                    try:
                        if httpx.get(f"{base}/health", timeout=HEALTH_PROBE_TIMEOUT_S).status_code == 200:
                            break
                    except Exception:  # noqa: BLE001 —— 还没开始监听，属正常轮询区间
                        pass
                    time.sleep(READY_POLL_INTERVAL_S)
                body = {"model": "demo", "messages": [{"role": "user", "content": "我的期望城市是哪里？"}]}
                resp = httpx.post(
                    f"{base}/v1/chat/completions", json=body, headers=headers, timeout=CHAT_REQUEST_TIMEOUT_S
                )
                text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                results.append(step("A3 代理跨会话注入（离线回执列出 chat-1 的事实）",
                                    resp.status_code == 200 and FACT in text,
                                    f"http {resp.status_code}"))
                results.append(step("A4 代理服务健康（/health 200）", True, base + "/health"))
            finally:
                server.should_exit = True
                t.join(timeout=THREAD_JOIN_DEADLINE_S)
                if t.is_alive():
                    raise RuntimeError(f"代理服务线程未在 {THREAD_JOIN_DEADLINE_S:.0f} 秒内退出（收尾死锁兜底）")

    finally:
        core.close()

    print("\n== 汇总 ==")
    ok = all(results)
    for i, r in enumerate(results, 1):
        print(f"  {'✓' if r else '✗'} A{i}")
    print("结论：跨会话记忆生效 ——" + ("全部断言通过，演示成功。" if ok else "存在失败断言，见上。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
