# 由前身 hippocampus_prototype/llm_proxy.py 抽取移植（vendoring），仅做包内 import 改写。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 透明代理 -- llm_proxy.py
上游调用层：按 api_mode 构造 URL，非流式/流式两种调用，字节级逐行转发。

- call_upstream: 非流式 httpx.post；非 2xx 原样返回上游错误体（(status_code, body)）；连接失败抛 httpx.HTTPError
- stream_upstream: httpx.stream POST；async generator 逐行 yield 原始行（含空行，保留换行）；超时 120s
  - 非 2xx 抛 _UpstreamError(status_code, body_text)；连接/读取异常向上抛，由调用方转 error 事件
"""

import httpx


class UpstreamError(Exception):
    """上游非 2xx 响应（流式路径专用）。带状态码与响应体。"""

    def __init__(self, status_code: int, body_text: str):
        super().__init__(f"上游返回 {status_code}: {body_text[:300]}")
        self.status_code = status_code
        self.body_text = body_text


def build_upstream_url(llm_cfg: dict, endpoint: str) -> str:
    """按 api_mode 构造上游 URL。

    endpoint: "chat" -> base_url + /chat/completions
              "messages" -> base_url + /v1/messages
              "responses" -> base_url + /responses
    """
    base = llm_cfg["base_url"].rstrip("/")
    if endpoint == "chat":
        return base + "/chat/completions"
    if endpoint == "messages":
        return base + "/v1/messages"
    if endpoint == "responses":
        return base + "/responses"
    raise ValueError(f"未知端点类型: {endpoint}")


def build_headers(llm_cfg: dict) -> dict:
    """构造上游请求头：按 api_mode 分流。
    anthropic_messages -> x-api-key + anthropic-version；其余 -> Authorization Bearer。
    """
    if llm_cfg.get("api_mode") == "anthropic_messages":
        return {
            "x-api-key": llm_cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    return {
        "Authorization": f"Bearer {llm_cfg['api_key']}",
        "Content-Type": "application/json",
    }


def call_upstream(llm_cfg: dict, url: str, payload: dict, headers: dict, timeout: float = 120.0):
    """非流式 POST。

    返回 (status_code, body)。非 2xx 原样返回上游错误体（由调用方决定转发策略）。
    连接失败/超时抛 httpx.HTTPError。
    """
    resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    return resp.status_code, body


def stream_upstream(llm_cfg: dict, url: str, payload: dict, headers: dict, timeout: float = 120.0):
    """流式 POST。返回 async generator，逐行 yield 原始行（含空行与换行，字节级保留）。

    非 2xx 抛 UpstreamError；连接/读取异常原样向上抛。
    """

    async def _gen():
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code // 100 != 2:
                    text = (await resp.aread()).decode("utf-8", errors="replace")
                    raise UpstreamError(resp.status_code, text)
                async for line in resp.aiter_lines():
                    yield line + "\n"

    return _gen()
