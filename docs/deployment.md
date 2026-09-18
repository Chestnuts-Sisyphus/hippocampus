# 部署模式（D5 补档 · 2026-09-18）：127.0.0.1 之外怎么跑

> 默认代理只监听 `127.0.0.1:8765`（安全面最小：环回无网卡暴露）。本文回答
> "单机自用之外"的几种跑法和各自的安全边界。默认不动任何配置即可用；下面全是显式选择。

## 一、默认：环回单机（推荐，零改动）

- 配置：`host=127.0.0.1`、`port=8765`（`HIPPOCAMPUS_PORT` 可换端口；`doctor` 打印实际值）。
- 鉴权：实例令牌（`instance_token`，首次启动生成，`Authorization: Bearer <token>`）——
  环回是"本机进程隔离"的第一道墙，令牌是第二道。
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

1. **只绑环回**。传非环回 `--host` 必须同时给 `--allow-remote`，否则拒起（退出码 2）；
   非环回还必须有实例令牌，拿不到令牌文件同样拒起——管理口能写记忆，裸奔到局域网不可接受。
2. **鉴权分两档（09-19 真机实测写清，不写成"全都 401"）**：
   · `POST /run`／`GET /trace` **必须**带 `Authorization: Bearer <instance_token>`，缺令牌 401（与代理形态同一条）；
   · `GET /health` 是**自 0.1.0 就存在的免鉴权探针位**（实测：不带令牌回 200），回的是端口／档位／计数／
     索引健康——默认只绑环回所以暴露面有限，但**是否收紧到必须令牌**属待定口径（已登记八轮候选，
     收紧会动到既有测试与外部探活用法，不擅自改）。
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

## 三、局域网/公网暴露（**不推荐**，真需要时按下面做）

1. **换监听地址**：`config.json` 或环境变量把 `host` 改为 `0.0.0.0`（任意网卡）或具体内网 IP。
2. **必须有令牌**：实例令牌默认就有；**不要**关掉鉴权跑公网。
3. **前置 TLS**：本项目代理是明文 HTTP——公网/跨网段请放在反向代理（nginx/caddy）后面
   终结 TLS，上游指向 `127.0.0.1:8765`。
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