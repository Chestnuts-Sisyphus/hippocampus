# 依赖审计（T2 交付物）

> 复跑：`python scripts/audit_deps.py`（退出码 0 = 干净）。下面的数字是**脚本跑出来的**。

## 一、可移植模块清单（抽取式 vendoring，不是重写）

前身 `hippocampus_prototype`（扁平模块 + 裸 import）→ 本项目 `hippocampus.memory` 子包。
迁移只做三件事：① 包内绝对 import；② 全局依赖改为显式注入；③ 配置与凭据改造。
**记忆机制一行未简化**。

| 批次 | 模块 | 承担的机制 |
|---|---|---|
| 第一批（核心链） | `database` | 多表 schema：entities／memories／episodes／relations／params＋参数快照 |
| | `retrieval` | 四通道检索（语义／BM25／图／事件线索）＋各通道独立底线＋**断崖截断**＋区块拼接＋预算装填＋防已见去重 |
| | `memory_bridge` | 会话门面：注入装配／固化／显式记住／开关／待确认队列 |
| | `extract` | LLM 抽取＋分块降级＋瞬时状态与情绪拦截 **＋离线句式规则抽取（本项目新增）** |
| | `pipeline` | 抽取→消歧→写入→回读 |
| | `dedup` | 三态去重（supersede／admit／drop）+ **P1 修复：drop 不再丢数据** |
| | `conflict` | 冲突检测（LLM）**＋规则冲突检测（本项目新增，离线可用）** |
| | `confirm` | 确认块与挂起队列、`确认 n`／`否决` 消费 |
| 第二批（长期与守卫） | `lifecycle` | active／dormant／archived ＋时间规则（可注入 `now`） |
| | `offline_consolidation` | 7 天 episode→memory 固化 |
| | `schema_compact` | 偏好 schema 合并 |
| | `security` | 五组守卫（注入／仿冒／密钥／劫持／PII），C·E 硬拦 |
| | `retrieval_guard` / `hub_guard` / `missed_extract` | 漏召补实体／mega-hub 标记／漏抽检测 |
| | `observe_log` | 观测日志（injected_ids） |
| | `embedding_models` | 嵌入模型解析（前缀分发） |
| | `event_time` / `disambiguate` / `feedback` / `observe` / `diagnose` | 事件时间解析／消歧／反馈环／观测报表／召回失败诊断 |
| 本项目新增 | `runtime` / `account` | 数据根与 scope 解析（前身对应模块兼账号体系，已重写） |
| | `config` | 配置（**去 DPAPI、去凭据落盘**） |
| | `llm` | 模型客户端（**去硬编码 key 路径**，离线档明确不可用） |
| | `builtin_embedding` | 内置嵌入档（零下载、离线、确定性） |
| | `calibration` | 嵌入档 → 检索参数标定表 |

## 二、需断开的全局依赖（前身的"扁平模块 + 模块级全局"）

| 前身写法 | 问题 | 本项目处置 | 状态 |
|---|---|---|---|
| `database.DB_PATH = runtime.data_root()/…`（导入即定死） | 多 scope 并行会串库 | 改为**兼容锚点 + 动态解析**：`connect(path=None)`，None 时按当前数据根解析 | ✓ |
| `retrieval.CHROMA_DIR = runtime.data_root()/…` | 同上 | 同上（`chroma_dir()` 动态解析；`MemorySession` 自带 `data_dir`） | ✓ |
| `memory_bridge.BASE = runtime.data_root()` | 无引用的遗留全局 | 删除 | ✓ |
| `runtime.data_root()` 单一全局根 | 无法同时服务多 scope／CI | 新增 `runtime.set_data_root()` / `using_data_root()`（可嵌套恢复） | ✓ |
| `config.py` DPAPI 加密 API key（`win32crypt`） | Windows 专有、且密文仍是凭据副本 | 删除；凭据只从环境变量／密钥服务读 | ✓ |
| `config.py` `msvcrt`／`fcntl` 文件锁 | 平台分支散落 | 收拢到 `core/locks.py`（唯一平台分支处） | ✓ |
| `account.py` 账号体系（注册/登录/令牌/元数据库） | 个人数据与多用户形态，本项目不做 | 重写为**scope → 目录**解析（含目录穿越校验） | ✓ |
| `memory_bridge` 里取 HTTP 请求体的函数（`extract_user_text(body)`／`detect_flow(body)`／`request_body_text(body)`） | 记忆层掺进传输协议 | 语义入口迁到 `MemoryCore`（`seen_text` 语义化）；HTTP 形状只留在 `proxy/app.py` | ✓ |
| `retrieval._EMB_FN` / `_EMB_FN_MODEL` 模块级缓存 | 跨 scope 共享嵌入函数 | **有意保留**（嵌入模型是配置项不是数据项），已标注 `[HIPPO]`；测试逐用例重置 | ✓（标注） |

## 三、第三方依赖对照

源码实际 import（脚本扫出来的）：

```
chromadb  fastapi  httpx  jieba  keyring  langgraph  numpy  onnxruntime  tokenizers  uvicorn
```

对照 `pyproject.toml`：

| 依赖 | 声明位置 | 说明 |
|---|---|---|
| `jieba` / `httpx` | 核心依赖 | 中文分词（BM25 通道）、HTTP 客户端 |
| `chromadb` | `[vector]` 可选 | 语义通道。**不装也能跑**：检索降级为词法通道（`MemoryCore(vector=False)`） |
| `fastapi` / `uvicorn` | `[proxy]` 可选 | 代理形态 |
| `langgraph` / `langchain-core` | `[agent]` 可选 | Agent 形态编排 |
| `keyring` | 未声明（可选探测） | 有则用作密钥服务通道，无则只用环境变量 |
| `numpy` / `onnxruntime` / `tokenizers` | chromadb 的传递依赖 | 仅显式使用 `onnx:` 嵌入档时需要 |

## 四、判据

```bash
python scripts/audit_deps.py     # 残留全局依赖数为 0 → 退出码 0
```
