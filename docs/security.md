# 安全与隐私（可复核）

本项目有两条**硬约束**，它们不是文档口号，都有对应的测试与扫描脚本。

## ① 凭据只从环境变量或密钥服务读取

**规则**：源码、示例、测试、文档、配置模板里**都不出现可用凭据字面量**；
测试用明确标注的占位符（形如 `placeholder-*`、`sk-test-*`）。

**实现**：

| 位置 | 行为 |
|---|---|
| `hippocampus/memory/config.py::resolve_api_key` | **全项目唯一读凭据的地方**：环境变量（`HIPPOCAMPUS_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`）→ 系统密钥服务（`keyring`，可选依赖）。不起子进程、不落盘、不写日志 |
| 配置文件 `<data_root>/config.json` | **不含 `api_key` 字段**；一旦出现疑似凭据字段（`api_key`/`token`/`secret`/`password`…）**拒绝加载**（fail loud，不静默忽略） |
| `doctor` 命令 | 只报告"凭据有没有"，从不打印凭据内容 |
| 出站请求 | 凭据只在 `hippocampus/memory/llm.py` 内拼进请求头；代理形态不接触凭据（转发也走同一个客户端） |

**怎么验**：

```bash
python scripts/scan_credentials.py              # 仓库扫描（含示例库可选）
pytest tests/test_a40_a41_safety.py -k credential -v
```

## ② 出站 URL 只允许 http/https，且拒绝环回／私有／保留地址

**两条通道，规则不同**（这条区分很重要，写错了要么拦不住要么拦太狠）：

| 通道 | 入口 | 来源 | 规则 |
|---|---|---|---|
| **数据/模型提供的 URL** | `validate_outbound_url` | 工具参数、记忆内容里的链接、抓取目标 | 仅 http/https；**拒绝** localhost／环回／私有／保留／链路本地／云元数据地址（含 IPv6） |
| **操作员配置的端点** | `validate_endpoint_url` | 模型 `base_url`、本代理监听地址 | 仅 http/https；**允许环回**（本地模型端点合法） |

**判定方式**：`ipaddress` 标准库（不是字符串前缀匹配）。下列写法都要拦住：

```
http://127.0.0.1/            http://localhost:8080/
http://[::1]/                http://0.0.0.0/
http://10.0.0.1/             http://192.168.1.1/
http://169.254.169.254/      （云元数据）
http://2130706433/           （127.0.0.1 的十进制写法）
http://0x7f000001/           （十六进制写法）
file:///etc/passwd           ftp://…   gopher://…
```

**落点**：`hippocampus/net.py`（校验实现）＋ `hippocampus/agent/tools.py` 的 `fetch_url`
工具（调用前校验，被拒时错误分类为"被拒"）＋ 模型端点在 `llm.py` 内校验。

**怎么验**：

```bash
pytest tests/test_a40_a41_safety.py -k url -v
```

## ③ 代理鉴权：实例令牌（A2）

**规则**：代理形态的三个推理端点（`/v1/chat/completions`、`/v1/responses`、`/v1/messages`）
要求 `Authorization: Bearer <实例令牌>`；未带或不对一律回 **401**。

| 项 | 说明 |
|---|---|
| 令牌从哪来 | 首次 `hippocampus proxy` 启动时生成（`secrets.token_hex(24)`，48 位十六进制），落盘 `<数据根>/instance_token`；进程重启复用同一份 |
| 怎么拿到 | `hippocampus doctor` 只打印**前 8 位**（够确认"是不是这份"，不够冒用）；完整令牌读文件 |
| 不是外部凭据 | 实例令牌只保护本机代理入口；上游模型凭据仍只从环境变量／密钥服务读（见 ①），两者不混 |
| 使用边界 | 这是**单机自用**的入口门禁，不是多用户鉴权：不区分角色、无过期时间、无吊销列表；换令牌＝删文件重启 |
| 不校验令牌的端点 | `/health`、`/v1/models`：只回本机元信息与配置的模型名，不含任何记忆内容。代理默认只监听 `127.0.0.1`，**不要绑到对外地址** |

**怎么验**：

```bash
pytest tests/test_n7_auth_passthrough.py -q     # 生成/复用、未带 401、带对放行、非 2xx 透传
```

## ④ 审计与观察文件（位置、内容、边界）

| 文件 | 位置 | 内容 |
|---|---|---|
| `audit.jsonl` | `<数据根>/accounts/<account>/audit.jsonl` | 每次注入检索的**候选全集**（top-N=50，超限只记计数）：doc_id／kind／channel／score／是否注入／被剔理由（A18） |
| `observe.jsonl` | 同目录 | 注入事件（query ＋实际注入的 id）与确认事件（confirm 的胜出／veto 的落选 id） |
| `trace`（JSON） | 同目录 | 一轮的注入／决策轨迹；`hippocampus explain` 把三份合并成一份 run 视图 |

- 两份 JSONL 都是**旁路追加写**：写失败只记 stderr，绝不阻断注入／固化主流程。
- 它们**含记忆原文与查询原文**（不是脱敏日志）：属本机私有数据，随账户目录一起管理；
  不进仓库、不进示例库（`scripts/scan_personal_data.py` 会扫示例库）。
- 文件权限：Windows 下继承账户目录 ACL（本项目的使用边界＝单机单用户自用，与实例令牌同一口径）。

**怎么验**：

```bash
pytest tests/test_n4_audit_sink.py tests/test_n16_disk_rotation.py -q
hippocampus explain --run <轨迹名>          # 候选全集 + 被剔理由
```

## ⑤ 其他隐私立场（写进设计，不是补丁）

- **记忆只增不删**：修改走 `supersede`（旧条保留、可追溯），删除走归档（`archived`）。
  这是"记忆可审计"的底座——能被悄悄改掉的记忆没法审计。
- **观察轨隔离**：模型输出进观察轨（`shadow=1`，**永不静默注入**）；只有用户原话或显式"记住"
  才进正式记忆。晋升只能由人确认触发（无自动晋升）。命名口径：**"双轨"专指确认轨（`fire_track_a`：
  用户消息的 preference／fact，输出完成前形成确认块）／非确认轨（`after_response`：status／resource
  响应后自动入库）**；模型侧一律叫**观察轨**（`shadow=1`），两者不是一个维度。
- **scope 标识是外部输入**：`account`／`session` 来自 HTTP 头或 CLI 参数，一律做
  目录穿越校验（只允许 `[A-Za-z0-9._-]`，≤64 字符，不含 `..`），非法在入口就拒（HTTP 400）。
- **写文件工具限定工作目录**：越界路径直接判"参数错"，不落盘。
- **危险动作需确认**：写文件／出站抓取属危险动作，未获确认时返回"被拒"并让编排层走
  "需人工升级"出口。
- **库目录单写者锁**：两个进程同时写同一记忆库不保证安全，因此有库级写锁
  （第二个写者排队或被告知；僵尸锁可回收）。文档明示该限制，不假装支持并发写。
