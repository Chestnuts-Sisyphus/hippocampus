"""配置（前身 config.py 的本项目版本）——**已去 DPAPI、已去凭据落盘**。

与前身的关键差异（对应五处改造之二 ＋ 安全硬约束①）：

1. **凭据不落盘**：前身把 api_key 用 Windows DPAPI 加密后写进配置文件（跨平台不可用，
   且"有密文"仍等于"有凭据副本"）。本项目**配置文件中不存在 api_key 字段**，
   运行时只从环境变量或密钥服务读取（见 `resolve_api_key`）。
2. **无平台专有依赖**：不再 import win32crypt / msvcrt。
3. **配置只放非敏感项**：模型端点、模型名、嵌入档、安全开关、端口。

配置文件：`<data_root>/config.json`（可缺省，缺省即用 DEFAULT_CONFIG）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hippocampus.memory import runtime

DEFAULT_PORT = 8765  # T1 命名实测定案（docs/naming.md）；全可配，doctor 打印实际值

# 环境变量名（凭据通道）。按 provider 习惯命名，命中第一个非空即用。
API_KEY_ENV_VARS = (
    "HIPPOCAMPUS_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
)
BASE_URL_ENV_VARS = ("HIPPOCAMPUS_BASE_URL", "OPENAI_BASE_URL")
MODEL_ENV_VARS = ("HIPPOCAMPUS_MODEL", "OPENAI_MODEL")

# 配置里允许出现的键（白名单：防止有人把凭据写进配置后"看起来能用"）
_LLM_ALLOWED_KEYS = {"provider", "base_url", "model", "api_mode", "timeout_s"}
_SECRET_HINTS = ("api_key", "apikey", "token", "secret", "password", "credential")

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "provider": "openai-compatible",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_mode": "chat_completions",
        "timeout_s": 90,
    },
    # 嵌入两级默认（方案 §十八-8 / 验收 A37）：
    #   内置档 builtin-hash：零下载、离线可跑、默认档
    #   推荐档 onnx:bge-small-zh-v1.5：中文质量更好，需本地模型（见 docs/embedding.md）
    "embedding": {"model": "builtin-hash", "enabled": True},
    # 确认块（A39）：由记忆层追加到响应文本，不由模型生成；开关在此
    "proxy": {"port": DEFAULT_PORT, "host": "127.0.0.1", "confirm_block": True},
    "agent": {"max_steps": 8, "model": "", "confirm_block": True},
    "security": {"enabled": True},
    "offline": {"enabled": False},
}


def config_path() -> Path:
    """配置文件位置（数据根由 runtime 解析；数据根本身就是根，不做二次拼接）。"""
    return runtime.data_root() / "config.json"


def _reject_secret_keys(cfg: dict, origin: str) -> None:
    """配置文件里出现疑似凭据键 → 直接拒绝加载（fail loud，不静默忽略）。"""
    for section, body in cfg.items():
        if not isinstance(body, dict):
            continue
        for key in body:
            if any(hint in str(key).lower() for hint in _SECRET_HINTS):
                raise ValueError(
                    f"配置 {origin} 的 [{section}] 节出现疑似凭据字段 {key!r}；"
                    "本项目凭据只从环境变量或密钥服务读取（见 docs/security.md）——请删除该字段。"
                )
        if section == "llm":
            unknown = set(body) - _LLM_ALLOWED_KEYS
            if unknown:
                raise ValueError(f"配置 {origin} 的 [llm] 节出现未知字段 {sorted(unknown)}")


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _fresh_default() -> dict:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def get_config() -> dict:
    """读取配置（缺省=默认值；文件损坏 → 抛错，不静默吞掉用户设置）。"""
    path = config_path()
    if not path.exists():
        return _fresh_default()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件格式错误（顶层应为对象）: {path}")
    _reject_secret_keys(raw, str(path))
    return _deep_merge(_fresh_default(), raw)


def write_config(patch: dict) -> dict:
    """合并写入配置（原子写：.tmp + replace）。凭据类键会被拒绝。"""
    _reject_secret_keys(patch, "写入内容")
    cfg = _deep_merge(get_config(), patch)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return cfg


def resolve_api_key(env: dict | None = None) -> str:
    """**唯一的凭据入口**：环境变量／密钥服务。绝不读配置文件、绝不落盘。

    通道一：环境变量（`HIPPOCAMPUS_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`）。
    通道二：操作系统密钥服务（`keyring` 库，可选依赖；服务名固定 `hippocampus-memory`，
    账户名 `default`）。两条通道都是**进程内 API 读取**：不起子进程、不落副本。
    """
    source = os.environ if env is None else env
    for name in API_KEY_ENV_VARS:
        value = (source.get(name) or "").strip()
        if value:
            return value
    return _read_from_keyring()


def _read_from_keyring() -> str:
    """从系统密钥服务读取（未安装 keyring / 无记录 → 空串，不报错）。"""
    try:
        import keyring
    except ImportError:
        return ""
    try:
        return (keyring.get_password("hippocampus-memory", "default") or "").strip()
    except Exception:
        return ""


def get_llm_config(env: dict | None = None) -> dict:
    """返回 provider/base_url/model/api_key/api_mode。**api_key 只来自环境变量／密钥服务**。"""
    source = os.environ if env is None else env
    llm = dict(get_config().get("llm") or {})
    base_url = next((source[n] for n in BASE_URL_ENV_VARS if source.get(n)), "") or llm.get("base_url", "")
    model = next((source[n] for n in MODEL_ENV_VARS if source.get(n)), "") or llm.get("model", "")
    return {
        "provider": llm.get("provider", "openai-compatible"),
        "base_url": base_url,
        "model": model,
        "api_key": resolve_api_key(source),
        "api_mode": llm.get("api_mode", "chat_completions"),
        "timeout_s": int(llm.get("timeout_s", 90)),
    }


def get_embedding_config() -> dict:
    cfg = get_config().get("embedding") or {}
    return {
        "model": (os.environ.get("HIPPOCAMPUS_EMBEDDING_MODEL") or cfg.get("model") or "builtin-hash").strip(),
        "enabled": bool(cfg.get("enabled", True)),
    }


def get_security_config() -> dict:
    cfg = get_config().get("security") or {}
    return {"enabled": bool(cfg.get("enabled", True))}


def get_proxy_config() -> dict:
    cfg = get_config().get("proxy") or {}
    return {
        "host": str(cfg.get("host") or "127.0.0.1"),
        "port": int(os.environ.get("HIPPOCAMPUS_PORT") or cfg.get("port") or DEFAULT_PORT),
        "confirm_block": bool(cfg.get("confirm_block", True)),
    }


def get_agent_config() -> dict:
    cfg = get_config().get("agent") or {}
    return {
        "max_steps": int(cfg.get("max_steps", 8)),
        "model": str(cfg.get("model") or ""),
        "confirm_block": bool(cfg.get("confirm_block", True)),
    }


def is_offline() -> bool:
    """离线档：env HIPPOCAMPUS_OFFLINE=1 或配置 offline.enabled。"""
    if os.environ.get("HIPPOCAMPUS_OFFLINE", "").strip() in {"1", "true", "yes"}:
        return True
    return bool((get_config().get("offline") or {}).get("enabled", False))


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_PORT",
    "config_path",
    "get_agent_config",
    "get_config",
    "get_embedding_config",
    "get_llm_config",
    "get_proxy_config",
    "get_security_config",
    "is_offline",
    "resolve_api_key",
    "write_config",
]
