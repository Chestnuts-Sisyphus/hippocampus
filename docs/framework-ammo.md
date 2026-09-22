# 框架弹药卡（面试应答：框架管执行图，我管纪律）

> 用途：被问到"用了 LangChain／LangGraph，那你自己做了什么"时的应答卡。
> 纪律：只说本仓库真实存在的东西（每条都给了落点，可当场指）；不吹"自研框架"，
> 也不把自己说成"只是调库"。**禁词自查**：不出现"熟悉／精通／生产级／高并发／真实用户量／通用框架"。
> **范围**：本卡只管**框架层**口径（编排边界、工具协议、MCP 支撑面）。**通用认知的十六个域**
> （上下文工程／记忆范式／评测基准／可观测性／Agent 安全／MCP 规范版本／成本工程／微调 vs RAG／
> 多 Agent 框架／协议／多模态／幻觉控制／容器化／数据严谨性）**不在本卡**——它们已另行整理成
> **求职工作区里的「面试弹药库」**（本机求职目录下，非本仓文件）。那份文档**第一章是禁背清单
> （19 条编造或错引，背进面试＝自爆）**，引用任何通用概念前先过它。
> 三者分工一句话：**数字看 `docs/benchmark.md`，框架看本卡，通用认知看弹药库**；两处冲突时，
> 数字与框架口径以本卡与 `docs/benchmark.md` 为准。

## 一、一句话立场

**编排与工具交给框架（LangChain / LangGraph），记忆的纪律我自己写**——因为框架不回答
"什么该记、什么不该记、旧了怎么办、模型自己说的话能不能进记忆"这四个问题。

## 二、边界对照（谁管什么）

| 层 | 谁提供 | 本项目的实际用法（落点） |
|---|---|---|
| 图与状态、条件边 | LangGraph | `agent/graph.py`：`StateGraph` ＋ `think→act→answer` 三节点 ＋ 两条条件边（`route_after_think`／`route_after_act`）。**没用 checkpointer，也没用 `interrupt`**：`MemorySaver`／`SqliteSaver`／`checkpointer`／`interrupt`／`Command` 在本仓 `src/` 下 **0 命中**；`needs_human` 是规则式出口（危险动作确认闸 `agent/tools.py` 回"被拒" → `act` 置出口），轨迹复演是 `agent/runner.py::replay` 的**指纹比对**，都不是框架的检查点／中断语义 |
| 工具定义与调用协议 | LangChain 生态惯例（本项目用同一套 JSON Schema 子集自校验，零额外依赖） | `agent/tools.py`：`validate_args` ＋ 错误四分类（参数错／被拒／工具错／环境错） |
| 模型接入 | OpenAI 兼容端点（可换上游） | `memory/llm.py`（唯一读凭据的地方）＋ `proxy/`（三向格式转换） |
| **记忆的写入门槛** | **本项目** | 五组安全守卫（`memory/security.py`）＋ 去重三态（`memory/dedup.py`：supersede／admit／挂起）＋ **证据闸**（`agent/policy.py::has_evidence`：只认语义与关键词通道的分数） |
| **来源分轨** | **本项目** | 确认轨／非确认轨（`memory_bridge.fire_track_a`／`after_response`）＋ 观察轨（`shadow=1`，模型输出永不静默注入） |
| **过期与修正** | **本项目** | 生命周期降级／归档（`memory/lifecycle.py`）＋ 冲突挂起与 TTL（`confirm.py`＋`core.confirm`） |
| **轮末守卫** | **本项目** | 漏抽补实体／消歧（`memory/missed_extract.py`／`retrieval_guard.py`／`disambiguate.py`，接在 `core.consolidate`） |
| **可审计** | **本项目** | 旁路 `audit.jsonl`（候选全集 top-50）＋ `observe.jsonl`＋`explain`（"那条为什么没进"） |

## 三、预设问答

**Q1「框架都是现成的，你的贡献在哪？」**
编排确实不是我的贡献——所以我把它写薄：`agent/` 一共 4 个文件、图只有三个节点。
我的代码在记忆层（`memory/` 与 `core/`）：写入门槛、来源分轨、过期修正、轮末守卫、可审计。
判据很直接：把 LangGraph 换成别的编排器，记忆层一行不用改——`MemoryCore` 是框架无关的门面
（v1 冻结接口、无 HTTP 词汇、`scripts/check_interface.py` 是 CI 门禁）。

**Q2「为什么不用现成的记忆组件（Mem0／Letta 这类）？」**
定位不同：那些更像"给应用用的记忆服务／SDK"；本项目的形态是**一个记忆核心 + 两种消费方式**
（代理形态零改造接入／Agent 形态内嵌），要求是能把纪律写死并可测：证据闸、观察轨、
确认轨、守卫每一条都有对应测试文件。用现成组件意味着这些纪律只能"配置"，不能"定义"。

**Q3「LangGraph 的人在环中断（interrupt）你用了么？」**
没有用 `interrupt` 原语，用的是**记忆层的确认块**（`confirm.py`）：挂起块随回复呈现、
`确认 n`／`否决` 在请求入口消费、TTL 7 天、跨进程可裁决（`pending_blocks` 落库）。
取舍：确认块属于**记忆语义**（不只是图执行状态），放在记忆层才能被两种形态共用；
代价是不能用框架的检查点续跑语义——这一点在 `docs/roadmap.md` 里如实记着。

**Q4「框架升级／换掉怎么办？」**
门面冻结＋契约门禁：`MemoryCore` v1 只允许追加字段（`check_interface.py` 三条断言），
编排层与形态层都只经这个门面；换编排器或换形态，记忆层不动。

**Q5「你这条链上有几个地方会调模型？成本怎么控？」**
三处：写入抽取（确认轨／非确认轨）、轮末消歧、观察轨抽取。控成本三个闸：
**学习开关**（`停止学习` → 三条抽取链全停）、**离线档**（无端点则全部规则降级，零调用）、
**观察轨可关**。评测／基准默认钉离线档（`hippocampus bench` 默认强制离线，见 `docs/benchmark.md`）。

**Q6「你写过 MCP server 么？技能栏那个 MCP 是怎么支撑的？」**
分两句，**别混**：
- **对外提供：做了。** **兄弟项目 GitTok**（独立仓库，非本仓）的 MCP server，位于那边的 `mcp-gittok/` ——
  `search`／`top`／`detail` 三个**只读**工具（`tools.ts:158,232,305`）、**stdio** 传输
  （`index.ts:17,185-191`）、构建期把站点同一套排序／搜索实现内联进单文件
  （`build.mjs`，产物 `dist/index.js` 实测 782,483 B）、**17 项 parity 测试**逐条比对站点源码、
  CI 单开一个 job（`.github/workflows/ci.yml:76`）。
- **对内消费：没做、且已取消承诺。** 早期文案写过"含 MCP"，源码从未实现，2026-09-17 定论
  （`docs/roadmap.md:133-136`）。技能栏提到 MCP 时**只能拿 GitTok 那句讲**。
- **口径为什么两句不冲突**（一句话备好）：`docs/roadmap.md:17` —— GitTok 的 MCP 是**对外提供**方向，
  本项目"不做 MCP 工具接入"说的是**消费**方向（不通过 MCP 接第三方工具）。
- **讲协议先报版本**：MCP 现行协议版本 **2026-07-28**；版本号口径本身就是"最后一次不向后兼容
  变更的日期"，所以跨版本要先协商 `_meta` 里的 `protocolVersion`，不支持则回
  `UnsupportedProtocolVersionError`。**传输别讲 HTTP+SSE** —— 它在 **2025-03-26** 那版就被
  **Streamable HTTP** 替换了。出处：`https://modelcontextprotocol.io/specification/versioning`、
  `.../specification/2025-03-26/changelog`。

## 四、别这么说（口径红线）

- 不要说"我写了一个 agent 框架"——写的是记忆层＋**薄编排**；
- 不要说"记忆质量优于 X"——只报实测数字与样本边界（`docs/benchmark.md` 有公开基准的离线口径）；
- 不要把"用了 LangGraph"说成难点——难点是**纪律的可测性**（每条机制都有测试文件）；
- 不要提 **Hippocampus 的** MCP 接入（已取消承诺，见 `docs/roadmap.md` §六）。
  ⚠ **别读成"一句 MCP 都不能提"**：GitTok 的 MCP server 是**对外提供**、真实上线的那一件，
  技能栏讲 MCP 只能拿它讲；两句方向相反、不冲突（`docs/roadmap.md:17`）。细节与完整答法见 Q6。
