# 命名资源实测（T1 · 2026-09-17 · 含当日改名）

> 依据：交接正本 §4-T1 ／ 方案 v2 §十九-1（命名资源实测前置）。
> **2026-09-17 更新**：栗子要求项目名去掉 `-agent` 后缀，直接叫 **Hippocampus**——
> 下表按改名后的实际状态重测。

## 一、四项实测结果

| # | 资源 | 实测命令 | 结果 | 结论 |
|---|---|---|---|---|
| 1 | **项目名 / 仓库名** | `gh repo view Chestnuts-Sisyphus/hippocampus` | `200`（PUBLIC） | **`Chestnuts-Sisyphus/hippocampus` 已启用**（由 `hippocampus-agent` 改名而来；GitHub 对旧 URL 自动重定向） |
| 1b | 改名前置条件 | `gh api repos/Chestnuts-Sisyphus/Hippocampus` | 存在但 `size=0KB`、**零提交**（2026-08-08 建的占位仓） | 已改名为 `hippocampus-placeholder` 让出名字（空仓，可随时删除） |
| 2 | **CLI 命令名** | `hippocampus --help`（本机 `Scripts/` 无同名可执行文件） | 无冲突 | **`hippocampus` 可用** |
| 3 | **import 包名** | `import hippocampus` | 正常 | **`hippocampus` 可用** |
| 4 | **默认端口** | `netstat -ano \| grep :8765` | 无监听 | **8765 空闲** |
| 5 | **PyPI 分发名** | `curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/hippocampus/json` | `200` | ❌ `hippocampus` **被第三方占用**（Pascal Jürgens 的 sqlite memoization 包，v0.1.4）；`hippocampus-agent`/`hippocampus-memory` 均为 `404` 可用 |

## 二、定案（改名后）

| 面 | 取值 | 说明 |
|---|---|---|
| 项目名 | **Hippocampus** | README／文档／简历／GitHub 描述统一用它 |
| GitHub 仓库 | **`Chestnuts-Sisyphus/hippocampus`** | 旧地址 `…/hippocampus-agent` 由 GitHub 自动重定向 |
| CLI 命令 | `hippocampus` | `[project.scripts]` 不变 |
| import 包名 | `hippocampus` | 不变 |
| 默认端口 | `8765` | 写入 `config.py` 的 `DEFAULT_PORT`，由 `hippocampus doctor` 打印实际值 |
| **PyPI 分发名** | **`hippocampus-memory`** | 唯一"被迫不同"的一处：PyPI 的 `hippocampus` 是别人的包；分发名只在 `pip install` / `pip show` 出现（PyPI 发布本身列投递后可选） |

> 为什么不用 `hippocampus-agent` 继续当分发名：栗子明确不喜欢 `-agent` 后缀，
> 而分发名会在 `pip show` 里露脸；`hippocampus-memory` 既避开被占名，也与"记忆是核心"一致。

## 三、复跑

```bash
# 仓库与旧地址重定向
curl -s -o /dev/null -w "new repo: %{http_code}\n" https://github.com/Chestnuts-Sisyphus/hippocampus
curl -s -o /dev/null -w "old repo: %{http_code}（301/302=已重定向）\n" -I https://github.com/Chestnuts-Sisyphus/hippocampus-agent

# PyPI
curl -s -o /dev/null -w "pypi hippocampus:        %{http_code}（200=被占）\n" https://pypi.org/pypi/hippocampus/json
curl -s -o /dev/null -w "pypi hippocampus-memory: %{http_code}（404=可用）\n" https://pypi.org/pypi/hippocampus-memory/json

# 端口
netstat -ano | grep -E ":8765\b" || echo "8765 free"
```

> 端口实测为本机口径（`netstat` 只看本机）；换机复跑同法。
