# 命名资源实测（T1 · 2026-09-17）

> 依据：交接正本 §4-T1 ／ 方案 v2 §十九-1（命名资源实测前置）。四条实测命令与结论如下。

| # | 资源 | 实测命令 | 结果 | 结论 |
|---|---|---|---|---|
| 1 | PyPI 分发包名 | `curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/hippocampus-agent/json` | `404` | **`hippocampus-agent` 可用** |
| 1b | PyPI 分发包名（对照） | `curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/hippocampus/json` | `200` | `hippocampus` **已被占用**（故分发包名不取裸名） |
| 2 | CLI 命令名 | `hippocampus --help`（本机 `Scripts/` 无同名可执行文件） | 无冲突 | **`hippocampus` 可用** |
| 3 | GitHub 仓库名 | `curl -s -o /dev/null -w "%{http_code}" https://api.github.com/repos/<owner>/hippocampus-agent` | `404` | **`hippocampus-agent` 可用**（同名近似仓库 2 个：`agentic-hippocampus`、`hippocampus-agent-memory`，均非同名） |
| 4 | 默认端口 | `netstat -ano \| grep :8765` | 无监听 | **8765 空闲**（8651 亦空闲，但 8651 为私有记忆代理占用历史端口，按方案定案用 8765） |

## 定案

- **PyPI 分发包名**：`hippocampus-agent`（`hippocampus` 已被占用；**PyPI 发布本身列投递后可选**，此处只保证名字不被占用）
- **import 包名**：`hippocampus`
- **CLI 命令名**：`hippocampus`
- **GitHub 仓库名**：`hippocampus-agent`
- **默认端口**：`8765`（写入 `hippocampus/config.py` 的 `DEFAULT_PORT`，由 `hippocampus doctor` 打印实际值）

## 复跑

```bash
curl -s -o /dev/null -w "pypi hippocampus-agent: %{http_code}\n" https://pypi.org/pypi/hippocampus-agent/json
curl -s -o /dev/null -w "pypi hippocampus:       %{http_code}\n" https://pypi.org/pypi/hippocampus/json
curl -s -o /dev/null -w "github repo:            %{http_code}\n" https://api.github.com/repos/<owner>/hippocampus-agent
netstat -ano | grep -E ":8765\b" || echo "8765 free"
```

> 端口实测为本机口径（`netstat` 只看本机）；换机复跑同法。
