"""应用级设置门面（形态层用）。

形态层（代理 / Agent）需要读配置（端口、端点、嵌入档、离线开关），但**不该直接
import 记忆层内部模块**——记忆层的实现细节属于 `hippocampus.core` 背后。本模块
把"形态层需要的设置"整理成一个稳定入口，同时把凭据规则挡在里面（只暴露"有没有"，
不暴露"是什么"）。

配置本身仍然只有一处实现（`hippocampus.memory.config`），这里只做转发。
"""

from __future__ import annotations

from typing import Any

from hippocampus.memory import config as _cfg

DEFAULT_PORT = _cfg.DEFAULT_PORT


def proxy_config() -> dict[str, Any]:
    """代理形态配置：host / port / confirm_block。"""
    return _cfg.get_proxy_config()


def agent_config() -> dict[str, Any]:
    """Agent 形态配置：max_steps / model / confirm_block。"""
    return _cfg.get_agent_config()


def embedding_config() -> dict[str, Any]:
    return _cfg.get_embedding_config()


def is_offline() -> bool:
    return _cfg.is_offline()


def endpoint_ready() -> bool:
    """是否配好了模型端点（有端点 + 有凭据）。**不返回凭据本身**。"""
    cfg = _cfg.get_llm_config()
    return bool(cfg["base_url"] and cfg["api_key"])


def endpoint_model() -> str:
    return _cfg.get_llm_config()["model"]


def llm_available() -> bool:
    """离线档下恒为 False；供策略层判断"能不能用模型决策"。"""
    from hippocampus.memory import llm

    return llm.available()


def tier_params(model: str) -> dict[str, Any]:
    """某嵌入档的标定参数（策略层用它取证据线）。"""
    from hippocampus.memory import calibration

    return calibration.params_for_tier(model)


def llm_chat_json(system: str, user: str, **kwargs: Any) -> dict[str, Any]:
    """结构化模型调用（只有配了端点与凭据才可用）。"""
    from hippocampus.memory import llm

    return llm.chat_json(system, user, **kwargs)


def llm_chat_messages(messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """多轮模型调用（代理转发用）。凭据与端点校验都在记忆层内部完成。"""
    from hippocampus.memory import llm

    return llm.chat_messages(messages, **kwargs)


def llm_post_json(payload: dict[str, Any], *, endpoint: str, timeout_s: float | None = None) -> tuple[int, dict]:
    """把构造好的请求体原样转发到上游（代理形态的三端点转发通道）。

    凭据不出记忆层：本函数只转发，调用方拿不到也不需要拿 key。
    """
    from hippocampus.memory import llm

    return llm.post_json(payload, endpoint=endpoint, timeout_s=timeout_s)


def upstream_endpoint() -> str:
    """配置里的上游端点类型：chat / anthropic / responses。"""
    from hippocampus.memory import llm

    return llm.endpoint_mode()


def validate_scope_id(account_id: str | None) -> str:
    """校验 scope 标识（目录穿越防护）。形态层的入口用它挡外部输入。"""
    from hippocampus.memory.account import safe_account_id

    return safe_account_id(account_id)


__all__ = [
    "DEFAULT_PORT",
    "agent_config",
    "llm_chat_json",
    "llm_chat_messages",
    "llm_post_json",
    "upstream_endpoint",
    "tier_params",
    "embedding_config",
    "endpoint_model",
    "endpoint_ready",
    "is_offline",
    "llm_available",
    "proxy_config",
    "validate_scope_id",
]
