# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)；
`MemoryCore` 接口自 v1 起**只允许追加字段**（见 `docs/memory-core-v1.md`）。

## [0.1.0] — 2026-09-17

首个可运行版本（alpha）。记忆核心取自既有实现的抽取式移植（机制未简化），
本版本的工作集中在**接口化、去耦合与可验证**。

### 新增

- **`MemoryCore` v1 冻结接口**：`write` / `search` / `inject_finalize` / `confirm` /
  `consolidate`（scope 为第一参数；不含任何 HTTP 概念；只允许追加字段）。
  自动检查：`scripts/check_interface.py`。
- **代理形态**：OpenAI 兼容端点（默认 `127.0.0.1:8765`）——任何兼容客户端改 `base_url`
  即获得记忆。确认块由**记忆层生成、代理追加**（可关，`确认 n`／`否决` 在入口消费）。
- **Agent 形态**：LangGraph 三节点（think / act / answer）＋ LangChain 工具接入；
  三出口（完成／无法完成／需人工升级）；轨迹可复演（`replay` 两次指纹一致）。
- **命令行**：`doctor` / `seed` / `demo` / `proxy` / `chat` / `replay` / `explain` /
  `memory list|pending|candidates|update|delete|off|on|scopes` / `learning off|on`。
- **离线档**：无 key／无网可跑记忆纪律；新增**句式规则抽取**与**规则冲突检测**
  （原先两者都依赖模型，离线会断链）。
- **内置嵌入档 `builtin-hash`**（默认，零下载、确定性）＋ 推荐档 `onnx:bge-small-zh-v1.5`
  可选；新增按档标定表 `memory/calibration.py` 与标定脚本 `scripts/calibrate.py`。
- **单写者锁**（库目录级，含僵尸锁回收）与 `doctor --unlock`。
- **出站 URL 校验**（仅 http/https；拒环回／私有／保留／元数据地址，含 IPv6 与
  十进制／十六进制写法）。
- **评测最小版**：10 题（7 问 + 3 动作）＋ 记忆开/关对照 ＋ 标签双来源一致率 ＋ 边界声明。

### 修复

- **P1（数据丢失级）**：语义去重命中后，机械判据拿不准时会**静默丢弃**新句（用户改口不生效）。
  现在三条路——`supersede`（确认值变更）／`admit`（同构换值，两条都留）／
  **挂起确认**（判据不确定时新条以候选入库，旧值继续生效）——**任何一条都不丢数据**。
- **离线断链**：无模型时"改口 → 挂起确认"整条链曾不可用，现由规则冲突检测承接。
- **生命周期与固化**的"当前时间"参数化（演示可用压缩时间跑同一代码路径）。

### 变更（相对移植来源）

- 去代理形态耦合：记忆语义入口收进 `MemoryCore`，HTTP 形状只留在代理层。
- 去 Windows 专有：删除 DPAPI 加密；凭据只从环境变量／密钥服务读取（**不落盘**）。
- 去个人数据：不再有账号体系；示例数据由 `hippocampus seed` 生成（合成、含已知真值）。
- 数据根与 scope 解析改为显式注入（前身为模块级全局单例，多 scope 并行会串库）。

### 已知限制

- **并发写不保证**：同一记忆库目录同时只有一个写者（第二个排队或被告知）。
- 内置嵌入档是**词法级**表征，同义改写召回弱于神经档；边界写在 `docs/embedding.md`。
- 评测为最小版（10 题／合成数据），只证方法可复现，不构成效果结论。
- 尚未发布到 PyPI（安装以源码／GitHub 直装为准）。
