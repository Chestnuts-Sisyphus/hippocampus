"""代理形态：OpenAI 兼容端点。

任何兼容客户端把 `base_url` 指到 `http://127.0.0.1:8765/v1` 即获得记忆——
请求前注入、响应后固化、冲突确认块随回复回传。
"""

from hippocampus.proxy.app import CONFIRM_HEADER, build_app, make_upstream, serve

__all__ = ["CONFIRM_HEADER", "build_app", "make_upstream", "serve"]
