"""`MemoryCore` 的公开数据类型。

**接口冻结约定（v1，见 docs/memory-core-v1.md）**
- 所有方法以 `Scope` 为**第一参数**；
- 类型里**不出现任何 HTTP 概念**（没有 request／body／header／status_code／messages 这类字段）；
- v1 之后**只允许追加字段**（新增字段必须有默认值），不允许改名字或删字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# 记忆类型（与前身一致）
MemoryKind = Literal["preference", "fact", "resource", "status"]
# 来源轨（来源标签，不是存储分区）
Source = Literal["user", "model", "tool"]


@dataclass(frozen=True)
class Scope:
    """作用域：谁（account）／哪一段对话（session）／哪条来源轨（source）。

    - `account`：存储分区（一个库一个目录）。默认 `default`。
    - `session`：会话标识，用于经历归组与"同一轮已见内容"去重。
    - `source`：来源标签。`user`／`tool` 可进正式记忆；`model` 属观察轨（永不注入）。
    """

    account: str = "default"
    session: str = "default"
    source: Source = "user"

    def with_source(self, source: Source) -> Scope:
        return Scope(account=self.account, session=self.session, source=source)


@dataclass
class MemoryItem:
    """一条记忆的对外视图。"""

    id: str
    kind: str
    content: str
    status: str = "active"
    lifecycle: str = "active"
    shadow: int = 0
    score: float = 0.0
    channel: str = ""
    source_quote: str = ""
    entities: list[str] = field(default_factory=list)
    created_at: int = 0
    last_hit_at: int = 0
    supersedes: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Dropped:
    """被剔除的候选 + 理由（可追责用；A18 的解释数据源）。"""

    id: str
    reason: str
    score: float = 0.0
    stage: str = ""


@dataclass
class WriteResult:
    """一次写入的结果。"""

    ids: list[str] = field(default_factory=list)
    created: int = 0
    superseded: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    skipped: list[Dropped] = field(default_factory=list)
    episode_id: str = ""
    entities: dict[str, str] = field(default_factory=dict)
    note: str = ""


@dataclass
class SearchResult:
    """一次检索的结果（含被剔除候选，供解释）。"""

    items: list[MemoryItem] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    channels: dict[str, int] = field(default_factory=dict)
    flow: str = "user"


@dataclass
class Injection:
    """一次注入装配的结果。"""

    stable_text: str = ""
    fluid_text: str = ""
    items: list[MemoryItem] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    injected_ids: list[str] = field(default_factory=list)
    flow: str = "user"
    enabled: bool = True
    # 注入旁路说明（索引异常告警等；v1 追加字段，默认空串不破坏既有调用方）
    note: str = ""
    # 每次注入调用的语义链标识（A10：explain 按它贯通审计事件，杜绝同 query 多步误匹配）
    run_id: str = ""

    @property
    def text(self) -> str:
        return "\n".join(part for part in (self.stable_text, self.fluid_text) if part)


@dataclass
class ConfirmResult:
    """确认动作的结果。"""

    decision: str = ""          # confirm | veto | none
    text: str = ""              # 面向用户的固定文案
    winner_id: str = ""
    loser_ids: list[str] = field(default_factory=list)
    block_text: str = ""        # 被消费确认块原文（diff 用，A39）


@dataclass
class TurnResult:
    """一轮对话的写入结果（用户轮 + 助手轮）。

    `observed_ids`：本轮**观察轨**入库的 id（模型输出里提取到的资源/状态，`shadow=1`，永不注入）。
    这是"模型输出轨"在生产路径上真的被接上的证据（此前只有随迁测试在调 `extract_response`）。
    """

    write: WriteResult = field(default_factory=WriteResult)
    confirm_block: str = ""
    pending: int = 0
    observed_ids: list[str] = field(default_factory=list)


__all__ = [
    "ConfirmResult",
    "Dropped",
    "Injection",
    "MemoryItem",
    "MemoryKind",
    "Scope",
    "SearchResult",
    "Source",
    "TurnResult",
    "WriteResult",
]
