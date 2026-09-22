"""九轮 W6：模型口（代理形态）**真机冒烟**——真起 CLI 子进程、真发 HTTP、桩上游、收尾必杀。

为什么单独要这一层（缺口 K10）：八轮 V5 只把**管理口**（`serve`）的真机冒烟送进了 CI，模型口
（`proxy`）此前只有 in-process 测试（TestClient／线程内 uvicorn）。命令行参数→配置→真实端口→
真实进程这一段没闸：W1 刚补的鉴权接线若只在 `build_app` 上被测，真机上漏传照样全绿。

五条语义各一条真 HTTP 断言（任务书 W6 验收）：

1. `chat_200` —— 三向入站之一：`/v1/chat/completions`（OpenAI Chat）经桩上游回 200；
2. `responses_200`／`messages_200` —— 另两向入站（`/v1/responses`、`/v1/messages`）同样 200；
3. `health_200` —— 环回档 `/health` 免鉴权可探（W1 只收紧非环回，环回体验不变）；
4. `models_200` —— `/v1/models` 回**配置的模型名**（环回免鉴权）；
5. `auth_401` —— 缺令牌打 `/v1/chat/completions` 回 401（与管理口同一条 `_authorized`）；
6. `remote_refused_rc2` —— 真 CLI 用 `--host 0.0.0.0` 不带 `--allow-remote` 时必须**拒起**（rc=2、不占端口）。

三条实现纪律（沿用 V5 的教训）：
- 服务端输出**落文件不接 PIPE**（PIPE 没人读会假性挂起）；
- 随机空闲端口＋独立 `HIPPOCAMPUS_HOME`（不抢 8765、不碰真实库）；
- 收尾必杀＋**psutil 残留进程检查**（起不来的孤儿进程会让后续 CI 步骤莫名红）。

零出站：上游指向**环回 http 的桩服务**（操作员端点通道允许环回 http，见 `docs/security.md` §②），
凭据环境变量全部清掉、只留明确占位串；本脚本不访问任何公网地址。

跑法：`python scripts/live_proxy_smoke.py [--json <路径>]`；退出码 0＝五类语义全命中。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = ["-c", "import sys;from hippocampus.cli import main;raise SystemExit(main())"]

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
    "HIPPOCAMPUS_ALLOW_PLAINTEXT_OUTBOUND",
)

MODEL_NAME = "smoke-stub-model"
STUB_REPLY = '{"entities": [], "memories": [], "reply": "桩上游：已收到（零出站）"}'
PLACEHOLDER_KEY = "placeholder-not-a-real-key-for-loopback-stub"
# 纯死锁兜底（不是功能预算）：真等待靠轮询 /health 就绪
STARTUP_DEADLINE_S = 300.0


class SmokeError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _StubHandler(BaseHTTPRequestHandler):
    """桩上游：任何 POST 都回一条 OpenAI chat 形状的回答（不外发一个字节）。"""

    def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 约定名
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        body = {
            "id": "stub-1",
            "object": "chat.completion",
            "model": payload.get("model") or MODEL_NAME,
            "choices": [
                {
                    "index": 0,
                    # 回**合法 JSON 文本**：模型口的轮末固化会把同一条端点当抽取器用，
                    # 抽取要求 {"entities":[],"memories":[]} 形状（非 JSON 会让 consolidate 抛错）。
                    "message": {"role": "assistant", "content": STUB_REPLY},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16},
        }
        blob = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args: object) -> None:  # 静音（默认会写 stderr）
        return


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _StubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}/v1"


def _env(home: Path, stub_base_url: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_ENV}
    env["HIPPOCAMPUS_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("HIPPOCAMPUS_OFFLINE", None)
    if stub_base_url:
        env["HIPPOCAMPUS_BASE_URL"] = stub_base_url
        env["HIPPOCAMPUS_MODEL"] = MODEL_NAME
        env["HIPPOCAMPUS_API_KEY"] = PLACEHOLDER_KEY
    else:
        env["HIPPOCAMPUS_OFFLINE"] = "1"
    return env


class ProxyProcess:
    """真起 `hippocampus proxy` 子进程；输出落文件；退出时必杀。"""

    def __init__(self, home: Path, stub_base_url: str, *, host: str = "127.0.0.1", extra: list[str] | None = None, allow_remote: bool = False, connect_host: str = "127.0.0.1"):
        self.home = home
        self.port = free_port()
        self.log_path = home / "proxy.log"
        cmd = [sys.executable, *CLI, "proxy", "--host", host, "--port", str(self.port), *(extra or [])]
        if allow_remote:
            cmd.append("--allow-remote")
        self._log = self.log_path.open("w", encoding="utf-8", errors="replace")  # noqa: SIM115  # stop() 里关
        self.process = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(REPO),
            env=_env(home, stub_base_url),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        # 绑定地址 ≠ 连接地址：`--host 0.0.0.0` 是**通配绑定**，不是可路由的目的地址；
        # 客户端连 `http://0.0.0.0:<port>` 在本机被中间层拦成 502、在 CI 上 300s 拿不到 200。
        # 因此探测与请求一律走 connect_host，绑定仍按调用方给的 host。
        self.base_url = f"http://{connect_host}:{self.port}"

    def url(self, path: str) -> str:
        """本冒烟只打本机环回：协议与主机写死为 http://127.0.0.1，只动态化本进程挑的端口。

        固定主机是刻意的——探针不该有能力打到任意主机（SSRF 面），也不出网。
        """
        if not path.startswith("/"):
            raise SmokeError(f"路径必须以 / 开头：{path!r}")
        if not (1024 <= self.port <= 65535):
            raise SmokeError(f"非法端口：{self.port!r}")
        return f"http://127.0.0.1:{self.port}{path}"

    @property
    def token(self) -> str:
        path = self.home / "instance_token"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def wait_ready(self) -> None:
        import httpx

        deadline = time.time() + STARTUP_DEADLINE_S
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise SmokeError(f"代理进程提前退出（rc={self.process.returncode}），日志见 {self.log_path}")
            try:
                # 非环回档 `/health` 同样要令牌（口径见 docs/security.md ⑤ 与 docs/deployment.md §二·五第 2 条）：
                # 无令牌轮询会一路 401，把"起没起来"误判成超时。令牌文件在起服时落盘，逐轮重读即可。
                tok = self.token
                hdrs = {"Authorization": f"Bearer {tok}"} if tok else None
                if httpx.get(f"{self.base_url}/health", headers=hdrs, timeout=2.0).status_code == 200:
                    return
            except Exception:  # noqa: BLE001 —— 还没监听，正常
                pass
            time.sleep(0.2)
        raise SmokeError(f"起代理超时（>{STARTUP_DEADLINE_S:.0f}s 纯死锁兜底），日志见 {self.log_path}")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=15)
        self._log.close()


def _hdr(token: str, account: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "content-type": "application/json",
        "X-Hippocampus-Account": account,
    }


def _json(resp) -> dict:
    """响应不是 JSON 时给出可诊断的壳（真机上 500 的 HTML 页会把 .json() 直接打挂）。"""
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"_non_json": True, "_status": resp.status_code, "_body": resp.text[:400]}


def chat_body(model: str = MODEL_NAME) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "我的目标岗位方向是什么？"}]}


def refuse_non_loopback(home: Path) -> tuple[bool, str]:
    """真 CLI 试一次"非环回不带 --allow-remote"：必须 rc=2 且日志说明原因（不占端口）。"""
    port = free_port()
    log_path = home / "proxy_refuse.log"
    cmd = [sys.executable, *CLI, "proxy", "--host", "0.0.0.0", "--port", str(port)]
    with log_path.open("w", encoding="utf-8", errors="replace") as sink:
        done = subprocess.run(  # noqa: S603
            cmd,
            cwd=str(REPO),
            env=_env(home, None),
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
    text = log_path.read_text(encoding="utf-8", errors="replace")
    refused = done.returncode == 2 and "--allow-remote" in text
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        bound = probe.connect_ex(("127.0.0.1", port)) == 0
    return refused and not bound, text


def residue_count() -> int:
    """本仓解释器下还在跑的 `hippocampus ... proxy` 子进程数（孤儿进程会让后续步骤莫名红）。"""
    try:
        import psutil
    except ImportError:
        return 0
    mine = os.getpid()
    hits = 0
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.info["pid"] == mine:
                continue
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "hippocampus.cli" in cmd and " proxy" in cmd:
                hits += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return hits


def run_smoke(root: Path) -> dict:
    import httpx

    account = "proxysmoke"
    out: dict = {}
    stub, stub_url = start_stub()
    try:
        home = root / "home_proxy"
        home.mkdir(parents=True, exist_ok=True)
        proxy = ProxyProcess(home, stub_url)
        try:
            proxy.wait_ready()
            token = proxy.token
            if not token:
                raise SmokeError(f"实例令牌没生成：{home / 'instance_token'}")
            hdrs = _hdr(token, account)

            chat = httpx.post(f"{proxy.base_url}/v1/chat/completions", headers=hdrs, json=chat_body(), timeout=60)
            chat_json = _json(chat)
            out["chat_200"] = chat.status_code == 200
            out["chat_status"] = chat.status_code
            out["chat_json"] = chat_json
            out["chat_content"] = (((chat_json.get("choices") or [{}])[0]).get("message") or {}).get("content")

            resp = httpx.post(
                f"{proxy.base_url}/v1/responses",
                headers=hdrs,
                json={"model": MODEL_NAME, "input": "我的目标岗位方向是什么？"},
                timeout=60,
            )
            out["responses_200"] = resp.status_code == 200
            out["responses_json"] = _json(resp)

            msg = httpx.post(
                f"{proxy.base_url}/v1/messages",
                headers=hdrs,
                json={"model": MODEL_NAME, "max_tokens": 64, "messages": [{"role": "user", "content": "硬性限制？"}]},
                timeout=60,
            )
            out["messages_200"] = msg.status_code == 200
            out["messages_json"] = _json(msg)

            health = httpx.get(f"{proxy.base_url}/health", timeout=15)
            out["health_200"] = health.status_code == 200
            out["health_fields"] = sorted(health.json().keys())

            models = httpx.get(f"{proxy.base_url}/v1/models", timeout=15)
            out["models_200"] = models.status_code == 200
            ids = [d.get("id") for d in (models.json().get("data") or [])]
            out["models_ids"] = ids
            out["models_configured_name"] = MODEL_NAME in ids

            noauth = httpx.post(f"{proxy.base_url}/v1/chat/completions", json=chat_body(), timeout=15)
            out["auth_401"] = noauth.status_code == 401
            badtok = httpx.post(
                f"{proxy.base_url}/v1/chat/completions",
                headers={"Authorization": "***", "content-type": "application/json"},
                json=chat_body(),
                timeout=15,
            )
            out["auth_401_bad_token"] = badtok.status_code == 401

            # 十轮 X17：非环回**成功路径**——带 --allow-remote 起服后，真机能起来、令牌可用，
            # 且鉴权面按正本收紧（非环回时 `/health`／`/v1/models` 同样要令牌，缺则 401）。
            # 口径正本：`docs/security.md` ⑤「免鉴权探针口（仅环回）」、
            # `docs/deployment.md` §二·五第 2 条、`docs/roadmap.md` 八轮 V8／九轮 W1 两行。
            # 探针一律打 http://127.0.0.1:<本进程挑的随机端口>——协议与主机写死、不出网、不指他机。
            remote_home = root / "home_remote"
            remote_home.mkdir(parents=True, exist_ok=True)
            proxy_remote = ProxyProcess(remote_home, stub_url, host="0.0.0.0", allow_remote=True)
            try:
                proxy_remote.wait_ready()
                token_remote = proxy_remote.token
                if not token_remote:
                    raise SmokeError(f"远程令牌没生成：{remote_home / 'instance_token'}")
                hdrs_remote = _hdr(token_remote, account)
                rp = proxy_remote.port

                # 非环回：/health 缺令牌 → 401（不是 200）
                out["health_remote_401_no_auth"] = httpx.get(f"http://127.0.0.1:{rp}/health", timeout=15).status_code == 401
                # 非环回：/v1/models 缺令牌 → 401
                out["models_remote_401_no_auth"] = httpx.get(f"http://127.0.0.1:{rp}/v1/models", timeout=15).status_code == 401
                # 非环回：带上令牌后两个探针口可用 → 200
                out["health_remote_200_with_auth"] = httpx.get(f"http://127.0.0.1:{rp}/health", headers=hdrs_remote, timeout=15).status_code == 200
                out["models_remote_200_with_auth"] = httpx.get(f"http://127.0.0.1:{rp}/v1/models", headers=hdrs_remote, timeout=15).status_code == 200
                # 聊天口缺令牌 → 401
                out["chat_remote_401_no_auth"] = httpx.post(f"http://127.0.0.1:{rp}/v1/chat/completions", json=chat_body(), timeout=15).status_code == 401
                # 聊天口带令牌 → 200
                out["chat_remote_200_auth"] = httpx.post(f"http://127.0.0.1:{rp}/v1/chat/completions", headers=hdrs_remote, json=chat_body(), timeout=60).status_code == 200
            finally:
                proxy_remote.stop()
        finally:
            proxy.stop()

        refuse_home = root / "home_refuse"
        refuse_home.mkdir(parents=True, exist_ok=True)
        refused, text = refuse_non_loopback(refuse_home)
        out["remote_refused_rc2"] = refused
        out["remote_refused_note"] = text.strip().splitlines()[-1:]
    finally:
        stub.shutdown()
        stub.server_close()
    return out


REQUIRED_KEYS = (
    "chat_200",
    "responses_200",
    "messages_200",
    "health_200",
    "models_200",
    "models_configured_name",
    "auth_401",
    "auth_401_bad_token",
    "remote_refused_rc2",
    # 十轮 X17：非环回成功路径——探针口按正本收紧（缺令牌 401／带令牌 200）＋聊天口两态
    "health_remote_401_no_auth",
    "models_remote_401_no_auth",
    "health_remote_200_with_auth",
    "models_remote_200_with_auth",
    "chat_remote_401_no_auth",
    "chat_remote_200_auth",
)


def evaluate(result: dict) -> list[str]:
    bad = [k for k in REQUIRED_KEYS if not result.get(k)]
    if not str(result.get("chat_content") or "").strip():
        bad.append("chat_content（三向入站要真回内容，不是空壳 200）")
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
    ap = argparse.ArgumentParser(description="模型口（代理形态）真机冒烟：五类语义＋非环回拒起")
    ap.add_argument("--home-root", help="临时数据根（默认系统临时目录下的子目录）")
    ap.add_argument("--json", dest="json_out", help="把结果写到该路径")
    args = ap.parse_args()

    root = Path(args.home_root) if args.home_root else Path(tempfile.mkdtemp(prefix="hc-proxy-smoke-"))
    root.mkdir(parents=True, exist_ok=True)
    started = time.time()
    result = run_smoke(root)
    result["residue_processes"] = residue_count()
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    missing = evaluate(result)
    print(f"模型口真机冒烟用时 {time.time() - started:.1f}s；产物根目录 {root}")
    print("  /health 字段：" + ", ".join(result.get("health_fields") or []))
    print("  /v1/models 回的名字：" + ", ".join(str(i) for i in result.get("models_ids") or []))
    if result["residue_processes"]:
        print(f"  ✗ 残留代理进程 {result['residue_processes']} 个")
    if missing:
        for key in missing:
            print(f"  ✗ 语义未成立：{key}")
        print("\n模型口真机冒烟：红（真进程下没跑通）")
        return 1
    print("✓ 模型口真机冒烟：三向入站／健康口／模型列表／缺令牌 401／非环回拒起 全部命中（零出站、零真凭据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
