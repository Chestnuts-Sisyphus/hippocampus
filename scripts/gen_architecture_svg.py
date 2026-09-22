"""生成 `docs/architecture.svg` 与 `docs/memory-lifecycle.svg`（对外包装 GD1，零依赖、纯离线）。

为什么用生成而不是手绘：面试官会拿图对着代码看。手绘 SVG 改一行代码就可能与实现脱节，
而"图与代码不一致"比没有图更糟。所以本脚本把**每个方框的 file:line 依据**写成
`ANCHORS` 表，生成前逐条校验「该文件的该行确实含该 token」——任何一条对不上就**拒绝生成**
（退出码 1），不会悄悄产出一张过期的图。

跑法：
    python scripts/gen_architecture_svg.py            # 校验锚点并写两个 SVG
    python scripts/gen_architecture_svg.py --check    # 只校验锚点，不写盘（CI/本地自检用）

产物是纯图形 + 文本（无 `<style>`、无脚本、无外链、无字体请求），因此 GitHub 的 SVG
沙箱可以直接渲染；文字用系统字体族兜底（Windows/macOS/Linux 都能出字）。

边界（与仓库既有口径一致，图上照写）：
- 默认嵌入档是 384 维**词法哈希**（`builtin-hash`），不是神经语义；神经档需显式切 `onnx:bge-small-zh-v1.5`。
- 三通道打分量纲不同，图里标了「禁跨通道比分数」这条实现纪律。
- 观察轨（模型输出）永不静默注入；晋升只能由人工触发。

本脚本不进 CI（产物是仓库内的静态 SVG，不需要每次 push 重新生成）；接线台账见
`tests/test_n53_ci_scripts_parity.py` 的 `CI_EXEMPT`。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# ---------------------------------------------------------------- 锚点台账（唯一的"图↔码"契约）

# key -> (仓内路径, 行号（1 基）, 该行必须包含的 token)
# 行号口径：**当前工作树**的 HEAD 行；源文件被重排时本脚本会在生成前报错，逼你回来改图。
ANCHORS: dict[str, tuple[str, int, str]] = {
    # 门面与类型
    "memcore": ("src/hippocampus/core/core.py", 137, "class MemoryCore"),
    "scope": ("src/hippocampus/core/types.py", 21, "class Scope"),
    "kinds": ("src/hippocampus/core/types.py", 15, "MemoryKind"),
    "source": ("src/hippocampus/core/types.py", 17, "Source"),
    "inject_asm": ("src/hippocampus/core/core.py", 717, "stable"),
    # 写入侧三轨
    "tracks_note": ("src/hippocampus/core/core.py", 877, "fire_track_a"),
    "track_a": ("src/hippocampus/memory/memory_bridge.py", 1101, "def fire_track_a"),
    "track_b": ("src/hippocampus/memory/memory_bridge.py", 1050, "def after_response"),
    "obs_note": ("src/hippocampus/core/core.py", 880, "shadow=1"),
    # 存储
    "db": ("src/hippocampus/memory/database.py", 42, "CREATE TABLE IF NOT EXISTS memories"),
    "rel_types": ("src/hippocampus/memory/database.py", 86, "SUPERSEDES"),
    "pending": ("src/hippocampus/memory/database.py", 462, "pending_blocks"),
    "supersede_fn": ("src/hippocampus/memory/database.py", 653, "def supersede_memory"),
    "axis_status": ("src/hippocampus/memory/database.py", 45, "superseded"),
    "axis_life": ("src/hippocampus/memory/database.py", 46, "lifecycle"),
    "life_rule": ("src/hippocampus/memory/lifecycle.py", 12, "正交"),
    "axis_shadow": ("src/hippocampus/memory/database.py", 57, "shadow"),
    "confirm_usage": ("src/hippocampus/memory/confirm.py", 34, "否决"),
    "confirm_block": ("src/hippocampus/memory/confirm.py", 44, "def build_confirm_block"),
    "conflict_rule": ("src/hippocampus/memory/conflict.py", 171, "def detect_rule_conflicts"),
    "col_mem": ("src/hippocampus/memory/retrieval.py", 48, "hippocampus_mem"),
    "col_ep": ("src/hippocampus/memory/retrieval.py", 49, "hippocampus_ep"),
    "embed": ("src/hippocampus/memory/builtin_embedding.py", 25, "DIM"),
    "cfg": ("src/hippocampus/memory/config.py", 49, "builtin-hash"),
    # 检索与融合
    # ⚠️ 指向**真管线**：`fuse` 自称 DEPRECATED、仅供 console debugRetrieve 展示，
    #    主流程排序已由 retrieve 内的区块管线取代（`_apply_budget` 按区块顺序装填）。
    #    拿 fuse 当融合实现讲会被追问即穿（见 docs/memory-pipeline.md 的同一更正）。
    "fuse": ("src/hippocampus/memory/retrieval.py", 1125, "def _apply_budget"),
    "order": ("src/hippocampus/memory/retrieval.py", 10, "禁跨通道比分数"),
    "semantic": ("src/hippocampus/memory/retrieval.py", 761, "def semantic_search"),
    "episode": ("src/hippocampus/memory/retrieval.py", 1201, "def episode_clue_search"),
    "lexical": ("src/hippocampus/memory/retrieval.py", 944, "def bm25_search"),
    "graph_search": ("src/hippocampus/memory/retrieval.py", 971, "def graph_search"),
    # 双形态入口
    "proxy_chat": ("src/hippocampus/proxy/app.py", 378, "/v1/chat/completions"),
    "proxy_resp": ("src/hippocampus/proxy/app.py", 383, "/v1/responses"),
    "proxy_anth": ("src/hippocampus/proxy/app.py", 388, "/v1/messages"),
}

# 文件级锚点：只要求"该文件里存在该 token"，不钉行号。
# 为什么需要这一档：`agent/graph.py` 是编排层，重构会让行号整体位移——钉行号会在无关改动后
# 误报"图过期"。文件级锚点仍然挡得住"这文件被删／被改名／编排被换掉"。
FILE_CHECKS: list[tuple[str, str, str]] = [
    ("Agent 形态用 LangGraph StateGraph 编排", "src/hippocampus/agent/graph.py", "graph = StateGraph(AgentState)"),
    ("Agent 形态三节点 think", "src/hippocampus/agent/graph.py", "def think("),
    ("Agent 形态三节点 act", "src/hippocampus/agent/graph.py", "def act("),
    ("Agent 形态三节点 answer", "src/hippocampus/agent/graph.py", "def answer("),
    ("代理形态挂多个入站端点", "src/hippocampus/proxy/app.py", "@app.post("),
]

# 图上只写短路径（省略 src/hippocampus/ 前缀），README 里说明这一点
SHORT_PREFIX = "src/hippocampus/"

# 除了"某一行有某 token"，图里还有几条**否定式**断言（"全仓没有 X"）。
# 这类断言同样在生成前用 grep 复核：命中数不等于期望值就拒绝出图。
# (说明, 搜索根, ripgrep 式字面量, 期望命中数)
ABSENCE_CHECKS: list[tuple[str, str, str, int]] = [
    ("记忆无物理删除", "src", "DELETE FROM memories", 0),
    ("无 namespace 多租户（隔离直径=account）", "src", "namespace", 0),
]


def load_sources() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path, _line, _tok in ANCHORS.values():
        if path not in out:
            out[path] = (ROOT / path).read_text(encoding="utf-8").splitlines()
    return out


def verify_anchors() -> list[str]:
    """逐条校验锚点（行级 + 文件级）；返回问题清单（空 = 全部对得上）。"""
    problems: list[str] = []
    cache = load_sources()
    for key, (path, line, token) in ANCHORS.items():
        lines = cache[path]
        if line > len(lines):
            problems.append(f"{key}: {path}:{line} 越界（文件只有 {len(lines)} 行）")
            continue
        if token not in lines[line - 1]:
            problems.append(f"{key}: {path}:{line} 不含 {token!r} —— 实际：{lines[line - 1].strip()[:80]!r}")
    for desc, path, token in FILE_CHECKS:
        text = (ROOT / path).read_text(encoding="utf-8")
        if token not in text:
            problems.append(f"{desc}: {path} 里找不到 {token!r}（文件被改／被删？）")
    return problems


def verify_absence() -> list[str]:
    """复核否定式断言（"全仓没有 X"）：逐字面量扫源树，命中数与期望不符即报错。"""
    problems: list[str] = []
    for desc, root, needle, expected in ABSENCE_CHECKS:
        hits = 0
        for path in (ROOT / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            hits += path.read_text(encoding="utf-8", errors="replace").count(needle)
        if hits != expected:
            problems.append(f"{desc}: 在 {root}/ 下命中 {needle!r} {hits} 次（期望 {expected}）")
    return problems


def ref(key: str) -> str:
    """写进图里的锚点小字（短路径:行号）。"""
    path, line, _tok = ANCHORS[key]
    short = path[len(SHORT_PREFIX):] if path.startswith(SHORT_PREFIX) else path
    return f"{short}:{line}"


# ---------------------------------------------------------------- SVG 基础件（无 style 块、无外链）

FONT = "Segoe UI, PingFang SC, Microsoft YaHei, Noto Sans CJK SC, Helvetica Neue, Arial, sans-serif"
MONO = "Consolas, SFMono-Regular, Menlo, DejaVu Sans Mono, Courier New, monospace"

INK = "#0f172a"       # 主文字
MUTED = "#64748b"     # 次级文字
LINE = "#94a3b8"      # 连线
BG = "#ffffff"
CARD = "#f8fafc"      # 卡片底
EDGE = "#cbd5e1"      # 卡片描边

C_BLUE = "#1d4ed8"
C_GREEN = "#047857"
C_AMBER = "#b45309"
C_PURPLE = "#6d28d9"
C_ROSE = "#be123c"
C_SLATE = "#334155"


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _wide(ch: str) -> bool:
    o = ord(ch)
    return (
        0x1100 <= o <= 0x115F
        or 0x2E80 <= o <= 0xA4CF
        or 0xAC00 <= o <= 0xD7A3
        or 0xF900 <= o <= 0xFAFF
        or 0xFE30 <= o <= 0xFE6F
        or 0xFF00 <= o <= 0xFF60
        or 0xFFE0 <= o <= 0xFFE6
        or 0x2500 <= o <= 0x257F
        or 0x2190 <= o <= 0x21FF
    )


def est_width(text: str, size: float) -> float:
    """粗略估宽（只用于"文字是否溢出方框"的自检，保守取大）。"""
    w = 0.0
    for ch in text:
        w += size * (1.0 if _wide(ch) else 0.56)
    return w


class Svg:
    """极简 SVG 拼装器：只吐 presentation attribute，不吐 CSS/脚本/外链。"""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.parts: list[str] = []
        self.overflow: list[str] = []

    # -- 基础 --------------------------------------------------------
    def rect(
        self, x: float, y: float, w: float, h: float, *, fill: str = CARD, stroke: str = EDGE,
        radius: float = 10, dash: str = "", width: float = 1.2,
    ) -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="{radius:.0f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{width:.1f}"{d}/>'
        )

    def text(
        self, x: float, y: float, s: str, *, size: float = 13, fill: str = INK, bold: bool = False,
        anchor: str = "start", mono: bool = False, opacity: str = "",
    ) -> None:
        family = MONO if mono else FONT
        weight = ' font-weight="700"' if bold else ""
        op = f' opacity="{opacity}"' if opacity else ""
        self.parts.append(
            f'<text x="{x:.0f}" y="{y:.0f}" font-family="{family}" font-size="{size:g}" '
            f'fill="{fill}" text-anchor="{anchor}"{weight}{op}>{esc(s)}</text>'
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, *, stroke: str = LINE,
             width: float = 1.4, dash: str = "", marker: bool = True) -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        m = ' marker-end="url(#arrow)"' if marker else ""
        self.parts.append(
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            f'stroke="{stroke}" stroke-width="{width:.1f}"{d}{m}/>'
        )

    def path(self, d: str, *, stroke: str = LINE, width: float = 1.4, dash: str = "", marker: bool = True) -> None:
        ds = f' stroke-dasharray="{dash}"' if dash else ""
        m = ' marker-end="url(#arrow)"' if marker else ""
        self.parts.append(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{width:.1f}"{ds}{m}/>')

    # -- 复合件 ------------------------------------------------------
    def tag(self, x: float, y: float, label: str, *, color: str = C_BLUE, w: float = 104) -> None:
        self.rect(x, y, w, 22, fill="#eef2ff", stroke="#c7d2fe", radius=11, width=1.0)
        self.text(x + w / 2, y + 15.5, label, size=12, fill=color, bold=True, anchor="middle")

    def card(
        self, x: float, y: float, w: float, h: float, title: str, lines: list[str], *,
        refs: list[str] | None = None, accent: str = C_BLUE, title_size: float = 13.5,
        line_size: float = 11.5, ref_size: float = 10.5, pad: float = 12,
    ) -> None:
        """一张卡：标题 + 说明行 + 锚点小字（左下角斜体感）。文字溢出卡宽会记进 overflow。"""
        self.rect(x, y, w, h)
        # 左侧色条
        self.parts.append(
            f'<rect x="{x:.0f}" y="{y:.0f}" width="4" height="{h:.0f}" rx="2" fill="{accent}"/>'
        )
        tx = x + pad
        ty = y + 21
        self.text(tx, ty, title, size=title_size, fill=accent, bold=True)
        if est_width(title, title_size) > w - 2 * pad:
            self.overflow.append(f"card title too wide: {title!r}")
        cy = ty + 17
        for ln in lines:
            self.text(tx, cy, ln, size=line_size, fill=SLATE_ISH)
            if est_width(ln, line_size) > w - 2 * pad:
                self.overflow.append(f"card line too wide in {title!r}: {ln!r}")
            cy += 16
        if refs:
            self.text(x + w - pad, y + h - 9, " · ".join(refs), size=ref_size, fill=MUTED,
                      anchor="end", mono=True)
            joined = " · ".join(refs)
            if est_width(joined, ref_size) > w - 2 * pad:
                self.overflow.append(f"card refs too wide in {title!r}: {joined!r}")

    def header(self, title: str, subtitle: str, note: str) -> None:
        self.text(40, 50, title, size=26, bold=True, fill=INK)
        self.text(40, 78, subtitle, size=13.5, fill=C_SLATE)
        self.text(40, 100, note, size=11, fill=MUTED)

    def render(self) -> str:
        body = "\n  ".join(self.parts)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}" role="img" aria-label="{esc("Hippocampus 架构图")}">\n'
            f'  <defs>\n'
            f'    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto-start-reverse">\n'
            f'      <path d="M 0 0 L 10 5 L 0 10 z" fill="{LINE}"/>\n'
            f'    </marker>\n'
            f'  </defs>\n'
            f'  <rect x="0" y="0" width="{self.width}" height="{self.height}" fill="{BG}"/>\n'
            f'  {body}\n'
            f'</svg>\n'
        )


SLATE_ISH = "#334155"


# ---------------------------------------------------------------- 图 1：架构总览

def build_architecture() -> tuple[str, list[str]]:
    W, H = 1240, 1030
    s = Svg(W, H)
    s.header(
        "Hippocampus — 架构总览",
        "一个记忆核心（MemoryCore）· 两种消费形态 · 双集合 · 三条正交状态轴 · 双轨＋观察轨",
        "图中每个方框右下角 = 该能力的代码锚点（仓内相对路径[:行号]，省略 src/hippocampus/ 前缀）；"
        "生成脚本会在出图前逐条校验锚点，对不上就拒绝生成。",
    )

    L, CW = 150, 1040  # 内容左边界 / 内容宽度

    # ① 入口
    y = 122
    s.tag(40, y, "① 入口", w=96)
    entries = [
        ("OpenAI Chat Completions", ["POST /v1/chat/completions"], "proxy_chat"),
        ("OpenAI Responses", ["POST /v1/responses"], "proxy_resp"),
        ("Anthropic Messages", ["POST /v1/messages"], "proxy_anth"),
        ("CLI / 库调用", ["hippocampus …  ·  MemoryCore"], "memcore"),
    ]
    bw, gap = 240.0, 19.0
    for i, (t, ls, key) in enumerate(entries):
        x = L + i * (bw + gap)
        s.card(x, y, bw, 62, t, ls, refs=[ref(key)], accent=C_SLATE, title_size=12.5, line_size=11)
        s.line(x + bw / 2, y + 62, x + bw / 2, y + 84, stroke="#94a3b8")

    # ② 双形态
    y = 206
    s.tag(40, y, "② 双形态", w=96)
    forms = [
        ("代理形态（Proxy）", [
            "按请求体形状判定格式 → 请求前注入 → 响应后固化",
            "确认块随回复回传；回复用客户端自己那套协议返回",
        ], ["proxy/app.py"], C_BLUE),
        ("Agent 形态（LangGraph）", [
            "think → act → answer 三节点；三出口：完成／无法／需人工升级",
            "记忆按出口与依据判定取舍固化；轨迹可复演（无检查点，指纹比对）",
        ], ["agent/graph.py"], C_PURPLE),
    ]
    fw = 505.0
    for i, (t, ls, rf, ac) in enumerate(forms):
        x = L + i * (fw + 30.0)
        s.card(x, y, fw, 84, t, ls, refs=rf, accent=ac)
        s.line(x + fw / 2, y + 84, x + fw / 2, y + 106, stroke="#94a3b8")

    # ③ MemoryCore 门面
    y = 316
    s.tag(40, y, "③ 记忆门面", w=96)
    s.card(
        L, y, CW, 78, "MemoryCore（唯一门面：库调用与两种形态走同一套语义）",
        [
            "Scope 第一参数 = account／session／source；一账户一目录一库（物理隔离）",
            "单写者锁 ＋ LRU 会话缓存（有界常驻）＋ 注入装配 stable／fluid 两层 ＋ 审计 jsonl",
        ],
        refs=[ref("memcore"), ref("scope"), ref("source"), ref("inject_asm")], accent=C_GREEN,
    )
    s.line(L + CW / 2, y + 78, L + CW / 2, y + 100, stroke="#94a3b8")

    # ④ 写入侧三轨
    y = 418
    s.tag(40, y, "④ 写入三轨", w=96)
    tracks = [
        ("确认轨（确认后入库）", [
            "输入：用户消息里的 preference／fact",
            "冲突机械检测 → 挂起为 candidate",
            "确认块随回复回传，等用户裁决",
            "裁决前旧值继续生效",
        ], "track_a", C_AMBER),
        ("非确认轨（自动入库）", [
            "输入：用户消息里的 status／resource",
            "响应后自动落库，不建确认块",
            "与确认轨共享 episode，防重复",
            "并发执行、各自持锁",
        ], "track_b", C_GREEN),
        ("观察轨（永不静默注入）", [
            "输入：模型回复里的 resource／status",
            "入库即 shadow=1（观察期）",
            "幻觉资源直接丢弃、留痕",
            "晋升到正式只能由人工触发",
        ], "obs_note", C_ROSE),
    ]
    bw2 = 336.0
    for i, (t, ls, key, ac) in enumerate(tracks):
        x = L + i * (bw2 + 16.0)
        s.card(x, y, bw2, 132, t, ls, refs=[ref(key)], accent=ac, title_size=12.5, line_size=11.5)
        s.line(x + bw2 / 2, y + 132, x + bw2 / 2, y + 154, stroke="#94a3b8")

    # ⑤ 存储：双集合 + SQLite + 旁路日志
    y = 576
    s.tag(40, y, "⑤ 存储", w=96)
    s.card(
        L, y, 520, 128, "SQLite 主库（每账户一份 memory.db）",
        [
            "四张主表：entities（实体三分）／memories／episodes／relations",
            "辅助表：pending_blocks（挂起块可跨进程恢复）／feedback_logs",
            "param_versions／param_snapshots／import_*",
            "关系边：ABOUT／MENTIONS／DERIVED_FROM／SUPERSEDES／…",
        ],
        refs=[ref("db"), ref("rel_types"), ref("pending")], accent=C_SLATE, title_size=12.5, line_size=11.5,
    )
    s.card(
        L + 536, y, 504, 128, "Chroma 双集合（每账户一个 PersistentClient）",
        [
            "hippocampus_mem = 经验层（memories）",
            "hippocampus_ep  = 经历层（episodes）",
            "metadata：hnsw:space = cosine",
            "缺 chromadb 时：语义通道降级关闭，BM25／图／事件照常",
        ],
        refs=[ref("col_mem"), ref("col_ep"), ref("cfg")], accent=C_BLUE, title_size=12.5, line_size=11.5,
    )
    # 三条正交轴
    y2 = 716
    axes = [
        ("status", "active ｜ superseded（另有 candidate 待裁决）", "axis_status", C_AMBER),
        ("lifecycle", "active ｜ dormant ｜ archived（与 status 正交）", "axis_life", C_GREEN),
        ("shadow", "0 正式 ｜ 1 观察期（不污染正式检索）", "axis_shadow", C_ROSE),
    ]
    aw = 336.0
    for i, (t, v, key, ac) in enumerate(axes):
        x = L + i * (aw + 16.0)
        s.rect(x, y2, aw, 54, fill="#fffbeb" if ac == C_AMBER else CARD)
        s.parts.append(f'<rect x="{x:.0f}" y="{y2:.0f}" width="4" height="54" rx="2" fill="{ac}"/>')
        s.text(x + 12, y2 + 22, f"状态轴 · {t}", size=12.5, fill=ac, bold=True)
        s.text(x + 12, y2 + 41, v, size=11, fill=SLATE_ISH)
        s.text(x + aw - 12, y2 + 22, ref(key), size=10.5, fill=MUTED, anchor="end", mono=True)

    # ⑥ 检索与注入
    y = 800
    s.tag(40, y, "⑥ 检索注入", w=96)
    s.card(
        L, y, 420, 118, "四通道检索（各自打分，禁跨通道比分数）",
        [
            f"语义：向量库余弦 — {ref('semantic')}（默认 384 维词法哈希）",
            f"关键词：自建 BM25 倒排 — {ref('lexical')}（库指纹失效重建）",
            f"图谱：实体邻接扩展 — {ref('graph_search')}",
            f"事件线索：episode 召回 — {ref('episode')}（相关性断崖截断）",
        ],
        refs=[ref("order")], accent=C_BLUE, title_size=12.5, line_size=11,
    )
    s.line(L + 420, y + 59, L + 452, y + 59, stroke=C_SLATE)
    s.card(
        L + 456, y, 268, 118, "融合与预算装填",
        [
            "区块管线：顺序 语义→BM25→图→事件",
            "token 预算裁剪（超限砍尾，不破顺序）",
            "跨通道分数只做同通道比较",
        ],
        refs=[ref("fuse")], accent=C_GREEN, title_size=12.5, line_size=11,
    )
    s.line(L + 724, y + 59, L + 756, y + 59, stroke=C_SLATE)
    s.card(
        L + 760, y, 280, 118, "分层注入",
        [
            "stable／fluid 两层装配后再给模型",
            "观察轨与 rejected 记忆不进注入",
            "注入了什么可查（审计 jsonl）",
        ],
        refs=[ref("inject_asm")], accent=C_PURPLE, title_size=12.5, line_size=11,
    )

    # 页脚：边界声明
    y = 936
    s.rect(40, y, W - 80, 66, fill="#fef2f2", stroke="#fecaca")
    s.text(56, y + 24, "边界（与 docs/roadmap.md 一致，别读多）", size=12.5, fill=C_ROSE, bold=True)
    s.text(56, y + 44, "默认嵌入档是 384 维词法哈希，不是神经语义（可切 onnx:bge-small-zh-v1.5）；"
                       "四通道自家消融显示高度冗余，不构成差分优势；", size=11.5, fill=SLATE_ISH)
    s.text(56, y + 60, "无 MCP／第三方工具接入；隔离直径是 account（一账户一库），session 只做会话内归组。",
           size=11.5, fill=SLATE_ISH)
    return s.render(), s.overflow


# ---------------------------------------------------------------- 图 2：状态轴与三轨（写入侧放大）

def build_lifecycle() -> tuple[str, list[str]]:
    W, H = 1240, 870
    s = Svg(W, H)
    s.header(
        "Hippocampus — 三条正交状态轴与写入侧三轨",
        "记忆可查看／可改／可删／可追溯：改 = supersede（旧条原地保留），删 = 归档（不物理删除）",
        "锚点口径同架构图：短路径[:行号]，省略 src/hippocampus/ 前缀；行级锚点在出图前逐条校验。",
    )

    # ① 三条正交轴
    y = 122
    s.tag(40, y, "① 状态轴", w=96)
    axes = [
        ("status（这条记忆还算不算数）", [
            "active     —— 正式生效，参与检索与注入",
            "superseded —— 被新条取代（旧条原地保留）",
            "candidate  —— 冲突挂起待裁决；旧值仍生效",
        ], ["axis_status"], C_AMBER),
        ("lifecycle（还用不用得上）", [
            "active  —— 正常",
            "dormant —— 久未命中，降权但仍在库",
            "archived —— 归档（删除即归档，不物理删除）",
        ], ["axis_life", "life_rule"], C_GREEN),
        ("shadow（谁能看见它）", [
            "1 —— 观察期：模型输出侧的记忆，不进正式检索",
            "0 —— 正式：只有用户原话／显式「记住」才进得来",
            "晋升只能人工触发（无自动晋升）",
        ], ["axis_shadow"], C_ROSE),
    ]
    aw = 336.0
    for i, (t, ls, keys, ac) in enumerate(axes):
        x = 150 + i * (aw + 16.0)
        s.card(x, y, aw, 132, t, ls, refs=[ref(k) for k in keys], accent=ac, title_size=12.5, line_size=11.5)
    s.text(150, y + 152, "三条轴相互正交：一条记忆同时携带 status／lifecycle／shadow 三个取值，"
                        "互不推导（例如 shadow=1 的记忆照样可以是 lifecycle=active）。",
           size=11.5, fill=MUTED)

    # ② 写入侧三轨
    y = 306
    s.tag(40, y, "② 写入三轨", w=96)
    rows: list[tuple[str, str, str, list[list[str]]]] = [
        (
            "确认轨", "track_a", C_AMBER,
            [
                ["用户消息", "preference／fact"],
                ["冲突检测（机械规则）", "＋ 可选 LLM"],
                ["命中冲突 → candidate", "写 pending_blocks 挂起"],
                ["确认块随回复回传", "等裁决（旧值仍生效）"],
            ],
        ),
        (
            "非确认轨", "track_b", C_GREEN,
            [
                ["用户消息", "status／resource"],
                ["响应后自动抽取", "（独立 prompt）"],
                ["直接落库 status=active", "不建确认块"],
                ["与确认轨共享 episode", "防重复入库"],
            ],
        ),
        (
            "观察轨", "obs_note", C_ROSE,
            [
                ["模型回复", "resource／status"],
                ["独立 prompt", "宁缺毋滥"],
                ["入库即 shadow=1", "观察期，不污染检索"],
                ["幻觉资源丢弃留痕", "晋升只能人工触发"],
            ],
        ),
    ]
    rh = 96.0
    chip_w, chip_gap, chip_x0 = 210.0, 16.0, 300.0
    for i, (name, key, ac, chips) in enumerate(rows):
        yy = y + i * (rh + 12.0)
        s.rect(150, yy, 1040, rh, fill="#fbfdff" if i % 2 == 0 else CARD)
        s.parts.append(f'<rect x="150" y="{yy:.0f}" width="4" height="{rh:.0f}" rx="2" fill="{ac}"/>')
        s.text(166, yy + 24, name, size=13, fill=ac, bold=True)
        s.text(166, yy + 66, ref(key), size=10.5, fill=MUTED, mono=True)
        for j, lines in enumerate(chips):
            cx = chip_x0 + j * (chip_w + chip_gap)
            s.rect(cx, yy + 22, chip_w, 52, fill=BG, stroke="#e2e8f0", radius=8, width=1.0)
            if len(lines) == 1:
                s.text(cx + chip_w / 2, yy + 53, lines[0], size=11, fill=SLATE_ISH, anchor="middle")
            else:
                for k, piece in enumerate(lines[:2]):
                    s.text(cx + chip_w / 2, yy + 45 + k * 15, piece, size=11, fill=SLATE_ISH, anchor="middle")
            if len(lines) > 2:
                s.overflow.append(f"chip 文字超过两行：{lines!r}")
            for piece in lines:
                if est_width(piece, 11) > chip_w - 16:
                    s.overflow.append(f"chip 文字溢出：{piece!r}")
            if j < len(chips) - 1:
                s.line(cx + chip_w, yy + 48, cx + chip_w + chip_gap - 2, yy + 48, stroke="#94a3b8")
    s.text(150, y + 3 * (rh + 12.0) + 24,
           "命名口径（docs/security.md）：双轨专指确认轨与非确认轨（都来自用户消息，区别是"
           "会不会生成确认块）；模型侧一律叫观察轨。",
           size=11.5, fill=MUTED)

    # ③ 确认回路
    y = 700
    s.tag(40, y, "③ 确认回路", w=96)
    loop = [
        ("冲突挂起", ["命中规则冲突 → 写 pending_blocks 表",
                      "candidate 不参与注入／去重基准"], ["confirm_block", "pending"], C_AMBER),
        ("用户裁决", ["回复「确认N」／「否决」（确认块随回复回传）",
                      "挂起块持久化，跨进程可恢复"], ["confirm_usage"], C_BLUE),
        ("落定", ["确认：新条 active／旧条 superseded＋SUPERSEDES 边",
                  "否决：新条丢弃；旧值全程继续生效"], ["supersede_fn", "conflict_rule"], C_GREEN),
    ]
    lw = 336.0
    for i, (t, ls, keys, ac) in enumerate(loop):
        x = 150 + i * (lw + 16.0)
        s.card(x, y, lw, 88, t, ls, refs=[ref(k) for k in keys], accent=ac,
               title_size=12.5, line_size=11.5)
        if i < 2:
            s.line(x + lw, y + 44, x + lw + 16, y + 44, stroke=C_SLATE)
    s.text(150, y + 108,
           "版本链缺口（如实）：没有独立版本号列／版本历史表，链条只能沿 SUPERSEDES 边反推；"
           "审计三件套 = audit.jsonl（检索候选全集）＋ observe.jsonl（注入／确认／求证事件）＋ param_versions。",
           size=11.5, fill="#b91c1c")
    return s.render(), s.overflow


def _wrap(text: str, max_px: float, size: float) -> list[str]:
    """按估算宽度折行（CJK 逐字算）。返回全部折行结果，超过两行由调用方报错。"""
    if est_width(text, size) <= max_px:
        return [text]
    out, cur = [], ""
    for ch in text:
        if cur and est_width(cur + ch, size) > max_px:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- 入口

def main(argv: list[str]) -> int:
    problems = verify_anchors() + verify_absence()
    if problems:
        print("锚点校验失败，拒绝出图（图与代码不一致比没有图更糟）：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"锚点校验通过：{len(ANCHORS)} 条行级锚点 ＋ {len(FILE_CHECKS)} 条文件级锚点 ＋ "
          f"{len(ABSENCE_CHECKS)} 条否定式断言全部成立。")
    if "--check" in argv:
        return 0

    arch, ov1 = build_architecture()
    life, ov2 = build_lifecycle()
    overflow = ov1 + ov2
    if overflow:
        print("文字溢出方框，拒绝出图（先改版式或缩短文案）：")
        for o in overflow:
            print("  -", o)
        return 1

    DOCS.mkdir(parents=True, exist_ok=True)
    for name, svg in (("architecture.svg", arch), ("memory-lifecycle.svg", life)):
        out = DOCS / name
        out.write_text(svg, encoding="utf-8", newline="\n")
        print(f"写入 {out}（{len(svg.encode('utf-8'))} B）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
