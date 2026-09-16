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

## ③ 其他隐私立场（写进设计，不是补丁）

- **记忆只增不删**：修改走 `supersede`（旧条保留、可追溯），删除走归档（`archived`）。
  这是"记忆可审计"的底座——能被悄悄改掉的记忆没法审计。
- **双轨隔离**：模型输出进观察轨（`shadow=1`，**永不注入**）；只有用户原话或显式"记住"
  才进正式记忆。晋升只能由人确认触发（无自动晋升）。
- **scope 标识是外部输入**：`account`／`session` 来自 HTTP 头或 CLI 参数，一律做
  目录穿越校验（只允许 `[A-Za-z0-9._-]`，≤64 字符，不含 `..`），非法在入口就拒（HTTP 400）。
- **写文件工具限定工作目录**：越界路径直接判"参数错"，不落盘。
- **危险动作需确认**：写文件／出站抓取属危险动作，未获确认时返回"被拒"并让编排层走
  "需人工升级"出口。
- **库目录单写者锁**：两个进程同时写同一记忆库不保证安全，因此有库级写锁
  （第二个写者排队或被告知；僵尸锁可回收）。文档明示该限制，不假装支持并发写。
