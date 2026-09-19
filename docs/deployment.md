# 部署模式（D5 补档 · 2026-09-18）：127.0.0.1 之外怎么跑

> 默认代理只监听 `127.0.0.1:8765`（安全面最小：环回无网卡暴露）。本文回答
> "单机自用之外"的几种跑法和各自的安全边界。默认不动任何配置即可用；下面全是显式选择。

## 一、默认：环回单机（推荐，零改动）

- 配置：`host=127.0.0.1`、`port=8765`（`HIPPOCAMPUS_PORT` 可换端口；`doctor` 打印实际值）。
- 鉴权：实例令牌（`instance_token`，首次启动生成，`Authorization: Bearer <token>`）——
  环回是"本机进程隔离"的第一道墙，令牌是第二道。
- **两形态同一启动闸（九轮 W1）**：`hippocampus proxy`（模型口）与 `hippocampus serve`（管理口）
  默认都只绑环回；要出网卡必须显式 `--allow-remote` **且**有实例令牌，否则拒起（退出码 2）。
  非环回时两侧的 `/health` 探针口同样要令牌，模型口的 `/v1/models` 也一并纳入（口径三件套见下 §二·五第 2 条）。
- 适用：个人电脑上用任何 OpenAI 兼容客户端接入。

## 二、同机多实例 / 多进程

- 一个数据根 = 一个记忆库 + 一个代理；要多开就**换数据根（不同 `HIPPOCAMPUS_HOME`）与端口**。
- 写锁：同一记忆库只允许一个写者进程（第二个排队/被告知）；不同数据根互不干扰。
- 会话缓存上限：多账户长跑内存有界（`HIPPOCAMPUS_SESSION_CACHE_MAX`，默认 16，见
  `docs/roadmap.md` A2/T5）。

## 二·五、服务形态（管理口：`/run` `/trace` `/health`，七轮 T3／E1）

代理形态是"模型口"（客户端把 base_url 指过来），**服务形态是"管理口"**：不给模型转发，
只暴露"跑一轮 / 追一次证据链 / 查健康"三件事，给运维脚本与桌面端按钮用。

```bash
hippocampus serve                      # 默认 127.0.0.1:8765（与代理形态同一端口，二择一起动）
hippocampus serve --port 8899          # 换端口
hippocampus serve --host 0.0.0.0 --allow-remote   # 显式承认要出网卡（无令牌则拒起）
```

| 端点 | 语义 | 备注 |
|---|---|---|
| `POST /run` | 执行一轮"注入＋固化"，回 `run_id`／注入内容／剔除原因／本轮写库 id／挂起数 | 请求体 `{"text": "…"}`；scope 走 `X-Hippocampus-Account`／`X-Hippocampus-Session` 头 |
| `GET /trace?run_id=…` | 按 `run_id` 取那次注入的**全链路审计**（候选全集＋injected 标记＋剔除原因）＋观察事件 | 依赖审计开关（`audit_enabled`，默认开）；查不到回 404 并说明原因 |
| `GET /health` | 索引与库健康（嵌入档／计数／锁／`index_health`） | 与代理形态同一个实现 |

安全默认（与 A2 一致，不可省略）：

1. **只绑环回（两形态同口径，九轮 W1）**。`serve` 与 `proxy` 都一样：传非环回 `--host` 必须同时给
   `--allow-remote`，否则拒起（退出码 2）；非环回还必须有实例令牌，拿不到令牌文件同样拒起——
   管理口能写记忆、模型口能把记忆注入并转给上游，裸奔到局域网都不可接受。
2. **鉴权分两档（八轮 V8 ＋ 九轮 W1 收紧后，两形态一致）**：
   · `POST /run`／`GET /trace`／`/v1/*` 对话端点 **必须**带 `Authorization: Bearer <instance_token>`，缺令牌 401；
   · 探针口 `GET /health`（两形态）与 `GET /v1/models`（模型口）**绑环回时免鉴权**
     （自 0.1.0 的探针位，实测不带令牌回 200，本机拨测方便）；
     **一旦绑到非环回（`--allow-remote`）就同样要令牌，缺令牌 401**——`/health` 回体里的计数／索引／锁、
     `/v1/models` 回的配置模型名都是实质信息面，挂到局域网裸奔不可接受。
     两态由 `tests/test_n35_r8_health_auth.py`（管理口）与 `tests/test_n38_r9_proxy_auth.py`（模型口）
     各钉一遍：接线（假 `uvicorn.run` 截 `build_app` 参数）＋行为（`TestClient` 401/200）＋ CLI 透参。
3. **不转发对话上游 ≠ 零出站**（09-19 真机实测订正）：服务形态起的是离线档（`upstream=None`），
   **不会把用户这轮话转给模型去作答**；但 `/run` 的**记忆固化**仍按当前配置调用模型端点做抽取——
   配到可用端点就出站（**仅 https**），配不到就走规则档（CI 正是靠这条不依赖任何 key）。
   真机踩到的原样：机器上存着失效 key 时，`/run` 曾以 ASGI **500** 崩在抽取那一步；
   现在兜底为 **502**，并在响应里带上**已完成的注入结果**（`run_id`／`injected`／`dropped`／`note`），
   断在哪一段一眼可见（回归测试 `tests/test_n31_r7_serve.py`）。

```bash
TOKEN=$(cat ~/.hippocampus/instance_token)
curl -s -X POST http://127.0.0.1:8765/run \
  -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -H "X-Hippocampus-Account: me" -d '{"text": "我找岗位时有哪些硬性限制？"}'
curl -s "http://127.0.0.1:8765/trace?run_id=<上一条返回的 run_id>" \
  -H "Authorization: Bearer $TOKEN" -H "X-Hippocampus-Account: me"
```

#### 返回字段（八轮 V9，以真机响应与代码为准）

下面的字段名**不是设计稿**：来自真起管理口进程拿到的响应（`scripts/live_management_smoke.py`），
取数实现是 `MemoryCore.trace_run()`（形态层不直接读记忆层日志，A22 架构闸）。
本小节由 `tests/test_n37_r8_live_smoke.py` 与真响应**双向比对**——实现加了字段而这里没补，测试就红。

**`/trace` 顶层**（查询参数 `run_id`，可选 `observe`＝`account`（默认）／`run`，scope 走头）

| 字段 | 含义 |
|---|---|
| `run_id` | 回显你传的那个 run_id（`/run` 的返回值） |
| `account` | 本次查询用的账户（`X-Hippocampus-Account`，缺省为默认账户） |
| `found` | 审计里有没有这个 run。**false 时接口直接回 404**，不回半个对象 |
| `audit` | 匹配到的检索审计事件数组（正常一条；同一 run_id 里检索被调用多次就会有多条） |
| `observe` | 观察事件数组（见下）。**粒度看 `observe_granularity`**，别默认它是"这一轮的" |
| `observe_granularity` | 本次取的是哪种粒度：`account`＝整个账户（默认，向后兼容）／`run`＝按该轮时间窗收窄 |

**`audit[]`** 一条＝一次注入检索（`memory/audit.record_retrieval`）

| 字段 | 含义 |
|---|---|
| `event` | 事件类型，目前恒为 `audit.retrieval` |
| `ts` | 毫秒时间戳 |
| `query` | 这一轮的问题原文（**截断 300 字**） |
| `run_id` | 该次注入的语义链标识（同一问题在多处调用时按它精确归位） |
| `candidates` | **候选全集**（排名顺序），最多落盘 `TOP_N`＝50 条 |
| `injected_ids` | 实际进入注入位的 id 列表（最多 50 条） |
| `total_candidates` | 截断前的候选总数 |
| `capped` | 超出 50 条、因此**没有**落盘的候选数（不是"总共只有这么多"） |

**`audit[].candidates[]`** 一条＝一个候选（这就是"为什么它没进来"的答案所在）

| 字段 | 含义 |
|---|---|
| `doc_id` | 候选的记忆／经历 id |
| `kind` | 条目类型（记忆类型；经历条另有其值） |
| `channel` | 命中它的检索通道（四通道之一；空串＝旧版事件没记） |
| `score` | 分数，保留 4 位小数 |
| `injected` | 是否真的被注入 |
| `dropped` | 是否被剔除（等价于 `reason` 非空） |
| `reason` | 剔除原因；**未剔除时是空串**（不是 null） |

**`observe[]`** 三类事件共用一个数组，按 `event` 区分（`memory/observe_log`）

| 字段 | 出现在 | 含义 |
|---|---|---|
| `event` | 全部 | `injection`／`confirmation`／`verification` |
| `ts` | 全部 | 毫秒时间戳 |
| `query`、`injected_ids` | injection | 该轮问题（截断 200 字）与实际注入 id（最多 20 条） |
| `decision`、`winner_id`、`loser_ids` | confirmation | 确认块消费结果（`confirm`／`veto`）与涉及的 id |
| `status`、`method`、`evidence`、`dropped` | verification | 可求证判定的状态／判据／证据摘要，以及是否因此不进正式库 |

> **一条实现事实，别按直觉理解**（九轮 W10 更新）：`observe.jsonl` 的三类事件**都不写 `run_id`**
> ——写它们在记忆层（`memory_bridge` / `core._log_verification`），够不到核心生成的那个 run_id。
> 所以默认粒度就是**账户级**：`observe=account` 会把该账户的 `verification`／`confirmation`
> 全部带出来（不只是这一轮的）。想要"只看这一轮"，传 **`?observe=run`**：实现按该 run 的
> 审计时间戳（拿不到则退回 `run_id` 前缀里嵌的毫秒）前后各 50 毫秒收窄，**只会更少不会更多**。
> 另一点直觉陷阱：`injection` 事件既不带 run_id 也不在那两类名单里，所以**两种粒度下它都不出现**在
> `observe` 数组里（它仍是落盘给数据质量线看的痕迹）。
> 这是**近似**不是严格隔离——同账户并发跑多个 run 时，落进同一窗口的仍会混进来；
> 真要做到逐 run 精确，得把 run_id 穿到记忆层的观察写入点（那是改协议，另案）。

**`/health`**（环回免鉴权；非环回要令牌——见上面第 2 条）

| 字段 | 含义 |
|---|---|
| `ok` | 恒 true（进程活着就回，探活用这个字段） |
| `offline` | 本进程是否离线档（管理口恒 true：不转发对话上游） |
| `confirm_block` | 是否追加确认块 |
| `port` | 配置里的代理端口（**不是**本次实际监听端口，端口以启动行为准） |
| `formats` | 支持的三种入站格式 |
| `upstream_endpoint` | 配置的上游端点类型（chat／anthropic／responses） |
| `embedding` | 当前嵌入档（`model`／`dimension` 等） |
| `stats` | 库内计数（记忆／经历／实体等） |
| `index` | 索引健康（`index_health()`：条数对齐、可写性、队列深度、最近错误） |
| `lock` | 会话锁状态 |

## 三、局域网/公网暴露（**不推荐**，真需要时按下面做）

1. **换监听地址**：`config.json` 或环境变量把 `host` 改为 `0.0.0.0`（任意网卡）或具体内网 IP。
2. **必须有令牌**：实例令牌默认就有；**不要**关掉鉴权跑公网。
3. **前置 TLS**：本项目代理是明文 HTTP——公网/跨网段请放在反向代理（nginx/caddy）后面终结 TLS，上游指向 `127.0.0.1:8765`；**已验证**（CI live_proxy_smoke 远程绑定测试通过）。
4. **出站只留 https**：项目自身出站默认仅 https（`validate_outbound_url` 拒环回/私有/保留之外
   **只放行调用方显式配置的模型端点**）；部署侧注意反向代理别把内网地址暴露成转发目标。

## 四、无代理的用法（纯记忆层）

不需要 HTTP 时别起代理：`hippocampus chat --offline`（CLI 问答）、`hippocampus memory list`
（管理）、`hippocampus demo`（评测）都直连记忆库，无端口占用、无鉴权面。

## 五、验证

```bash
hippocampus doctor                 # host/port/锁/索引健康/凭据有无
netstat -ano | grep :8765          # 只在本机监听 = 默认安全面
```

> 部署边界一句话：**默认环回+令牌，够了就别开网卡**；真要暴露，前置 TLS 且保留令牌。