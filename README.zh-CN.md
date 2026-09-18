# 🧠 Hippocampus

[![ci](https://github.com/Chestnuts-Sisyphus/hippocampus/actions/workflows/ci.yml/badge.svg)](https://github.com/Chestnuts-Sisyphus/hippocampus/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/Chestnuts-Sisyphus/hippocampus)](https://github.com/Chestnuts-Sisyphus/hippocampus/releases)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](pyproject.toml)
[![runtime deps](https://img.shields.io/badge/runtime%20deps-4-brightgreen.svg)](pyproject.toml)

**跨会话记忆 agent —— 一个记忆核心（`MemoryCore`），两种消费形态；公开基准官方判分已实测。**

[English](README.md) · [机制](docs/memory-core-v1.md) · [架构](#架构) · [跑起来](#快速开始) · [安全](docs/security.md)

---

Hippocampus 把"记忆"做成 agent 的核心能力，而不是外挂的检索库：

- **记忆核心**：四通道检索（语义／关键词／图谱／事件线索）＋ 相关性断崖截断 ＋ 预算装填；
  stable／fluid 分层注入；**双轨（确认轨／非确认轨）**＋**观察轨**隔离（模型输出永不静默
  注入）；冲突挂起人工确认；生命周期与离线固化；安全与漏抽守卫。每条记忆**可查看、可改、
  可删、可追溯来源**。
- **代理形态**：**三种入站格式**都接——OpenAI Chat Completions、OpenAI Responses、
  Anthropic Messages（按请求体形状判定）；把 `base_url` 指过来就获得记忆：请求前注入、
  响应后固化、冲突随回复回传确认块，回复按客户端**自己那套协议**返回。客户端不改一行代码。
  详见 [docs/proxy.md](docs/proxy.md)（含格式矩阵与诚实边界）。
- **Agent 形态**：LangGraph 编排（think／act／answer）＋ LangChain 工具接入；记忆机制全程
  融入思考与行动：检索决定上下文 → 执行 → **按出口与依据判定取舍固化**；三出口（完成／
  无法完成／需人工升级）；轨迹可复演。

对外可插拔目前落地的是**模型端点**（OpenAI 兼容，可换上游）；**记忆后端**（SQLite＋Chroma）
与**工具来源**的可插拔见 `docs/roadmap.md` 的路线图。工具来源当前是 Agent 形态内置的固定
工具集（写文件／列目录／记记忆／查记忆／搜岗位），**未做 MCP 等第三方工具接入**——不承诺
"含 MCP"。

---

## 官方判分（2026-09-18 实测）

官方判分臂（`bench --model-arm`，显式开关才出站）：模型基于**记忆层注入上下文（top-8，
非全文）**作答（deepseek-chat、temperature 0）＋ 官方判分——判分 prompt 与算法逐一钉官方
仓库 revision（LoCoMo `snap-research/locomo @3eb6f2c`、LongMemEval `xiaowu0162/LongMemEval
@9e0b455`）：

| 基准 | 官方指标 | 结果（95% CI） | 题量 |
|---|---|---|---|
| LongMemEval-oracle | LLM 判分准确率（官方 judge prompt） | **70.5%（63.8%–76.4%，Wilson）** | 抽 200 |
| LoCoMo-10 | 官方 F1（Porter 词干词面，官方判分脚本） | **32.55%（30.8%–34.4%，bootstrap）** | 全量 1986 |
| LoCoMo-10 + `--neighbors`（±1 轮扩展） | 同一个官方 F1 | **38.68%（36.8%–40.5%，bootstrap）** | 全量 1986 |

同批检索口径（证据命中／答在文内）：LongMemEval **100.0%**／48.5%；LoCoMo **45.5%**／17.4% 基线，
**63.7%**／22.7% 为 `--neighbors` 扩展臂（神经嵌入档 `bge-small-en-v1.5`；默认零下载词法档 36.7%）。

> **引用脚注（别省）**：作答输入＝记忆层 top-8 注入上下文，**不是全文**——与全文基线
> 同表比必须带记录；官方判分要花 API 费用（预算硬停 ¥30 内置，每轮报告余额跑前/跑后）。
> 完整数字、分类表、CI 方法、花费与复跑命令见 `docs/benchmark.md` 与 `docs/roadmap.md`。

性能（真实运行库同规模合成库：1154 记忆／2406 实体／6035 关系／497 事件）：
四通道检索 p50 **48 ms**；注入装配 p50 109 ms；单条写入 p50 **42 ms**（增量索引同步，
2026-09-18；修复前全量 re-sync 为 632.8 ms）。多账户长跑内存有界：会话缓存 LRU 上限可配，
LongMemEval 神经档 200 账户峰值 RSS **~1.1 GB**（原 ~4 GB），证据命中仍 100%。

## 快速开始

```bash
# 安装（三选一；尚未发布 PyPI，所以前两条是现在能用的路径）
pip install "hippocampus-agent[vector,proxy] @ git+https://github.com/Chestnuts-Sisyphus/hippocampus"
#   或：把源码目录拷到本机后  pip install -e "/path/to/hippocampus[vector,proxy]"
#   或（不装，只跑）：          PYTHONPATH=/path/to/hippocampus/src python -m hippocampus.cli doctor

hippocampus doctor                     # 体检：数据根／端口／锁／索引／嵌入档
hippocampus seed                       # 灌入示例数据（含已知真值：事实／冲突对／过期项）
hippocampus demo --memories            # 一键跑评测题 + 记忆开/关对照
# 官方判分（需要端点与凭据，env 注入；护栏=并发16/重试2/超时120s/预算¥30硬停）
export HIPPOCAMPUS_BASE_URL="https://api.deepseek.com/v1" HIPPOCAMPUS_MODEL="deepseek-chat"
hippocampus bench longmemeval --data D:/tmp/hc-bench/longmemeval_oracle.json --limit 200 --model-arm
hippocampus bench locomo --data D:/tmp/hc-bench/locomo10.json --model-arm
```

> 项目名是 **Hippocampus**，CLI 与 import 包名同样是 `hippocampus`，仓库也是 `…/hippocampus`；
> 只有 **PyPI 分发名**（`pip install` / `pip show` 里那一串）是 `hippocampus-agent`——
> 裸名 `hippocampus` 在 PyPI 上属于第三方（同名 memoization 包），
> 那样写会让 `pip install hippocampus` 装错东西。实测记录见 [docs/naming.md](docs/naming.md)。

> 两条路径都实测过：**无 git 环境**（把源码树拷过去 + `pip install --offline -e .`）
> 与**无网**（不装 `[vector]`：语义通道降级、其余通道照常，`doctor` 会明说）。
> 只装核心（不带 `[vector]`）时检索走词法通道，功能不丢、召回弱一些——
> 这条降级路径在 CI 里是一个独立任务（`core-only`）。

`demo` 的实测输出（本机 2026-09-17，离线档、无凭据、示例数据）：

```
[记忆开] 通过 10/10（成功率 100%，平均 2.0 步）
[记忆关] 通过 3/10（成功率 30%，平均 2.0 步）
     失败分类：检索未召回 7
[标签双来源] 一致率 100%（9 题可比）
边界声明：本表为最小版评测——题量 10（7 问 3 动作）、单模型端点、单轮次；
        离线档作答器为规则作答器（不是模型推理）；样本是合成数据，
        只证方法可复现，效果结论不作普适承诺。
```

> 数字口径：10 题、7 问 3 动作、两次对照；**合成示例数据**，不是真实用户数据。
> 换机复跑同法：`hippocampus seed && hippocampus demo --memories`。

代理形态：

```bash
hippocampus proxy --port 8765
# 首次启动会生成实例令牌（<数据根>/instance_token；`hippocampus doctor` 显示前 8 位）：
#   客户端请求带  Authorization: Bearer <完整令牌>，未带令牌回 401
# 然后把客户端的 base_url 改成 http://127.0.0.1:8765
#    OpenAI Chat：/v1/chat/completions   Responses：/v1/responses   Anthropic：/v1/messages
# stream:true 时上游 SSE 逐行转发（真流式）；上游非 2xx 原样透传状态码与错误体
```

Agent 形态：

```bash
hippocampus chat "帮我挑 5 个适合我的岗位"      # 需要配置模型端点
hippocampus chat --offline "记住：我不看外包"   # 无 key／无网也能跑记忆纪律
```

## 架构

| 层 | 形态 | 入口 |
|---|---|---|
| 记忆核心 | `MemoryCore`（SQLite＋Chroma＋BM25，四通道检索） | `hippocampus.core` |
| 代理 | OpenAI 兼容代理（`/v1/chat/completions`、`/v1/responses`、`/v1/messages`） | `hippocampus proxy` |
| Agent | LangGraph 三节点图（think／act／answer） | `hippocampus chat` |

每一层都接守卫：出站 URL 校验（默认拒环回／私有／保留）、凭据只来自环境变量或密钥服务
（源码与测试零字面量）、记忆写入过 safe-ident/safe-DDL、离线档＝**绝不发出站请求**——
基准默认离线，官方判分必须显式 `--model-arm`。

## 模型端点与凭据

**凭据只从环境变量或系统密钥服务读取，不写进任何文件**（源码、示例、配置、测试都不含
可用凭据字面量）：

```bash
export HIPPOCAMPUS_API_KEY=...        # 或 DEEPSEEK_API_KEY / OPENAI_API_KEY
export HIPPOCAMPUS_BASE_URL=https://api.deepseek.com   # 可选，也可写在 config.json
```

配置文件（`<数据根>/config.json`）只放非敏感项；出现疑似凭据字段会被**拒绝加载**。
详见 [docs/security.md](docs/security.md)。

## 离线档

无 key、无网、无 git 也能跑（CI 每次 push 都跑这一档）：

- 显式记忆（`记住 X` / `忘掉 X`）、检索与注入、冲突挂起与确认、生命周期判定、
  注入过滤与开关——**全部规则化可用**；
- 嵌入默认**内置档（零下载）**；
- 需要模型才能做的事（从自由文本里自动抽取记忆、多步 agent 推理）在离线档下不提供。

## 验证自己跑一遍

```bash
python -m pytest tests/ -q            # 515 条（含模型臂 21 条；C 盘紧张时加 --basetemp=D:/tmp/pt）
python scripts/check_interface.py     # MemoryCore v1 契约（scope 第一参数／无 HTTP 字段／只追加）
python scripts/audit_deps.py          # 依赖审计：全局单例残留必须为 0
python scripts/scan_credentials.py    # 凭据扫描：零命中
python scripts/scan_personal_data.py --home <示例库根>   # 示例库无个人数据
python scripts/calibrate.py           # 阈值标定（打印分数分布与建议证据线）
```

CI（GitHub Actions）跑的就是上面这一串，**全程无 key、无网**（离线档），
Windows 与 Linux 双平台。

## 工程约定

- 记忆库是**单写者**的：同一库目录同时只有一个进程可写，第二个写者排队或被告知
  （僵尸锁可回收，`hippocampus doctor` 可查锁状态）。
- 出站 URL：由数据或模型提供的地址仅允许 http／https，且拒绝环回／私有／保留地址。
- 记忆只增不删：修改走 **supersede**（旧条保留、可追溯），删除走归档。

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/benchmark.md](docs/benchmark.md) | 公开基准协议：钉版本数据（sha256）、检索/官方判分双口径、复跑命令 |
| [docs/roadmap.md](docs/roadmap.md) | 已知限制、诚实边界、改进路线图 |
| [docs/proxy.md](docs/proxy.md) | 代理形态：格式矩阵、鉴权、流式、诚实边界 |
| [docs/memory-core-v1.md](docs/memory-core-v1.md) | `MemoryCore` v1 接口契约（只追加） |
| [docs/security.md](docs/security.md) | 威胁模型、出站 URL 规则、凭据处理 |
| [docs/embedding.md](docs/embedding.md) | 嵌入档选择（带实测）、按模型池化配置 |
| [docs/offline.md](docs/offline.md) | 离线档：无网/无凭据下能做什么 |
| [docs/naming.md](docs/naming.md) | 命名决策（PyPI 分发名等） |
| [docs/forms-parity.md](docs/forms-parity.md) | 两形态功能对齐表 |
| [docs/verification-design.md](docs/verification-design.md) | 评测题验证方法 |

## 目录结构

```
src/hippocampus/
  core/      记忆核心门面（MemoryCore v1 冻结接口 + 单写者锁）
  memory/    记忆层实现（四通道检索／生命周期／守卫／安全／离线固化）
  proxy/     代理形态（OpenAI 兼容端点）
  agent/     Agent 形态（LangGraph 三节点 + 工具 + 轨迹）
  eval/      评测（题目集／判分／开关对照／官方判分臂）
  cli.py     命令行
tests/       随迁测试 + 本项目测试
docs/        接口冻结文档／命名实测／依赖审计／安全说明
```

## 状态与已知限制（如实）

- **无 MCP / 第三方工具接入**（设计如此，正本写明）——指**消费**方向；兄弟项目 GitTok 对外**提供**
  MCP server，与本合同无关，别混着读。
- **管理口 `/run` `/trace` `/health` 已实现但只绑环回**（七轮 T3：`hippocampus serve`；带实例令牌；
  与代理形态同一端口，一次只起一个）。
- **会话缓存有 LRU 上限**（`HIPPOCAMPUS_SESSION_CACHE_MAX`，默认 16）：常驻内存有界（~1.1 GB），
  代价是被逐出的账户下次访问要重开会话。
- **单条写入走增量索引同步**：1154 记忆规模 p50 **42 ms**（修复前全量 re-sync 为 632.8 ms）；
  残余风险＝并发写入下索引与库可能出现差异，由 `index_health` 与 `hippocampus index rebuild` 兜底。
- **英文语料 × 中文标定**分词／阈值：英文基准绝对分低于英文原生系统（逐基准有注）。
- **官方判分为单模型单次测量**（deepseek-chat；答辩引用必须带"top-8 注入、非全文"脚注）。
- **命名**：PyPI 分发名是 `hippocampus-agent`（见上）。

## 许可

MIT，见 [LICENSE](LICENSE)。