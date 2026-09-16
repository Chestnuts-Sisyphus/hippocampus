# `MemoryCore` v1 接口（冻结）

> 状态：**已冻结（v1）**。冻结日期 2026-09-17。检查脚本：`python scripts/check_interface.py`（CI 门禁）。

## 为什么先冻结接口

前身的教训是：代理形态与 agent 形态各写一套记忆调用，**两套代码分叉**，
同一个"注入"在两处行为不同。本项目的两条形态（代理／Agent）**共用同一个
`MemoryCore`**，所以接口必须先定死再动手——不是文档洁癖，是防分叉的工程手段。

## 三条约定（可执行）

| # | 约定 | 为什么 | 怎么验 |
|---|---|---|---|
| 1 | 所有方法**以 `Scope` 为第一参数** | 记忆天然属于"谁（account）／哪一段对话（session）／哪条来源轨（source）"，缺了它就只能用全局默认库 | `scripts/check_interface.py` ① |
| 2 | 签名与类型里**不出现 HTTP 概念**（request／body／header／messages／status_code／endpoint…） | 记忆语义与传输协议无关；接口一旦掺进 HTTP 字段，Agent 形态就被代理形态的协议绑住 | `scripts/check_interface.py` ② |
| 3 | **v1 只允许追加字段**（新字段必须带默认值；必填字段基线不许改） | 记忆层要被两条形态与第三方复用，破坏性改字段会让调用方静默失效 | `scripts/check_interface.py` ③ |

## 接口清单

```python
class MemoryCore:
    def __init__(*, home=None, scope=None, timeout_s=30.0, vector=True)

    # —— 冻结的五个语义入口 ——
    def write(scope, content, *, kind="fact", source_quote="", entities=None,
              source=None, episode_id=None) -> WriteResult
        # 显式写入（不经模型抽取）。source="model" → shadow=1（永不注入）。
        # 去重：完全重复跳过；语义命中 → supersede／admit；**判据不确定或冲突 → 挂起确认**（绝不丢弃）

    def search(scope, query, *, limit=8, flow="user", include_dropped=True) -> SearchResult
        # 四通道检索；结果里同时给出**被剔候选与理由**（可追责的数据源）

    def inject_finalize(scope, query, *, flow="user", seen_text="", limit=None) -> Injection
        # 装配最终注入文本（stable／fluid 两层）。seen_text=本轮模型已看到的内容全文，
        # 命中的记忆若已在其中则不重复注入（防"重复注入已见内容"）

    def confirm(scope, text) -> ConfirmResult | None
        # 消费 `确认 n` / `否决 n`；非确认指令返回 None。确认胜出的候选会转正为 active

    def consolidate(scope, *, user_text="", assistant_text="") -> TurnResult
        # 一轮对话结束后的固化（抽取→消歧→去重→冲突→挂起）
        # user_text → A 轨；assistant_text → B 轨（shadow=1，永不注入）

    # —— 记忆 CRUD（A9，supersede 语义）——
    def list_memories(scope, *, limit=20, kind=None, status="active",
                      include_shadow=False, order="recent") -> list[MemoryItem]
    def get_memory(scope, memory_id) -> MemoryItem | None
    def update_memory(scope, memory_id, *, content="", reason="") -> WriteResult   # 写新条 + 取代旧条
    def delete_memory(scope, memory_id, *, reason="") -> bool                      # 软删（archived）

    # —— 状态与运维（豁免"scope 第一参数"：它们不指向某份记忆）——
    def pending(scope) -> list[dict]
    def stats(scope) -> dict
    def set_switch(scope, command) -> str | None
    def lock_status(scope) -> dict
    def unlock(scope, *, stale_after_s=300.0) -> bool
    def close()
```

## 类型（`hippocampus.core.types`）

`Scope`（冻结值对象）｜`MemoryItem`｜`Dropped`（被剔候选 + 理由）｜`WriteResult`｜
`SearchResult`｜`Injection`｜`ConfirmResult`｜`TurnResult`

冻结基线（必填字段，不许改）：

- `MemoryItem`：`id` / `kind` / `content`
- `Dropped`：`id` / `reason`

**其余字段一律有默认值**；新增字段也必须带默认值——这样"加字段"不会弄坏任何已有调用方。

## 两条形态怎么用同一个核心

```
        ┌──────────────────────────────┐
        │        MemoryCore (v1)       │   ← 唯一记忆门面（scope 第一参数）
        └───────┬──────────────┬───────┘
                │              │
   代理形态（proxy）      Agent 形态（agent）
   OpenAI 兼容端点       LangGraph think/act/answer
                │              │
        └──── 都只经 hippocampus.core，不 import hippocampus.memory 内部模块 ────┘
```

这条约束有自动化版本：`tests/test_a22_offline_agent.py::test_agent_does_not_touch_memory_internals`
——形态层一旦直接 import 记忆层内部模块，测试就红。

## 变更流程

1. 只许**追加**（新参数必须有默认值；新类型字段必须有默认值）；
2. 改完跑 `python scripts/check_interface.py` 与 `pytest tests/test_memory_core_v1.py`；
3. 若确实要破坏性变更（改名/删字段），必须**升 major 版本**并在 `CHANGELOG.md` 写迁移指引。
