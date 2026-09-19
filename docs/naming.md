# 命名资源实测（T1 · 2026-09-17 · 含当日改名）

> 依据：交接正本 §4-T1 ／ 方案 v2 §十九-1（命名资源实测前置）。
> **2026-09-17 更新**：栗子要求项目名去掉 `-agent` 后缀，直接叫 **Hippocampus**——
> 下表按改名后的实际状态重测。

## 一、四项实测结果

| # | 资源 | 实测命令 | 结果 | 结论 |
|---|---|---|---|---|
| 1 | **项目名 / 仓库名** | `gh repo view Chestnuts-Sisyphus/hippocampus` | `200`（PUBLIC） | **`Chestnuts-Sisyphus/hippocampus` 已启用**（由 `hippocampus-agent` 改名而来；GitHub 对旧 URL 自动重定向） |
| 1b | 改名前置条件 | `gh api repos/Chestnuts-Sisyphus/Hippocampus` | 存在但 `size=0KB`、**零提交**（2026-08-08 建的占位仓） | 该空占位仓**已按栗子指示删除**（删除前复核：size=0／无分支／无提交；删除返回 204） |
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
| **PyPI 分发名** | **`hippocampus-agent`**（栗子指定） | 唯一"被迫不能取裸名"的一处：PyPI 的 `hippocampus` 是别人的包（实测见上表 5 号条目）。分发名只在 `pip install` / `pip show` 出现，项目名／仓库／CLI／import 包名都还是 Hippocampus |

> **为什么分发名不能是裸 `hippocampus`**：那是第三方的 sqlite memoization 包。
> 若把分发名写成 `hippocampus`，读者照 README 敲 `pip install hippocampus` 会装到别人的东西——
> 这是真实踩坑，所以分发名取 `hippocampus-agent`。

## 三、复跑

```bash
# 仓库与旧地址重定向
curl -s -o /dev/null -w "new repo: %{http_code}\n" https://github.com/Chestnuts-Sisyphus/hippocampus
curl -s -o /dev/null -w "old repo: %{http_code}（301/302=已重定向）\n" -I https://github.com/Chestnuts-Sisyphus/hippocampus-agent

# PyPI
curl -s -o /dev/null -w "pypi hippocampus:        %{http_code}（200=被占，故分发名不能取裸名）\n" https://pypi.org/pypi/hippocampus/json
curl -s -o /dev/null -w "pypi hippocampus-agent:  %{http_code}（404=可用，本项目分发名）\n" https://pypi.org/pypi/hippocampus-agent/json

# 端口
netstat -ano | grep -E ":8765\b" || echo "8765 free"
```

> 端口实测为本机口径（`netstat` 只看本机）；换机复跑同法。

## 四、术语与代码列对照（A8/A9 登记，2026-09-18）

代码列名 **不等于** 用户可见口径，这里登记映射，避免文档与代码各说各话：

| 代码里 | 正本/用户口径 | 说明 |
|---|---|---|
| `memories.shadow=1` / 库内 `shadow` 列 | **观察轨（模型侧）** | A8：确认轨/非确认轨是双轨，模型输出不走正式记忆，`shadow=1` 是"观察轨内部实现"列名。模型输出提取的条目永不注入，确认后经 `promote_shadow` 晋升为正式记忆 |
| `entities.is_hub`（hub_guard 标记） | mega-hub 诊断标记 | A9 结论：**检索侧不消费**——按前身设计，`is_hub` 只是维护扫描的"报告/分流建议"标记；检索侧的遍历上限由 `retrieval.py` 的 `MAX_GRAPH_PER_ENTITY` 保障（写入/检索两道防线在各自环节），hub 标记不改变检索行为（有测试钉住：`tests/test_n23_t3_small_fixes.py::test_hub_flag_does_not_filter_retrieval`） |
| `status='candidate'` | 待裁决候选 | 写入时与旧值冲突→挂起，裁决前不参与注入/去重；计数视图＝`core.candidate_audit`（A12） |
