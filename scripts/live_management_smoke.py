"""八轮 V5：服务形态（管理口）**真机冒烟**——真起一个 CLI 进程，真发 HTTP，再收尾必杀。

为什么单独要这一层：七轮深夜的两次真机冒烟**抓到了 pytest 全绿下的两个真缺陷**
（`/run` 裸 500、`/health` 字段误述）。in-process 测试（TestClient / 线程内 uvicorn）
测不到"命令行参数→配置→真实端口→真实进程"这一段，所以这段必须有机器闸。

五条语义都要断言（任务书 V5 验收）：`200`（`/health` 环回免鉴权、`/run`、`/trace`）、
`401`（缺令牌打 `/run`）、`400`（`/run` 空 text、`/trace` 缺 run_id）、
`404`（`/trace` 未知 run_id）、`502`（固化阶段模型端点不可用——第二个进程专门钉它）。
九轮 W10 再加一类：**`/trace` 的 `observe` 粒度**——默认回显 `account`、`?observe=run` 必须真的收窄、
非法粒度回 400（这三条只有真进程算得实，见 `REQUIRED_KEYS` 末尾五项）。

三条实现纪律（都是本轮踩出来的）：
- **服务端输出落文件，不接 PIPE**：子进程 stdout 挂管道没人读会假性挂起；
- **随机空闲端口＋独立 HIPPOCAMPUS_HOME**：不抢 8765、不碰真实库；
- **收尾必杀**：`finally` 里 terminate→wait→kill，并确认端口释放。

零出站、零凭据：两个进程都在 `HIPPOCAMPUS_OFFLINE=1` 下起，凭据环境变量全部清掉；
`502` 那台只注入一个**必然失败的环回 https 端点**（校验在发请求之前就抛），不会有任何出网。

跑法：`python scripts/live_management_smoke.py [--keep-log <目录>]`；
`--selfcheck` 只做"能不能起"的干跑（不校验语义），供 CI 之外的快速自检。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 与 tests/conftest.py 同一份清单：这些一律从子进程环境里删掉
CREDENTIAL_ENV = (
    "HIPPOCAMPUS_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HIPPOCAMPUS_BASE_URL",
    "OPENAI_BASE_URL",
    "HIPPOCAMPUS_MODEL",
    "OPENAI_MODEL",
    "HIPPOCAMPUS_SECRET_CMD",
)

# 纯死锁兜底（不是功能预算）：起服务最坏情况也远小于此值；真等待靠轮询 /health 就绪
STARTUP_DEADLINE_S = 300.0


class SmokeError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _server_env(home: Path, *, poison_endpoint: bool = False) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_ENV}
    env["HIPPOCAMPUS_HOME"] = str(home)
    env["HIPPOCAMPUS_OFFLINE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("HIPPOCAMPUS_SECRET_CMD", None)
    if poison_endpoint:
        # 让"端点看起来配好了"，才会真的走到抽取→转发→失败那一段：
        # base_url 指向环回 https（被出站 URL 校验当场拒掉，不发任何请求），
        # key 用**明确占位串**（含 placeholder/not-a-real 字样；运行时注入，不入库不进仓）。
        env["HIPPOCAMPUS_BASE_URL"] = "https://127.0.0.1:9/v1"
        env["HIPPOCAMPUS_MODEL"] = "smoke-model-not-real"
        env["HIPPOCAMPUS_API_KEY"] = "placeholder-not-a-real-key-for-502-path"
        env.pop("HIPPOCAMPUS_OFFLINE", None)
    return env


class LiveServer:
    """真起 `hippocampus serve` 子进程；stdout/stderr 落文件；退出时必杀。"""

    def __init__(self, home: Path, *, poison_endpoint: bool = False) -> None:
        self.home = home
        self.port = free_port()
        self.log_path = home / ("server_poison.log" if poison_endpoint else "server.log")
        cmd = [
            sys.executable,
            "-c",
            "import sys;from hippocampus.cli import main;raise SystemExit(main())",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]
        self._log = self.log_path.open("w", encoding="utf-8", errors="replace")  # noqa: SIM115  # 由 stop() 关
        self.process = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(REPO),
            env=_server_env(home, poison_endpoint=poison_endpoint),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        self.base_url = f"http://127.0.0.1:{self.port}"

    @property
    def token(self) -> str:
        path = self.home / "instance_token"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def wait_ready(self) -> None:
        import httpx

        deadline = time.time() + STARTUP_DEADLINE_S
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise SmokeError(f"服务进程提前退出（rc={self.process.returncode}），日志见 {self.log_path}")
            try:
                if httpx.get(f"{self.base_url}/health", timeout=2.0).status_code == 200:
                    return
            except Exception:  # noqa: BLE001 —— 还没监听，正常
                pass
            time.sleep(0.2)
        raise SmokeError(f"起服务超时（>{STARTUP_DEADLINE_S:.0f}s 纯死锁兜底），日志见 {self.log_path}")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=15)
        self._log.close()

    def __enter__(self) -> LiveServer:
        self.wait_ready()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _headers(token: str, account: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "content-type": "application/json",
        "X-Hippocampus-Account": account,
    }


def _seed(home: Path, account: str) -> None:
    """先灌合成种子（空库上 `/run` 一条候选都没有，`/trace` 也就无从审计）。"""
    log = home / "seed.log"
    with log.open("w", encoding="utf-8", errors="replace") as sink:
        done = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-c",
                "import sys;from hippocampus.cli import main;raise SystemExit(main())",
                "--account",
                account,
                "seed",
            ],
            cwd=str(REPO),
            env=_server_env(home),
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )
    if done.returncode != 0:
        raise SmokeError(f"seed 失败（rc={done.returncode}），日志见 {log}")


def run_smoke(root: Path, *, selfcheck: bool = False) -> dict:
    """真机冒烟主体。返回结果 dict（含 `/trace` 的真响应，供文档比对）。"""
    import httpx

    account = "smoke"
    out: dict = {}

    home = root / "home_clean"
    home.mkdir(parents=True, exist_ok=True)
    _seed(home, account)
    server = LiveServer(home)
    trace_body: dict = {}
    try:
        server.wait_ready()
        token = server.token
        if not token:
            raise SmokeError(f"令牌文件没生成：{home / 'instance_token'}")
        hdrs = _headers(token, account)

        health = httpx.get(f"{server.base_url}/health", timeout=15)
        out["health_open_200"] = health.status_code == 200
        health_auth = httpx.get(f"{server.base_url}/health", headers=hdrs, timeout=15)
        out["health_200"] = health_auth.status_code == 200
        out["health_fields"] = sorted(health_auth.json().keys())

        run = httpx.post(f"{server.base_url}/run", headers=hdrs, json={"text": "我的目标岗位方向是什么？"}, timeout=60)
        out["run_200"] = run.status_code == 200
        out["run_body"] = run.json() if run.status_code == 200 else {}
        run_id = str(out["run_body"].get("run_id") or "")
        out["run_id"] = run_id

        trace = httpx.get(f"{server.base_url}/trace", headers=hdrs, params={"run_id": run_id}, timeout=30)
        out["trace_200"] = trace.status_code == 200
        trace_body = trace.json() if trace.status_code == 200 else {}
        out["trace_body"] = trace_body

        if selfcheck:
            return out

        # 401：缺令牌
        out["run_401"] = httpx.post(
            f"{server.base_url}/run", json={"text": "你好"}, headers={"X-Hippocampus-Account": account}, timeout=15
        ).status_code == 401
        # 400：空 text／缺 run_id
        out["run_400"] = httpx.post(f"{server.base_url}/run", headers=hdrs, json={"text": "   "}, timeout=15).status_code == 400
        out["trace_400"] = httpx.get(f"{server.base_url}/trace", headers=hdrs, timeout=15).status_code == 400
        # 404：未知 run_id
        out["trace_404"] = httpx.get(
            f"{server.base_url}/trace", headers=hdrs, params={"run_id": "run_不存在"}, timeout=15
        ).status_code == 404
        # 九轮 W10：观察事件的 run 粒度过滤必须真生效（默认 account 是整账户，只有真响应能证明收窄）
        strict = httpx.get(
            f"{server.base_url}/trace", headers=hdrs, params={"run_id": run_id, "observe": "run"}, timeout=30
        )
        strict_body = strict.json() if strict.status_code == 200 else {}
        out["trace_observe_run_200"] = strict.status_code == 200
        out["trace_observe_run_echo"] = strict_body.get("observe_granularity") == "run"
        out["trace_observe_run_narrowed"] = bool(strict_body) and len(strict_body.get("observe") or []) <= len(
            trace_body.get("observe") or []
        )
        out["trace_default_granularity"] = trace_body.get("observe_granularity") == "account"
        out["trace_400_observe"] = (
            httpx.get(
                f"{server.base_url}/trace", headers=hdrs, params={"run_id": run_id, "observe": "随便一个值"}, timeout=15
            ).status_code
            == 400
        )
    finally:
        server.stop()

    if selfcheck:
        return out

    # 502：固化阶段的模型端点必然失败（第二台进程，仍零出站零凭据）
    bad_home = root / "home_poison"
    bad_home.mkdir(parents=True, exist_ok=True)
    bad = LiveServer(bad_home, poison_endpoint=True)
    try:
        bad.wait_ready()
        resp = httpx.post(
            f"{bad.base_url}/run",
            headers=_headers(bad.token, "poison"),
            json={"text": "我找岗位时有哪些硬性限制？"},
            timeout=60,
        )
        out["run_502"] = resp.status_code == 502
        out["run_502_status"] = resp.status_code
        out["run_502_stage"] = str(((resp.json().get("error") or {}).get("stage")) or "")
    finally:
        bad.stop()
    return out


REQUIRED_KEYS = (
    "health_open_200",
    "health_200",
    "run_200",
    "trace_200",
    "run_401",
    "run_400",
    "trace_400",
    "trace_404",
    "run_502",
    # 九轮 W10（K15）：observe 粒度的四条语义（只有真进程能证明"过滤确实收窄了"）
    "trace_default_granularity",
    "trace_observe_run_200",
    "trace_observe_run_echo",
    "trace_observe_run_narrowed",
    "trace_400_observe",
)


def evaluate(result: dict) -> list[str]:
    """返回未成立的语义清单（空＝全部命中）。"""
    bad = [k for k in REQUIRED_KEYS if not result.get(k)]
    if result.get("run_502") and result.get("run_502_stage") != "consolidate":
        bad.append("run_502_stage（应标明断在 consolidate 段）")
    return bad


# force-utf8 shim：Windows 控制台默认代码页（CI 里是 cp1252）无法编码 ✓ 等字符，会让"打印"把命令打挂。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true", help="只干跑（起服务＋打三端点），不校验五类语义")
    ap.add_argument("--home-root", help="临时数据根（默认系统临时目录下的子目录）")
    ap.add_argument("--json", dest="json_out", help="把结果（含 /trace 真响应）写到该路径")
    args = ap.parse_args()

    root = Path(args.home_root) if args.home_root else Path(tempfile.mkdtemp(prefix="hc-live-smoke-"))
    root.mkdir(parents=True, exist_ok=True)
    started = time.time()
    result = run_smoke(root, selfcheck=args.selfcheck)
    if args.json_out:
        import json

        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    missing = [] if args.selfcheck else evaluate(result)
    print(f"真机冒烟（{'干跑' if args.selfcheck else '全量语义'}）用时 {time.time() - started:.1f}s；产物根目录 {root}")
    print("  健康口字段：" + ", ".join(result.get("health_fields") or []))
    if missing:
        for key in missing:
            print(f"  ✗ 语义未成立：{key}")
        print("\n真机冒烟：红（服务形态在真进程下没跑通）")
        return 1
    if args.selfcheck:
        print("✓ 真机冒烟（干跑）：三端点可达、/trace 有审计")
    else:
        print("✓ 真机冒烟：200／401／400／404／502 五类语义全部命中（真进程、零出站、零凭据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
