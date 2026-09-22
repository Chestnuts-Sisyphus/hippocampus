"""把**真跑出来的** demo 终端输出渲染成 PNG（对外包装 GD1）。

为什么要有这个脚本：README 里的 demo 截图必须是真跑的，不能是画的。所以本脚本自己做三件事——

1. **真跑**（无窗口、零出站、零凭据、写 D:/tmp 或 --tmp-root 指定的临时根）：
   - `scripts/demo_flow.py`：跨会话三形态演示（A1–A4 断言）；
   - `hippocampus doctor` ＋ `seed` ＋ `demo --questions 10 --memories`：离线档评测对照。
2. **只做展示层处理**（不改一个字符）：HTML 转义、按行着色、长行按宽度软换行。
   过滤只用在评测演示上，且**把用到的过滤条件原样写进图里**（图里那行 `grep -vE …`
   就是脚本内部执行的同一条规则），读者可以自己复跑核对。
3. **Edge headless 截图**（`--headless=new`，不弹窗、不抢焦点、独立 user-data-dir，
   不碰用户正在用的浏览器配置）。

跑法：
    python scripts/gen_demo_screenshot.py                  # 默认写 docs/ 与 D:/tmp/hc-gd1-assets
    python scripts/gen_demo_screenshot.py --tmp-root D:/tmp/xxx --only cross-session

产物（仓库内）：
    docs/demo-cross-session.png   跨会话三形态演示（真实 stdout；诊断行走 stderr，图中注明）
    docs/demo-offline-eval.png    离线档 doctor／seed／评测对照（长行软换行）

本脚本不进 CI（截图是仓库内的静态产物，不需要每次 push 重生成）；接线台账见
`tests/test_n53_ci_scripts_parity.py` 的 `CI_EXEMPT`。
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# 评测演示只取输出尾部（图里照写 `tail -n 28`，读者可用同一条管道复现图中的行集合）
TAIL_LINES = 28

# 跨会话演示里剔掉的逐实体消歧行（图里照写这条正则，读者可用同一条 grep 复现图中的行集合）
NOISE_PATTERN = r"^[[:space:]]+\[消歧"
_NOISE_RE = re.compile(r"^[ \t]+\[消歧")

# 终端版式（列数按"半角格"计；CJK 按 2 格估宽，与等宽字体的实际渲染一致到像素级）
MAX_COLS = 148
FONT_PX = 13.0
CHAR_PX = FONT_PX * 0.552      # Consolas 的进宽比
LINE_PX = FONT_PX * 1.42
PAD_PX = 18
BAR_PX = 40

COLOR_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\$ .*$"), "#7ee787"),                    # 命令行整体
    (re.compile(r"^==.*==$"), "#79c0ff"),                    # 分节标题
    (re.compile(r"\[PASS\]"), "#7ee787"),
    (re.compile(r"\[FAIL\]"), "#ff7b72"),
    (re.compile(r"✓"), "#7ee787"),
    (re.compile(r"✗"), "#ff7b72"),
    (re.compile(r"^\[记忆开\].*$"), "#ffd866"),
    (re.compile(r"^\[记忆关\].*$"), "#ffd866"),
    (re.compile(r"^\[标签双来源\].*$"), "#ffd866"),
    (re.compile(r"^边界声明.*$"), "#8b949e"),
    (re.compile(r"^结论：.*$"), "#7ee787"),
    (re.compile(r"^\[shadow\]|^\[消歧|^\[新建"), "#6e7681"),  # 诊断行（仅跨会话图里会出现）
    (re.compile(r"^数据根.*$"), "#79c0ff"),
]

DEFAULT_COLOR = "#e6edf3"


def disp_width(text: str) -> int:
    """等宽终端下的列数（CJK/全角按 2 格）。"""
    width = 0
    for ch in text:
        o = ord(ch)
        wide = (
            0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
            or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
            or 0xFFE0 <= o <= 0xFFE6
        )
        width += 2 if wide else 1
    return width


def soft_wrap(line: str, max_cols: int) -> list[str]:
    """长行软换行（续行缩进 2 格，示意这是折行不是新输出）。"""
    if disp_width(line) <= max_cols:
        return [line]
    out: list[str] = []
    cur, cur_w = "", 0
    limit = max_cols
    for ch in line:
        w = disp_width(ch)
        if cur_w + w > limit and cur:
            out.append(cur)
            cur, cur_w = ch, w
            limit = max_cols - 2  # 后续行留 2 格悬挂缩进
        else:
            cur += ch
            cur_w += w
    if cur:
        out.append(cur)
    for i in range(1, len(out)):
        out[i] = "  " + out[i]
    return out


def colorize(line: str) -> str:
    """按规则整行着色；未命中规则的原样输出。不改动任何字符。"""
    for pat, color in COLOR_RULES:
        if pat.search(line):
            return f'<span style="color:{color}">{html.escape(line)}</span>'
    return f'<span style="color:{DEFAULT_COLOR}">{html.escape(line)}</span>'


def render_html(lines: list[str], title: str) -> tuple[str, int, int]:
    wrapped: list[str] = []
    for ln in lines:
        wrapped.extend(soft_wrap(ln.rstrip(), MAX_COLS))
    cols = max((disp_width(ln) for ln in wrapped), default=60)
    cols = max(cols, disp_width(title) + 8)

    body = "\n".join(colorize(ln) for ln in wrapped)
    width = int(cols * CHAR_PX) + PAD_PX * 2
    height = int(len(wrapped) * LINE_PX) + PAD_PX * 2 + BAR_PX
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body style="margin:0;background:#010409">
  <div style="width:{width}px;height:{height}px;background:#0d1117;box-sizing:border-box;
              border-radius:8px;overflow:hidden;border:1px solid #21262d">
    <div style="height:{BAR_PX}px;background:#161b22;border-bottom:1px solid #21262d;
                box-sizing:border-box;padding:0 14px">
      <span style="display:inline-block;width:11px;height:11px;border-radius:50%;
                   background:#ff5f56;margin-right:7px;vertical-align:middle;position:relative;top:{BAR_PX // 2 - 6}px"></span>
      <span style="display:inline-block;width:11px;height:11px;border-radius:50%;
                   background:#ffbd2e;margin-right:7px;vertical-align:middle;position:relative;top:{BAR_PX // 2 - 6}px"></span>
      <span style="display:inline-block;width:11px;height:11px;border-radius:50%;
                   background:#27c93f;margin-right:14px;vertical-align:middle;position:relative;top:{BAR_PX // 2 - 6}px"></span>
      <span style="color:#8b949e;font:400 12px Consolas,'Microsoft YaHei',monospace;
                   vertical-align:middle;position:relative;top:{BAR_PX // 2 - 8}px">{html.escape(title)}</span>
    </div>
    <pre style="margin:0;padding:{PAD_PX}px;color:{DEFAULT_COLOR};
                font:{FONT_PX:g}px/{LINE_PX:.1f}px Consolas,'Cascadia Mono','Microsoft YaHei',monospace;
                white-space:pre">{body}</pre>
  </div>
</body></html>
"""
    return doc, width, height


def _edge_bin() -> str:
    """定位 Edge（本机与 CI 都不联网、不弹窗）。可用 EDGE_BIN 覆盖。"""
    env = os.environ.get("EDGE_BIN")
    if env and Path(env).exists():
        return env
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/microsoft-edge",
        "/usr/bin/google-chrome",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    raise SystemExit("找不到 Edge／Chrome，可用 EDGE_BIN=<绝对路径> 覆盖。")


def screenshot(lines: list[str], title: str, out_png: Path, tmp: Path) -> None:
    doc, width, height = render_html(lines, title)
    tmp.mkdir(parents=True, exist_ok=True)
    html_path = tmp / (out_png.stem + ".html")
    html_path.write_text(doc, encoding="utf-8", newline="\n")
    profile = tmp / ("edge-profile-" + out_png.stem)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _edge_bin(),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--force-device-scale-factor=1",
        f"--user-data-dir={profile}",
        f"--screenshot={out_png}",
        f"--window-size={width},{height}",
        html_path.as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not out_png.exists():
        raise SystemExit(f"截图失败（rc={proc.returncode}）：{proc.stderr[-500:]}")
    print(f"写入 {out_png}（{out_png.stat().st_size} B，{width}x{height}）")


def _run(cmd: list[str], env: dict[str, str]) -> tuple[str, str, int]:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(ROOT), timeout=900)
    return proc.stdout, proc.stderr, proc.returncode


def _base_env(tmp_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["HIPPOCAMPUS_OFFLINE"] = "1"
    for k in ("HIPPOCAMPUS_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(k, None)
    env["HIPPOCAMPUS_HOME"] = str(tmp_root / "quickstart")
    return env


def cross_session(tmp_root: Path) -> list[str]:
    """跨会话三形态演示：真跑 scripts/demo_flow.py，取 stdout（逐通道断崖／shadow 日志在 stderr）。"""
    home = tmp_root / "cross-session"
    env = _base_env(tmp_root)
    out, err, rc = _run(
        [sys.executable, "-X", "utf8", "scripts/demo_flow.py", "--home", str(home), "--port", "8821"],
        env,
    )
    if rc != 0:
        raise SystemExit(f"demo_flow 失败（rc={rc}）：\n{out[-1500:]}\n{err[-1500:]}")
    raw = out.splitlines()
    kept = [ln for ln in raw if not _NOISE_RE.match(ln)]
    lines = [f"$ python scripts/demo_flow.py --home {home.as_posix()} --port 8821 "
             f"2>stderr.log | grep -vE '{NOISE_PATTERN}'"]
    lines += kept
    lines.append("")
    lines.append(f"（stdout 原文 {len(raw)} 行，按图里那条 grep 剔掉 {len(raw) - len(kept)} 行逐实体消歧行；"
                 f"stderr 另有 {len(err.splitlines())} 行逐通道断崖／shadow 观察轨日志，本例未展示）")
    lines.append("（退出码 0 = A1–A4 全部断言通过）")
    return lines


def offline_eval(tmp_root: Path) -> list[str]:
    """离线档：doctor → seed → 评测对照（评测部分＝图上那条 `tail -n 28` 的产物）。"""
    env = _base_env(tmp_root)
    home = Path(env["HIPPOCAMPUS_HOME"])
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)
    py = [sys.executable, "-X", "utf8", "-m", "hippocampus.cli"]
    lines = [
        f"$ export HIPPOCAMPUS_OFFLINE=1 HIPPOCAMPUS_HOME={home.as_posix()}",
    ]
    for argv in (["doctor"], ["--account", "ci", "seed"]):
        out, _err, rc = _run(py + argv, env)
        if rc != 0:
            raise SystemExit(f"hippocampus {' '.join(argv)} 失败（rc={rc}）")
        lines.append("$ hippocampus " + " ".join(argv))
        lines += out.strip("\n").splitlines()
        lines.append("")
    demo_argv = ["--account", "ci", "demo", "--questions", "10", "--memories"]
    out, _err, rc = _run(py + demo_argv, env)
    if rc != 0:
        raise SystemExit(f"hippocampus demo 失败（rc={rc}）")
    raw = out.splitlines()
    kept = raw[-TAIL_LINES:]
    if not kept or not kept[0].startswith("[记忆开]"):
        raise SystemExit(f"demo 输出尾部形状变了（首行 {kept[0][:40]!r}），先改 TAIL_LINES 再出图")
    lines.append("$ hippocampus " + " ".join(demo_argv) + f" | tail -n {TAIL_LINES}")
    lines += kept
    lines.append("")
    lines.append(f"（上一条命令真跑了 {len(raw)} 行：前面 {len(raw) - len(kept)} 行是逐实体的消歧／新建／"
                 f"检索断崖诊断行，按图里那条 tail 截掉；保留部分是原文，一个字符未改）")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真跑 demo 并渲染成 README 用终端截图")
    ap.add_argument("--tmp-root", default="D:/tmp/hc-gd1-assets", help="临时数据根（默认 D:/tmp/hc-gd1-assets）")
    ap.add_argument("--out-dir", default=str(DOCS), help="PNG 输出目录（默认 docs/）")
    ap.add_argument("--only", choices=["cross-session", "offline-eval"], help="只跑其中一个")
    args = ap.parse_args(argv)

    tmp_root = Path(args.tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out_dir)
    work = tmp_root / "_render"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)

    stamp = time.strftime("%Y-%m-%d")
    if args.only in (None, "cross-session"):
        lines = cross_session(tmp_root)
        screenshot(lines, f"demo_flow.py — 跨会话记忆 · 三形态（{stamp}）",
                   out_dir / "demo-cross-session.png", work)
    if args.only in (None, "offline-eval"):
        lines = offline_eval(tmp_root)
        screenshot(lines, f"hippocampus doctor / seed / demo — 离线档（{stamp}）",
                   out_dir / "demo-offline-eval.png", work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
