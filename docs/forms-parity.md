# 两形态覆盖表：记忆层的每一项能力，代理形态 / Agent 形态各怎么落

> 依据：设计正本 §一「Hippocampus 的全部设计与功能**不得在 agent 里被阉割或重构**」。
> 这张表是**核对工具**：哪一项在哪一形态没接，一眼看得出来。测试钉在
> `tests/test_n13_agent_parity.py`（入口一致性用计数器抓）。
> 更新纪律：记忆层新增机制时**先补本表**，再补两边的接线与测试。

## 一、共享的入口（两形态都只经 `MemoryCore`）

| 记忆层能力 | 入口（`MemoryCore`） | 代理形态落点 | Agent 形态落点 |
|---|---|---|---|
| 分层注入（stable／fluid＋去重） | `inject_finalize` | `proxy/app.py::_handle` 请求前注入（按入站格式落位） | `agent/graph.py::think` → `_memory_lines()` |
| 四通道检索＋被剔理由 | `search` / `inject_finalize` | 注入链内部调用 | `think` 注入 ＋ `search_memory` 工具 |
| 确认轨（preference／fact 提取→冲突→确认块） | `consolidate` → `fire_track_a` | 响应后固化（`_handle`） | **轮末固化**（`answer` 节点） |
| 非确认轨（status／resource 自动入库） | `consolidate` → `after_response` | 同上 | 同上 |
| **观察轨（模型输出→`shadow=1`，永不注入）** | `consolidate` → `extract_response` | 同上（`assistant_text` 分支） | 同上；另加"本轮结论"显式写入（`write(source="model")`） |
| 轮末守卫（漏抽补实体／消歧） | `consolidate` → `_run_turn_guards` | 同上 | 同上 |
| 确认消费（`确认 n`／`否决`） | `confirm` | `_handle` 请求入口消费 | **`think` 轮首消费**（`step==1`） |
| 显式写入／改写／删除 | `write` / `update_memory` / `delete_memory` | 记忆 CRUD 走 CLI（代理不暴露写接口） | `remember`／`list_memories` 工具 |
| 待确认视图／候选／可疑 | `pending` / `pending_blocks` / `suspicious` | `/health` 与 CLI | `RunResult.pending` 与 CLI |
| 生命周期／离线固化／维护扫描 | 会话维护路径（`maybe_run_maintenance`） | 注入路径低频触发 | 同左（同一会话对象） |
| 索引健康与自愈 | `index_health` / `rebuild_index` | `doctor`／`hippocampus index rebuild` | 同左 |
| 审计与 explain | `audit.jsonl`（旁路）＋ `explain` | `explain --run`（读轨迹＋审计） | 同左（轨迹含每步注入与被剔） |

## 二、形态特有的部分（不是"阉割"，是职责不同）

| 项 | 代理形态 | Agent 形态 |
|---|---|---|
| 上游模型调用 | 转发客户端请求（三种入站格式 × 三种上游格式） | 自带编排（`think`／`act`／`answer`） |
| 输出协议 | 按客户端那一套回（chat／anthropic／responses） | 直接给任务答案 |
| 工具面 | 不做动作（危险动作归 Agent） | 内置 5 工具（写文件／列目录／记／查／搜岗位）＋确认闸 |
| 出口 | 回模型文本＋确认块 | 三出口（完成／无法完成／需人工升级） |
| 确定性复演 | 不适用 | `replay`（同一图同一策略重跑，指纹可比） |

## 三、本轮核对发现并修掉的（2026-09-17 二轮）

| # | 问题 | 后果 | 处置 |
|---|---|---|---|
| 1 | Agent 形态**不调用 `consolidate`** | 对话固化／轮末守卫／确认块在 Agent 形态全不走（正本"不阉割"条款） | `answer` 节点轮末调用 `consolidate`；`RunResult.pending` 取自 `turn.pending` |
| 2 | Agent 形态**不消费 `confirm`** | 用户说「确认 1」无人处理，确认轨在 Agent 里断链 | `think` 轮首消费 `confirm`，命中即裁决并把结果作为答复 |
| 3 | **观察轨在生产路径上没有调用点** | `extract_response`（模型输出→`shadow=1`）只有随迁测试在调，等于"模型输出轨"没接上 | 接进 `consolidate` 的 `assistant_text` 分支——**两形态同一处**（都走 `consolidate`），`TurnResult.observed_ids` 留证 |
| 4 | 检索无命中时 agent **跳过第 2 步**（意图动作） | 空库上跑「记住：X」「列出偏好」「写成文件」什么都不做，还报 `completed`（假完成） | `route_after_act` 改为检索后一律回 `think`；检索类动作的答复如实说命中条数（空命中按"没依据"走三出口） |

> 第 3 条的开关口径：观察轨抽取受**学习开关**（`停止学习`／`继续学习`）与"是否配了模型端点"约束；
> 离线档不调模型（不花钱），有端点时每轮多一次抽取调用——这是前身设计，想省掉就停学习开关。
