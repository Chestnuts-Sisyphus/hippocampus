# Hippocampus

> **以记忆为核心的 agent 运行时** —— 一个记忆核心（`MemoryCore`），两种消费形态。

Hippocampus 把"记忆"做成 agent 的核心能力，而不是外挂的检索库：

- **记忆核心**：四通道检索（语义／关键词／图谱／事件线索）＋ 相关性断崖截断 ＋ 预算装填；
  stable／fluid 分层注入；双轨隔离（模型输出永不注入）；冲突挂起人工确认；生命周期与
  离线固化；安全与漏抽守卫。每条记忆**可查看、可改、可删、可追溯来源**。
- **代理形态**：任何 OpenAI 兼容客户端把 `base_url` 指过来就获得记忆——请求前注入、
  响应后固化、冲突随回复回传确认块。客户端**不改一行代码**。
- **Agent 形态**：LangGraph 编排（think／act／answer）＋ LangChain 工具接入；检索决定
  上下文 → 执行 → 判分决定固化；三出口（完成／无法完成／需人工升级）；轨迹可复演。

对外可插拔只限三点：**记忆后端**、**工具来源**（MCP）、**模型端点**（OpenAI 兼容）。

---

## 快速开始

```bash
# 安装（三选一；尚未发布 PyPI，所以前两条是现在能用的路径）
pip install "hippocampus-memory[vector,proxy] @ git+https://github.com/Chestnuts-Sisyphus/hippocampus"
#   或：把源码目录拷到本机后  pip install -e "/path/to/hippocampus[vector,proxy]"
#   或（不装，只跑）：          PYTHONPATH=/path/to/hippocampus/src python -m hippocampus.cli doctor

hippocampus doctor                     # 体检：数据根／端口／锁／索引／嵌入档
hippocampus seed                       # 灌入示例数据（含已知真值：事实／冲突对／过期项）
hippocampus demo --memories            # 一键跑评测题 + 记忆开/关对照
```

> 项目名是 **Hippocampus**，CLI 与 import 包名同样是 `hippocampus`；
> 只有**分发名**（`pip show` 里那一行）是 `hippocampus-memory`——PyPI 上的
> `hippocampus` 已被第三方占用（同名 memoization 包），见 [docs/naming.md](docs/naming.md)。

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
# 然后把你的 OpenAI 兼容客户端的 base_url 改成 http://127.0.0.1:8765/v1
```

Agent 形态：

```bash
hippocampus chat "帮我挑 5 个适合我的岗位"      # 需要配置模型端点
hippocampus chat --offline "记住：我不看外包"   # 无 key／无网也能跑记忆纪律
```

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
python -m pytest tests/ -q            # 242 条（含随迁的记忆层测试）
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

## 目录结构

```
src/hippocampus/
  core/      记忆核心门面（MemoryCore v1 冻结接口 + 单写者锁）
  memory/    记忆层实现（四通道检索／生命周期／守卫／安全／离线固化）
  proxy/     代理形态（OpenAI 兼容端点）
  agent/     Agent 形态（LangGraph 三节点 + 工具 + 轨迹）
  eval/      评测（题目集／判分／开关对照）
  cli.py     命令行
tests/       随迁测试 + 本项目测试
docs/        接口冻结文档／命名实测／依赖审计／安全说明
```

## 状态

项目处于 alpha：接口 v1 已冻结（只追加字段）。尚未发布的形态见 `CHANGELOG.md`。

## 许可

MIT，见 [LICENSE](LICENSE)。
