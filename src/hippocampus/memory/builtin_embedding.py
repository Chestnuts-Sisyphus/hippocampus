"""内置嵌入档 `builtin-hash`（验收 A37 第一级）——**零下载、零网络、确定性**。

为什么需要它：前身默认用 chromadb 的 `ONNXMiniLM_L6_V2`，首次使用会**联网下载**
模型（且中文相似度虚高，标定报告已证）。项目要满足"无网/无 key 也能跑"（A36／A38），
所以默认档必须是纯本地、可离线、确定性的嵌入。

做法：**字符 n-gram 带符号哈希（hashing trick）**
  1. 文本 → 字符 uni/bi-gram ＋ 拉丁数字词元（中文免分词，语言无关）
  2. 词元用 `blake2b`（跨进程稳定，不用 Python 内置 hash——它带随机盐）
     散列到 D 维，签名位决定加减
  3. 次线性 tf 加权（1+log tf）＋ L2 归一化

它是**词法级**表示（不是神经语义），因此：中文近义改写召回弱于 bge 档；
代价是零依赖、零下载、可复现——这正是 CI 与离线档需要的那一级。
质量档由 `onnx:bge-small-zh-v1.5` 提供（见 docs/embedding.md）。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

DIM = 384  # 与前身 MiniLM 同维（便于对比与迁移）
MODEL_NAME = "builtin-hash"
_NAME_RE = re.compile(r"[0-9A-Za-z_]+")


def _tokens(text: str) -> list[str]:
    """字符 uni/bi-gram ＋ 拉丁数字词元。"""
    s = (text or "").strip().lower()
    if not s:
        return []
    out: list[str] = []
    out.extend(_NAME_RE.findall(s))
    chars = [c for c in s if not c.isspace()]
    out.extend(chars)
    out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return out


def _bucket(token: str) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % DIM, 1.0 if (value >> 63) & 1 else -1.0


def embed_text(text: str) -> list[float]:
    """单条文本 → 归一化向量（空文本返回全零）。"""
    tokens = _tokens(text)
    if not tokens:
        return [0.0] * DIM
    counts: dict[int, float] = {}
    for token in tokens:
        idx, sign = _bucket(token)
        counts[idx] = counts.get(idx, 0.0) + sign
    vec = [0.0] * DIM
    for idx, raw in counts.items():
        weight = math.copysign(1.0 + math.log1p(abs(raw)), raw)
        vec[idx] = weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class BuiltinHashEmbeddingFunction:
    """chromadb 兼容的内置档嵌入函数（无外部依赖）。"""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim

    # chromadb 0.5+/1.x 的持久化协议：name()/build_from_config/get_config
    @staticmethod
    def name() -> str:
        return MODEL_NAME

    def get_config(self) -> dict[str, Any]:
        return {"dim": self.dim}

    @classmethod
    def build_from_config(cls, config: dict[str, Any]) -> BuiltinHashEmbeddingFunction:
        return cls(dim=int(config.get("dim", DIM)))

    def __call__(self, input: Any) -> list[list[float]]:
        docs = [input] if isinstance(input, str) else list(input)
        return [embed_text(str(text)) for text in docs]

    # chromadb 1.x 契约：`embed_query(input=<str|list>) -> Embeddings`，返回**二维**
    # （它按 `embeddings[0]` 取查询向量，返回一维会炸 "float is not a Sequence"）。
    def embed_query(self, input: Any = None, text: str | None = None) -> list[list[float]]:
        value = input if input is not None else text
        return self.__call__(value if value is not None else "")

    def embed_documents(self, input: Any) -> list[list[float]]:
        return self.__call__(input)

    @staticmethod
    def is_legacy() -> bool:
        return False
