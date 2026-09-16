"""LLM 客户端（前身 llm.py 的本项目版本）——**已去硬编码 key 路径，已去 urllib**。

差异：
1. 凭据只从环境变量／密钥服务读（`config.resolve_api_key`），源码/配置/测试都不出现可用字面量。
2. 走 `httpx`（项目统一 HTTP 客户端），端点 URL 过 `net.validate_endpoint_url`（仅 http/https）。
3. **离线档**：无 key 或 `--offline` 时 `available() == False`，`chat_json` 抛 `LLMUnavailable`，
   调用方走规则降级路径（验收 A36）。

保留前身的两个坑位记录：max_tokens 至少 2000（截断=JSON 作废）；prompt 要求紧凑 JSON。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from hippocampus.memory.config import get_llm_config, is_offline
from hippocampus.net import UnsafeURLError, validate_endpoint_url


class LLMUnavailable(RuntimeError):
    """无可用模型端点（离线档 / 未配置 key / 未装 httpx）。"""


def available(env: dict | None = None) -> bool:
    """是否有可用模型端点：未开离线档 且 有 base_url 且 有凭据。"""
    if is_offline():
        return False
    cfg = get_llm_config(env)
    return bool(cfg["base_url"] and cfg["api_key"])


def _post(messages: list[dict], max_tokens: int, temperature: float, json_mode: bool) -> dict:
    cfg = get_llm_config()
    if is_offline():
        raise LLMUnavailable("离线档（--offline）：不做模型调用，请走规则降级路径")
    if not cfg["base_url"]:
        raise LLMUnavailable("未配置模型端点（HIPPOCAMPUS_BASE_URL 或 config.json 的 llm.base_url）")
    if not cfg["api_key"]:
        raise LLMUnavailable("未提供凭据（HIPPOCAMPUS_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY 或系统密钥服务）")
    try:
        endpoint = validate_endpoint_url(cfg["base_url"].rstrip("/") + "/chat/completions")
    except UnsafeURLError as e:
        raise LLMUnavailable(f"模型端点未通过 URL 校验: {e}") from e
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    with httpx.Client(timeout=cfg["timeout_s"]) as client:
        resp = client.post(endpoint, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    msg = data["choices"][0]["message"]
    return {"content": msg.get("content", ""), "usage": data.get("usage", {})}


def chat(system: str, user: str, max_tokens: int = 2000, temperature: float = 0.2, json_mode: bool = True) -> dict:
    """单轮调用，返回 {'content': str, 'usage': dict}。"""
    return _post(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=json_mode,
    )


def chat_json(system: str, user: str, max_tokens: int = 2000, temperature: float = 0.2) -> dict:
    """调用并强制校验 JSON 可解析，失败抛异常。"""
    result = chat(system, user, max_tokens=max_tokens, temperature=temperature)
    content = result["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM 返回 JSON 解析失败（可能是 max_tokens 截断）: {e}\n内容前200字: {content[:200]}") from e


def chat_messages(
    messages: list[dict], max_tokens: int = 1500, temperature: float = 0.2, json_mode: bool = False
) -> dict:
    """多轮 messages 调用（代理形态转发用）。"""
    return _post(messages, max_tokens=max_tokens, temperature=temperature, json_mode=json_mode)


__all__ = ["LLMUnavailable", "available", "chat", "chat_json", "chat_messages"]
