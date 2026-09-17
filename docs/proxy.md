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
- 流式（`stream: true`）按各家协议回：chat 的 `data:` chunk ＋ `[DONE]`；
  anthropic 的 `message_start`→`content_block_delta`→`message_stop`；
  responses 的 `response.created`→`…`→`response.completed`（八事件序列）。

## 四、诚实的边界（写在文档里，不藏）

| 限制 | 说明 |
|---|---|
| **流式是"单 delta"** | 我们**没有**把上游的逐行流式转发给客户端（前身有）。客户端拿到的是"一次性到达的完整回复"，协议正确但**没有逐字效果**。真·逐行转发见缺口清单。 |
| **无鉴权** | 代理默认只监听 `127.0.0.1`，不校验令牌。前身有实例令牌（401）——这一项**尚未移植**，见缺口清单。 |
| **chat→responses 不支持** | 与前身同样回 501。 |
| 工具调用 | chat 入站→chat 上游透传；anthropic/responses 的工具字段按随迁转换器处理，**未做端到端联调**（没有可用的工具调用客户端做实测）。 |

## 五、怎么自己验

```bash
# 起代理（离线档也能起，用来验协议形状）
hippocampus proxy --port 8765

# chat 入站
curl -s localhost:8765/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"m","messages":[{"role":"user","content":"我投简历有什么要求？"}]}' | head -c 300

# anthropic 入站
curl -s localhost:8765/v1/messages -H 'content-type: application/json' \
  -d '{"model":"m","max_tokens":256,"system":"你是助手","messages":[{"role":"user","content":"我投简历有什么要求？"}]}' | head -c 300

# responses 入站
curl -s localhost:8765/v1/responses -H 'content-type: application/json' \
  -d '{"model":"m","instructions":"你是助手","input":"我投简历有什么要求？"}' | head -c 300

# 协议形状的自动化验证（真服务 + 真 HTTP 客户端）
python -m pytest tests/test_proxy_formats.py -q
```
