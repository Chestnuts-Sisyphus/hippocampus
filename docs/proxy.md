# 代理形态：三种入站格式

> 一句话：**任何 OpenAI 兼容或 Anthropic 兼容的客户端，把 base_url 指过来就获得记忆**——
> 客户端不用改代码，回复也按**它自己那一套协议**返回。

## 一、格式矩阵（与前身一致）

入站格式按**请求体形状**判定（不看路径，三个端点都通吃）：

| 入站格式 | 判别式 | 典型客户端 |
|---|---|---|
| OpenAI Chat Completions | 有 `messages`，且 `system` 不是字符串/数组 | 绝大多数聊天客户端、脚本、自建 agent |
| OpenAI Responses API | 有 `input` | Codex 一类使用 Responses 的客户端 |
| Anthropic Messages | 有 `system`（str 或 block 数组）且 `messages` | Claude Code、Anthropic SDK |

上游格式由配置 `llm.api_mode` 决定：`chat_completions`（默认）／`anthropic_messages`／`responses`。

**支持的组合**：

| 入站 ＼ 上游 | chat | anthropic | responses |
|---|---|---|---|
| **chat** | 透传 | `chat_to_anthropic_request` | **501**（前身同样不支持） |
| **anthropic** | `messages_to_chat_request` | 透传 | **501**（前身同样不支持） |
| **responses** | `responses_to_chat_body` | `responses_to_anthropic_request` | 透传 |

不支持的组合**如实回 501**，不假装能转。转换器在 `format_converters.py`（前身原样 vendoring，
随迁 21 条测试）；`responses↔chat` 两个辅助在 `formats.py`。

## 二、记忆落在哪（按入站格式，不是同一个位置）

| 入站 | stable 层（长期偏好／身份前言） | fluid 层（本轮相关） |
|---|---|---|
| chat | 并进首条 `system` 消息（无则插到最前） | 追加一条 `user` 消息（`[海马体记忆]\n…`） |
| anthropic | 追加进 `system` 块数组（够长时挂 `cache_control: ephemeral`） | 追加 messages 末尾 |
| responses | 拼到 `instructions` 末尾 | 追加 `input` 末尾一条 message |

> 这条分层是**前身的设计**：稳定层每轮都在、可被缓存；流动层只放本轮相关。
> 面试被问"为什么不全塞 system"时，答案就是：稳定层可缓存、流动层语义是"本轮相关"。

## 三、回复与确认块

- 客户端收到**自己那一套**响应：chat 的 `choices[].message.content`／anthropic 的
  `content[].text`／responses 的 `output[].content[].text`。
- **确认块由记忆层生成、由代理追加**（不由模型生成，A39）；关开关（配置或
  `X-Hippocampus-Confirm: 0` 头）就不追加；`确认 n`／`否决 n` 在入口消费，不转发上游。
- 流式（`stream: true`）是**真流式**：上游 SSE **逐行转发**给客户端，每来一行就转发一行
  （chat／anthropic／responses 三种上游格式各自按本家协议转），没有"攒完再一次性吐出"。
  流末做本轮固化，确认块作为**末尾 delta** 追加；上游非 2xx 时按状态码＋错误体原样透传。

## 四、诚实的边界（写在文档里，不藏）

| 限制 | 说明 |
|---|---|
| chat→responses 不支持 | 与前身同样回 501。 |
| 工具调用 | chat 入站→chat 上游透传；anthropic／responses 的工具字段按随迁转换器处理，三向都有端到端用例（`tests/test_n15_proxy_tools.py`）。 |
| 代理 session 默认按天分桶 | `X-Hippocampus-Session`／`X-Session-Id` 可显式指定；缺省按 `day-YYYYMMDD` 分桶（可配 `proxy.session_bucketing`）。 |
| `/v1/models` 回配置的模型名 | 列表回 `llm.model` 配的那个名字（未配则回内置默认名），不再回占位串。 |
| 请求进入代理后记忆写入单库 | 代理与 Agent 两种形态共用同一记忆库；同一库目录是**单写者**（库级写锁），不要同时跑两个写进程。 |

## 五、怎么自己验

```bash
# 起代理（离线档也能起，用来验协议形状；首次启动生成实例令牌）
hippocampus proxy --port 8765
TOKEN=$(cat <数据根>/instance_token)     # 数据根见 `hippocampus doctor`

# chat 入站
curl -s localhost:8765/v1/chat/completions -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"model":"m","messages":[{"role":"user","content":"我投简历有什么要求？"}]}' | head -c 300

# anthropic 入站
curl -s localhost:8765/v1/messages -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"model":"m","max_tokens":256,"system":"你是助手","messages":[{"role":"user","content":"我投简历有什么要求？"}]}' | head -c 300

# responses 入站
curl -s localhost:8765/v1/responses -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"model":"m","instructions":"你是助手","input":"我投简历有什么要求？"}' | head -c 300

# 未带令牌应当拿到 401（鉴权确实生效）
curl -s -o /dev/null -w '%{http_code}\n' localhost:8765/v1/chat/completions \
  -H 'content-type: application/json' -d '{"model":"m","messages":[]}'

# 协议形状的自动化验证（真服务 + 真 HTTP 客户端）
python -m pytest tests/test_proxy_formats.py tests/test_n6_streaming.py tests/test_n7_auth_passthrough.py -q
```
