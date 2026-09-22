# 参与贡献（Contributing）

> **English TL;DR** — Docs, code comments and commit messages in this repo are **Chinese-first** (that is a
> deliberate choice, not an accident). Run `python -m pytest tests/ -q` and `ruff check src tests scripts`
> before opening a PR, keep everything **offline-runnable with zero credentials** (that is exactly how CI
> runs), never hard-code paths that live outside this repository into tracked files, and always attach a
> reproducible command + real output for any number you claim. Questions are welcome as issues.

本文件写给"想改这个仓库"的人（包括未来的我自己）。它只写**动代码前必须知道的事**：
跑什么、怎么验、哪些红线一碰就废。

---

## 0. 先在脑子里装上这四条（能省下好几轮返工）

| 规矩 | 具体含义 | 为什么 |
|---|---|---|
| **可验证 > 功能多** | 新增能力必须同时给出可复跑的命令与真实输出；mock／近似／假设值必须显式标注，不许混进实测口径 | 仓库里最大的资产是"说的每一句都能当场跑出来"。一个漂亮但验不了的数字会让全部数字一起贬值 |
| **离线优先** | 无 key、无网时默认路径绝不发出站请求；要模型的能力只能挂在显式开关后面（例如官方判分臂的 `--model-arm`） | CI 每次 push 都在"什么都没配"的状态下跑全部测试——任何偷偷依赖模型／网络的代码路径都会在那里红掉 |
| **公开面纪律** | 提交进仓库的东西（含文档、测试、注释、截图）里：**零凭据字面量**、**不出现仓外正本／个人目录路径**、示例数据必须是合成的 | 仓库是公开的。"第几行是什么"和"正本在我哪个目录"本身就是泄露面 |
| **只增不删** | 记忆层：改 = `supersede`（旧条原地保留），删 = 归档；文档口径同理——纠正一处数字要**同时**改所有引用它的地方 | 能被悄悄改掉的记忆没法审计；能被悄悄改掉的口径没法信任 |

---

## 1. 本地跑起来

```bash
git clone https://github.com/Chestnuts-Sisyphus/hippocampus
cd hippocampus

# 依赖：核心（记忆核心 + Agent 形态必需）／dev（测试与 lint）／vector（语义通道，chromadb）／proxy（三种入站协议）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev,vector,proxy]"   # Windows
# source .venv/bin/activate && pip install -e ".[dev,vector,proxy]"  # Linux / macOS

hippocampus doctor        # 体检：数据根 / 端口 / 写锁 / 索引 / 嵌入档 / 是否离线
hippocampus seed          # 灌入**合成**示例数据（含已知真值：冲突对、过期项、观察轨样本）
hippocampus demo --memories
```

只装核心（不装 `[vector]`）也是**受支持的一档**：语义通道按设计降级关闭，BM25／图／事件通道照常，
`doctor` 会明说当前档位。CI 里有一个专门的任务（`core-only`）守这条降级路径。

> 数据根默认在用户目录下；做实验请显式指定，别污染默认库：
> `export HIPPOCAMPUS_HOME=D:/tmp/hc-dev`（或 `hippocampus --home D:/tmp/hc-dev …`）。

---

## 2. 改完必须跑的闸（顺序即优先级）

```bash
# ① 全量测试（Windows 上 C 盘紧时务必给临时目录，公用的 D:/tmp/pt 会被并发会话撞锁）
python -m pytest tests/ -q --basetemp=D:/tmp/pt-<你的后缀>

# ② lint（CI 的口径就是这一条；基线 0 error，新增告警会被打回）
ruff check src tests scripts

# ③ 公开面三条闸（改过文档／测试／配置就跑，源码改动也建议跑）
python scripts/check_interface.py        # MemoryCore v1 契约：只追加、scope 第一参数、无 HTTP 字段
python scripts/scan_credentials.py       # 凭据字面量：零命中
python scripts/scan_public_leak.py       # 仓外正本路径／行号指针／本机账号路径：零命中（CI 另跑 --git-text）
python scripts/scan_personal_data.py --home <你的示例库根>   # 示例库无真实个人数据

# ④ 改了架构相关代码 → 图必须跟着更新（生成脚本会先校验锚点，对不上就拒绝出图）
python scripts/gen_architecture_svg.py
```

`scripts/` 下**每个脚本要么在 CI 里被引用，要么在 `tests/test_n53_ci_scripts_parity.py` 的
`CI_EXEMPT` 台账里写明"为什么不接 CI"**。新增脚本忘了登记，这个测试会红——这是设计好的提醒。

### 测试纪律

- **不许为了让测试变绿而改断言。** 仓库里有一批"植入即红"的对抗性测试（planted-failure）：
  它们故意构造应该失败的情形，验证守卫真的会拦。改断言就等于把守卫拆了。
- `xfail` 是 **strict** 的（`xfail_strict=true`）：写 `xfail` 的问题一旦真的修好了，测试会红，
  逼你回来把它改成正式断言。
- 根目录／工具脚本**顶层不许有副作用**（起进程、联网、写文件一律包进 `if __name__ == "__main__":`），
  文件名不许用 `test_*.py`／`*_test.py`——否则 pytest 会把工具脚本当成测试收集，历史上有过事故。
- 测试必须能在**无网、无凭据**下跑完。需要外部付费产物（例如官方判分批次报告）的验证项，
  用环境变量注入路径，未配置就 `pytest.skip`（示例：`tests/test_n23_t3_small_fixes.py`）。

---

## 3. 提交规范

```
<type>(<scope>): <轮次或缺口编号> —— <中文描述>
```

- **type**：`feat` / `fix` / `docs` / `test` / `refactor` / `chore` / `perf` / `build` / `ci`（Conventional Commits）。
- **scope**：常用 `core`（记忆核心）／`cli`／`scripts`／`proxy`／`agent`／`eval`／`docs`。
- **描述用中文**：本仓库既有习惯，不改成英文。
- **带编号**：能对上任务／缺口编号就写上（形如"十轮 X16"、"八轮 V3"），便于回溯"这行为什么这么写"。
- 实测样例：

  ```
  fix(core): 六轮 G6 —— CLI help 行修 --candidates 漂移；psutil 进 dev extras＋脚本缺装降级
  ```

- **只按文件名精确 `git add`**，不要 `git add .` / `git add -A`（这条是为了让每个提交都能说清"为什么改了这几行"）。
- 分支 `main` **已开分支保护**，CI 六个检查（三任务的矩阵展开）是必需状态：
  `full (ubuntu-latest, 3.11)`、`full (ubuntu-latest, 3.12)`、`full (windows-latest, 3.11)`、
  `full (windows-latest, 3.12)`、`core-only`、`lint`。贡献者请走 PR；仓库管理员本次规则下仍可直接推送。
  查当前规则：`gh api repos/Chestnuts-Sisyphus/hippocampus/branches/main/protection`。

---

## 4. 文档、图、截图

- `README.md` 是英文（对外第一屏），`README.zh-CN.md` 是中文全量版——**改一处要同时改两份**，
  不然两份口径会在几天内分叉。
- `docs/` 下的文档以中文为主。**任何数字都要带出处**：要么是仓内可复跑的命令，要么标注它是外部
  付费批次的产物并给出复跑口径。数字的同步清单见 `docs/release-sync.md`。
- **架构图是代码生成的**：`python scripts/gen_architecture_svg.py` → `docs/architecture.svg`、
  `docs/memory-lifecycle.svg`。图里每个方框右下角都写着 `文件:行号`，生成前逐条校验（对不上拒绝出图）。
  手改 SVG 会在下一次生成时被覆盖，所以**改图请改生成脚本**。
- **demo 截图是真跑出来的**：`python scripts/gen_demo_screenshot.py` 会真跑
  `scripts/demo_flow.py` 与 `hippocampus doctor/seed/demo`，把输出渲染成 PNG（无窗口 Edge headless）。
  图上标了它剔除了哪些诊断行（并写出对应的 `tail`／`grep`），**读者可以用同一条管道复现图中的行集合**。
  不要手工截图后修图——一张改过的截图会让整套"可验证"的说法失效。
- `.github/workflows/*.yml` 里**新增或改动的注释请用英文**：这是本仓库"一律中文"的唯一例外
  （仓内约定，见 `AGENTS.md`）。
- **发布链**：推 `v*` tag 触发 `.github/workflows/release.yml`（构建 → 装 wheel 冒烟 → 挂到 Release）。
  PyPI 发布默认关闭，开启方式写在该 workflow 的注释里。
- **分支保护的维护者操作**（规则由 `gh api` 设置，不是手点的，所以记在这里）：

  ```bash
  # 看当前规则（六个必需检查 / 是否要求人工批准 / 是否允许强推）
  gh api repos/Chestnuts-Sisyphus/hippocampus/branches/main/protection

  # 临时解绑（例如需要强推修复时）：删掉规则 → 干完再加回来
  gh api -X DELETE repos/Chestnuts-Sisyphus/hippocampus/branches/main/protection

  # 重新加上（把 contexts 换成当前 CI 的 job 名；下面这份 JSON 是实测跑通的形式）
  cat > /tmp/protection.json <<'JSON'
  {
    "required_status_checks": {
      "strict": false,
      "contexts": ["full (ubuntu-latest, 3.11)", "full (ubuntu-latest, 3.12)",
                   "full (windows-latest, 3.11)", "full (windows-latest, 3.12)",
                   "core-only", "lint"]
    },
    "enforce_admins": false,
    "required_pull_request_reviews": null,
    "restrictions": null,
    "allow_force_pushes": false,
    "allow_deletions": false
  }
  JSON
  gh api -X PUT repos/Chestnuts-Sisyphus/hippocampus/branches/main/protection --input /tmp/protection.json
  ```

  `enforce_admins=false` 是**故意的**：单维护者仓库需要保留"直接推 main"的能力，
  必需检查只约束走 PR 的贡献者。规则不要求人工批准（一个人没法给自己批准）。

---

## 5. 红线（碰了就不是"小问题"）

1. **凭据**：源码、测试、文档、示例配置里不许出现真实或可用的 key／token／密码。凭据只从环境变量或
   系统密钥服务读取；日志里疑似凭据的字段会被拒绝加载。
2. **仓外路径**：不把仓外的正本目录、登记簿路径、个人目录写进 tracked 文件（含测试与文档）。
   需要读外部输入时用环境变量注入。
3. **示例数据**：示例库只能由 `hippocampus seed` 生成合成数据；不许把真实记忆、真实 JD 原文、
   真实个人信息灌进示例库或截图。
4. **数据安全**：`meta` 库与 `accounts` 相关 schema 不做破坏性改动；记忆只增不删（改走 supersede，
   删走归档），全仓不出现物理删除记忆的语句。
5. **隐私默认值**：代理与管理口默认只绑环回，出站默认仅 https 且拒绝环回／私有／保留地址；
   要放宽必须走显式开关，不允许改默认值。

---

## 6. 发布（维护者流程）

```bash
# 1) 版本号与 CHANGELOG 同步、口径三件套对齐（docs/release-sync.md 的清单逐条过）
# 2) 打 tag 并推送
git tag v0.5.1 && git push origin v0.5.1
```

推 tag 会触发 `.github/workflows/release.yml`：装依赖 → 构建 sdist/wheel → **用构建出来的 wheel
装一遍并跑一次 `hippocampus doctor` 冒烟** → 上传构建产物到 Actions → 用 `gh release` 把产物挂到
同名 Release 上（Release 已存在则只补传产物）。

- **目前没有发布到 PyPI**（README 里也这么写）。分发名已定：`hippocampus-agent`
  （裸名 `hippocampus` 在 PyPI 上属于第三方，别用）。要开启 PyPI 发布的话，推荐走
  **trusted publishing**（免 token）：在 PyPI 侧登记本仓库与本 workflow 名，然后给 `release.yml`
  加一个 `pypi` job（`permissions: id-token: write` + `pypa/gh-action-pypi-publish`）——
  具体开关位置写在 `release.yml` 的注释里，**默认关闭**，因为那是对外动作，需要维护者明确决定。
