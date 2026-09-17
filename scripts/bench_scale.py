#!/usr/bin/env python
"""Hippocampus 规模档基准（合成库：1154 记忆 / 2406 实体 / 6035 关系 / 497 经历）。

用途
    在一台机器上量"固定规模下"记忆核心关键路径的延迟与吞吐。所有数字**实测**得到，
    脚本里不写死任何结果。六个序列：
      ① 检索 lat  core.search(limit=8)
      ② 注入 lat  core.inject_finalize
      ③ 审计开销  同一注入序列，审计通道开 vs 关（活跃快照 audit_enabled）
      ④ 每轮固化  core.consolidate（轮末守卫开 vs 关；关=进程内 monkeypatch 空实现）
      ⑤ 写入吞吐  core.write（**每次写都含一次全量索引同步**）
      ⑥ 任务成功率 复用 eval.runner.run_bundle 的"记忆开/关"对照（10 题 demo 口径）

口径与边界（诚实声明，随结果一起打印）
    - **离线档**：进程启动即删掉全部 API key 环境变量并置 `HIPPOCAMPUS_OFFLINE=1`
      （清单与 `tests/conftest.py` 同源），无模型端点、无网络、无凭据；数字与在线档不可比。
    - 嵌入档 `builtin-hash`（零下载内置档）。换机器、换嵌入档、换磁盘都会变——
      这些数字**只对本机本次运行成立**，不作普适承诺。
    - 分位数用最近秩法；每序列先热身 `--warmup` 次（丢弃）再计时 `--iters` 次。
    - fixture 是**合成数据**：中文、项目管理场景、无个人数据/凭据/真实机构名。

fixture 构建捷径（为什么不是 1154 次 core.write）
    `core.write` 每次都做全量索引同步（`sync_index` 全表 upsert），1154 次写入是 O(n²)
    的索引量。构建阶段因此改用项目自己的行级 API（`db.add_entity` / `db.add_memory` /
    `db.add_episode` / `db.add_relation`）直接落库，收尾调用
    `core.rebuild_index(scope, pool="all")` 把向量索引一次性对齐到 DB——这正是仓里
    `index rebuild` 运维命令走的同一条路径。⑤ 号序列测的写入走完整 `core.write`。

跑法
    .venv/Scripts/python.exe scripts/bench_scale.py --json D:/tmp/bench_scale_result.json
    `--home` 改 fixture 落点（默认 D:/tmp/hc-bench/scale，D 盘；C 盘不落大文件）；
    `--keep` 复用已建库、跳过重建（在既有库上测，并打印盘上计数）。

幂等
    默认 wipe 重建 fixture（**只在 temp 目录里**，绝不碰用户真实记忆库）；可重复运行。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import random
import shutil
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 0. 凭据卫生 + 离线档（**必须在 import hippocampus 之前**）
#    与 tests/conftest.py 的 API_KEY_ENV_VARS 同一份清单；任何一条留着都可能让
#    "基准跑着跑着调了模型端点"，那样量的就不是记忆层了。
# ---------------------------------------------------------------------------
API_KEY_ENV_VARS = (
    "HIPPOCAMPUS_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HIPPOCAMPUS_BASE_URL",
    "OPENAI_BASE_URL",
    "HIPPOCAMPUS_MODEL",
    "OPENAI_MODEL",
    "HIPPOCAMPUS_SECRET_CMD",
)
for _name in API_KEY_ENV_VARS:
    os.environ.pop(_name, None)
os.environ.setdefault("HIPPOCAMPUS_OFFLINE", "1")

from hippocampus.core import MemoryCore, Scope  # noqa: E402
from hippocampus.memory import database as db  # noqa: E402
from hippocampus.memory import retrieval as rt  # noqa: E402
from hippocampus.memory import runtime  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 真实跑过的规模（本次基准的固定档）
TARGET = {"memories": 1154, "entities": 2406, "relations": 6035, "episodes": 497}
ENTITY_COUNT = TARGET["entities"]
MEMORY_COUNT = TARGET["memories"]
EPISODE_COUNT = TARGET["episodes"]
RELATION_TARGET = TARGET["relations"]

DAY_MS = 86_400_000
DEFAULT_HOME = Path("D:/tmp/hc-bench/scale")
DEFAULT_ACCOUNT = "bench-scale"   # 大规模库所在 scope
TURN_ACCOUNT = "bench-turn"       # ④ 号序列的独立小库（每轮固化会全量索引同步，用独立库避免串扰）
WRITE_SESSION = "bench-write"     # ⑤ 号序列的会话名
EVAL_ACCOUNT = "bench"            # ⑥ 号序列（CLI demo 口径）的 scope

_MEM_KINDS = ("fact", "preference", "status", "resource")
_CN_DIGITS = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")
_QUERY_TEMPLATES = (
    "{name} 现在进展怎么样",
    "{name} 的排期有什么要求",
    "{name} 相关的资料在哪",
    "{name} 目前的状态",
    "关于 {name} 有哪些记录",
    "{name} 的编号是多少",
)

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _pct(values: list[float], p: float) -> float:
    """最近秩分位数（p∈(0,100]；小样本下比插值更保守，不虚构尾部）。"""
    if not values:
        return float("nan")
    xs = sorted(values)
    k = max(1, math.ceil(p / 100.0 * len(xs)))
    return xs[min(k, len(xs)) - 1]


def _ms(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def _measure(fn, *, iters: int, warmup: int) -> list[float]:
    """热身 warmup 次（丢弃）→ 计时 iters 次，返回每轮毫秒样本。"""
    out: list[float] = []
    for _ in range(max(warmup, 0)):
        fn()
    for _ in range(iters):
        out.append(_ms(fn))
    return out


@contextlib.contextmanager
def _quiet_stdout():
    """把 stdout 重定向到 devnull。

    固化/抽取管线自带 `print`（[记忆]/[去重]/[消歧调用失败] …），控制台 I/O 不该计入
    记忆层延迟；被静音的范围写进 口径 里，不做无声处理。
    """
    old = sys.stdout
    devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = devnull
    try:
        yield
    finally:
        sys.stdout = old
        devnull.close()


def _disp_width(text: str) -> int:
    """终端显示宽度（CJK 全角按 2 计），让中文表头对齐。"""
    w = 0
    for ch in text:
        w += 2 if "\u1100" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" else 1
    return w


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(width - _disp_width(text), 0)
    return text + " " * gap if align == "left" else " " * gap + text


def _cn_number(n: int) -> str:
    """0-999 → 中文数字串（合成句里的对象名用，避免任何真实姓名/机构）。"""
    return "".join(_CN_DIGITS[int(d)] for d in f"{n % 1000:03d}")


def _summary(samples: list[float]) -> dict:
    return {
        "n": len(samples),
        "p50_ms": round(_pct(samples, 50), 2),
        "p95_ms": round(_pct(samples, 95), 2),
        "min_ms": round(min(samples), 2) if samples else None,
        "max_ms": round(max(samples), 2) if samples else None,
    }


# ---------------------------------------------------------------------------
# 1. fixture：合成库（直接行级写入 + 一次索引重建）
# ---------------------------------------------------------------------------


def _memory_text(kind: str, name: str, j: int) -> str:
    """合成记忆正文（引用实体名，让图通道/BM25 都能被踩到）。"""
    return {
        "fact": f"{name} 的模块编号是 M{j % 97}",
        "preference": f"我优先跟进 {name} 的排期",
        "status": f"{name} 目前正在做接口联调",
        "resource": f"{name} 的设计文档放在 D:/bench/docs/{j % 53}.md",
    }.get(kind, f"{name} 的记录 {j}")


def build_fixture(home: Path, account: str, seed: int) -> dict:
    """建合成库：2406 实体 → 1154 记忆 → 497 经历 → 关系补到 6035。返回实际计数。"""
    rng = random.Random(seed)
    data_dir = home / "accounts" / account
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(data_dir / "memory.db")
    try:
        # 2406 实体：项目A1 … 项目A2406（三型轮转）
        eids: list[str] = []
        for i in range(1, ENTITY_COUNT + 1):
            name = f"项目A{i}"
            etype = ("Abstract", "Concrete", "Event")[i % 3]
            eids.append(db.add_entity(conn, name, etype, description=f"合成实体 {name}"))

        # 1154 记忆：四类轮转，每条 ABOUT 1 个实体（关系计数可预测）
        kind_mix: dict[str, int] = {}
        for j in range(MEMORY_COUNT):
            idx = rng.randrange(ENTITY_COUNT)
            name = f"项目A{idx + 1}"
            kind = _MEM_KINDS[j % len(_MEM_KINDS)]
            kind_mix[kind] = kind_mix.get(kind, 0) + 1
            db.add_memory(
                conn,
                kind,
                _memory_text(kind, name, j),
                entity_ids=[eids[idx]],
                source_quote="bench-synthetic",
            )

        # 497 经历：时间戳摊在 ~200 天里，每条 MENTIONS 1-3 个实体
        now = db.now_ms()
        for k in range(EPISODE_COUNT):
            ts = now - int(200 * DAY_MS * (k / EPISODE_COUNT)) - rng.randrange(DAY_MS)
            ents = [eids[(k * 3 + p) % ENTITY_COUNT] for p in range(1 + k % 3)]
            db.add_episode(
                conn,
                f"bench_s_{k % 20}",
                ("user", "assistant")[k % 2],
                f"第{k}次基准经历记录：合成场景下的例行同步",
                entity_ids=ents,
                event_time=ts,
            )
        conn.commit()

        # 实体—实体 RELATED_TO 补足总关系数到 6035（图通道 2 跳的边）
        i = 0
        while db.count_stats(conn)["relations"] < RELATION_TARGET:
            a = i % ENTITY_COUNT
            b = (a + 1 + (i * 37) % (ENTITY_COUNT - 1)) % ENTITY_COUNT
            db.add_relation(conn, "entity", eids[a], "entity", eids[b], "RELATED_TO")
            i += 1
        conn.commit()
        counts = db.count_stats(conn)
        return {"counts": counts, "kind_mix": kind_mix, "entity_entity_relations": i}
    finally:
        conn.close()


def build_queries(seed: int, count: int) -> list[str]:
    """从合成库派生的中文查询集（互不重复；每条都含实体名，图通道可命中）。"""
    rng = random.Random(seed + 7)
    out: list[str] = []
    seen: set[str] = set()
    while len(out) < count:
        idx = rng.randrange(ENTITY_COUNT)
        tpl = _QUERY_TEMPLATES[rng.randrange(len(_QUERY_TEMPLATES))]
        q = tpl.format(name=f"项目A{idx + 1}")
        if q in seen:
            continue
        seen.add(q)
        out.append(q)
    return out


# ---------------------------------------------------------------------------
# 2. 序列实现
# ---------------------------------------------------------------------------


def set_active_param(conn, key: str, value) -> dict:
    """按项目自身的参数机制改活跃快照（`param_snapshots`，is_active=1）。

    本仓没有 `rt.set_active_params`；唯一的参数写入口就是活跃快照表本身
    （`db.ensure_*_params` / `mb.handle_switch_command` 都是同一段 SELECT→json→UPDATE），
    这里沿用同一段读写，改完用 `rt.get_active_params` **回读验证**（不信"我改了"）。
    """
    row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("活跃参数快照缺失：param_snapshots 里没有 is_active=1 的行")
    params = json.loads(row["params"])
    params[key] = value
    conn.execute(
        "UPDATE param_snapshots SET params=? WHERE id=?",
        (json.dumps(params, ensure_ascii=False), row["id"]),
    )
    conn.commit()
    return rt.get_active_params(conn)


def _audit_lines(home: Path, account: str) -> int:
    """审计旁路文件行数（证明 audit_enabled 真的生效，而不是只改了参数）。"""
    path = home / "accounts" / account / "audit.jsonl"
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def _no_guards(self, scope, session, user_text) -> None:
    """`MemoryCore._run_turn_guards` 的空实现（只用于④号序列的对照臂，与被打补丁方法同签名）。"""
    return None


def series_retrieval_injection_audit(core, scope, queries, iters: int, warmup: int, home: Path) -> dict:
    """①②③：检索／注入装配／审计通道开销（同一查询集轮转）。

    ③ 的两臂**交错测量**（偶数轮 先开后关、奇数轮 先关后开）：先测一臂再测另一臂会把
    "后跑的那臂更慢"（索引/WAL 落盘、磁盘缓存升温）算到开关头上，交错能抵掉这个顺序偏差。
    开关置位走项目自身的活跃快照写入口（`set_active_param`），并在测量外回读验证。
    """
    cursor = {"i": 0}

    def next_query() -> str:
        q = queries[cursor["i"] % len(queries)]
        cursor["i"] += 1
        return q

    def inject() -> None:
        core.inject_finalize(scope, next_query())

    search_samples = _measure(lambda: core.search(scope, next_query(), limit=8), iters=iters, warmup=warmup)
    last_channels = core.search(scope, queries[0], limit=8).channels
    inject_samples = _measure(inject, iters=iters, warmup=warmup)

    session = core._session(scope)  # noqa: SLF001 - 基准脚本，需直接读写活跃快照（与 CLI/运维同一张表）
    conn = session.conn
    audit_on: list[float] = []
    audit_off: list[float] = []
    _discard: list[float] = []  # 热身样本（丢弃）
    lines_before = _audit_lines(home, scope.account)
    on_calls = 0

    def arm(enabled: bool, bucket: list[float]) -> None:
        nonlocal on_calls
        set_active_param(conn, "audit_enabled", enabled)
        if enabled:
            on_calls += 1
        bucket.append(_ms(inject))

    for _ in range(max(warmup, 0)):
        arm(True, _discard)
        arm(False, _discard)
    for i in range(iters):
        if i % 2 == 0:
            arm(True, audit_on)
            arm(False, audit_off)
        else:
            arm(False, audit_off)
            arm(True, audit_on)
    lines_on = _audit_lines(home, scope.account) - lines_before
    # 审计关的独立佐证：连续 5 次注入（关）不应产生任何审计行
    set_active_param(conn, "audit_enabled", False)
    lines_off_before = _audit_lines(home, scope.account)
    for _ in range(5):
        inject()
    lines_off = _audit_lines(home, scope.account) - lines_off_before
    # 复原（库回到默认口径：audit_enabled=True）
    restored = set_active_param(conn, "audit_enabled", True)

    def _ch(**extra) -> dict:
        return {"iters": iters, "warmup": warmup, **extra}

    return {
        "retrieval": {
            "label": "core.search(limit=8)",
            "queries": len(queries),
            "channels_last": {
                "entities_found": last_channels.get("entities_found"),
                "semantic_hits": last_channels.get("semantic_hits"),
                "bm25_hits": last_channels.get("bm25_hits"),
                "graph_hits": last_channels.get("graph_hits"),
                "final_count": last_channels.get("final_count"),
            },
            **_ch(**_summary(search_samples)),
        },
        "injection": {"label": "core.inject_finalize", **_ch(**_summary(inject_samples))},
        "audit": {
            "param": "audit_enabled（活跃快照 param_snapshots）",
            "method": "两臂交错测量（未在同一次调用里嵌套计时）",
            "on": _ch(**_summary(audit_on)),
            "off": _ch(**_summary(audit_off)),
            "delta_p50_ms": round(_pct(audit_on, 50) - _pct(audit_off, 50), 2),
            "delta_p95_ms": round(_pct(audit_on, 95) - _pct(audit_off, 95), 2),
            "audit_jsonl_lines_added_on": lines_on,
            "audit_jsonl_lines_added_off": lines_off,
            "on_calls": on_calls,
            "restored_audit_enabled": restored.get("audit_enabled"),
        },
    }


def _run_token() -> str:
    """本次运行的唯一标记：拼进合成句/写入文本的尾部。

    为什么需要：④⑤ 的文本若完全确定，`--keep` 复跑时会被**完全去重**拦下（省掉写入与
    索引同步），量到的就不是写入路径了。带一个进程内标记即可让"抽取→去重→入库"全程真实发生，
    同时保持文本可复现（标记由时间戳给出，跑完写在 JSON 里）。
    """
    return f"批次{int(time.time())}"


def series_consolidate(core, iters: int, warmup: int, token: str) -> dict:
    """④：每轮固化的延迟（守卫开 vs 关；关=进程内 monkeypatch，不动仓里代码）。

    两臂**交错测量**：每轮先跑"守卫开"再跑"守卫关"（同一条库持续变大，先测一臂会把
    库增长的成本算到另一臂头上）。每轮都是新句子，抽取/去重/挂起确认真实发生。
    """
    scope = Scope(account=TURN_ACCOUNT, session="s-turn", source="user")
    stats_before = core.stats(scope)

    def sentence(i: int) -> str:
        name = f"项目甲{_cn_number(i)}"
        core_txt = f"我只看{name}的周报" if i % 2 == 0 else f"我目前正在推进{name}的接口联调"
        return f"{core_txt}（{token}）"

    def off_sentence(i: int) -> str:
        name = f"项目甲{_cn_number(500 + i)}"
        core_txt = f"我优先跟进{name}的排期" if i % 2 == 0 else f"{name}目前正在做接口联调"
        return f"{core_txt}（{token}）"

    c_on = {"i": 0}
    c_off = {"i": 0}

    def turn_on() -> None:
        i = c_on["i"]
        c_on["i"] += 1
        core.consolidate(scope, user_text=sentence(i), assistant_text="已记录。")

    def turn_off() -> None:
        i = c_off["i"]
        c_off["i"] += 1
        core.consolidate(scope, user_text=off_sentence(i), assistant_text="已记录。")

    guards_on: list[float] = []
    guards_off: list[float] = []
    original = MemoryCore._run_turn_guards
    try:
        with _quiet_stdout():
            for _ in range(max(warmup, 0)):
                MemoryCore._run_turn_guards = original  # type: ignore[method-assign]
                turn_on()
                MemoryCore._run_turn_guards = _no_guards  # type: ignore[method-assign]
                turn_off()
            for _ in range(iters):
                MemoryCore._run_turn_guards = original  # type: ignore[method-assign]
                guards_on.append(_ms(turn_on))
                MemoryCore._run_turn_guards = _no_guards  # type: ignore[method-assign]
                guards_off.append(_ms(turn_off))
    finally:
        MemoryCore._run_turn_guards = original  # type: ignore[method-assign]
    restored = MemoryCore._run_turn_guards is original

    return {
        "label": "core.consolidate(user_text=每轮新句, assistant_text=已记录。)（stdout 静音）",
        "account": TURN_ACCOUNT,
        "method": "两臂交错测量（每轮先守卫开、再守卫关）",
        "guards_on": {"iters": iters, "warmup": warmup, **_summary(guards_on)},
        "guards_off": {"iters": iters, "warmup": warmup, **_summary(guards_off)},
        "delta_p50_ms": round(_pct(guards_on, 50) - _pct(guards_off, 50), 2),
        "delta_p95_ms": round(_pct(guards_on, 95) - _pct(guards_off, 95), 2),
        "guards_restored": restored,
        "stats_before": stats_before,
        "stats_after": core.stats(scope),
    }


def series_write(core, scope, count: int, token: str) -> dict:
    """⑤：写入吞吐（每次 core.write 都含一次全量索引同步）。"""
    samples: list[float] = []

    def one(i: int) -> None:
        text = f"基准写入第{i}条：项目A{(i * 7) % ENTITY_COUNT + 1} 的第{i}次例行巡检记录编号 R{i}（{token}）"
        core.write(scope, text, kind="fact", source_quote="bench")

    t0 = time.perf_counter()
    for i in range(count):
        samples.append(_ms(lambda i=i: one(i)))
    total_s = time.perf_counter() - t0
    return {
        "label": "core.write(kind=fact, source_quote=bench)（每次写含一次全量索引同步）",
        "account": scope.account,
        "session": scope.session,
        "count": count,
        "total_s": round(total_s, 3),
        "throughput_per_s": round(count / total_s, 2) if total_s > 0 else None,
        **_summary(samples),
    }


def series_eval(eval_home: Path, account: str, questions: int, rebuilt: bool) -> dict:
    """⑥：复用 eval.runner.run_bundle（与 CLI `demo --memories --questions N` 同一条路径）。"""
    from hippocampus.eval.runner import run_bundle

    runtime.set_data_root(eval_home)
    core = MemoryCore(home=eval_home)
    try:
        scope = Scope(account=account, session="bench", source="user")
        before = core.stats(scope)
        report = run_bundle(core, scope, questions=questions, memories=True, offline=True)
        lines = report.render().splitlines()
        head_lines = [ln for ln in lines if ln.startswith("[") and "通过" in ln]
        return {
            "api": "hippocampus.eval.runner.run_bundle(questions=10, memories=True, offline=True)",
            "cli_equivalent": "hippocampus.cli demo --memories --questions 10 --offline",
            "home": str(eval_home),
            "account": account,
            "rebuilt": rebuilt,
            "stats_before": before,
            "stats_after": core.stats(scope),
            "arms": [
                {
                    "name": arm.name,
                    "passed": arm.passed,
                    "total": arm.total,
                    "success_rate": round(arm.success_rate, 4),
                    "avg_steps": round(arm.avg_steps, 3),
                    "failures": arm.failures,
                }
                for arm in report.arms
            ],
            "printed_head_lines": head_lines,
            "printed_lines": lines,
            "boundary": report.boundary,
        }
    finally:
        core.close()


# ---------------------------------------------------------------------------
# 3. 主流程
# ---------------------------------------------------------------------------


def _machine_info() -> dict:
    uname = platform.uname()
    return {
        "platform": f"{uname.system} {uname.release}",
        "machine": uname.machine,
        "processor": uname.processor or platform.processor() or "未知",
        "cpu_logical": os.cpu_count(),
        "python": platform.python_version(),
        "python_build": platform.python_implementation(),
    }


def _print_table(series: dict, fixture: dict, eval_info: dict) -> None:
    rows: list[tuple[str, str, str, str]] = []

    def row(label: str, p50, p95, n) -> None:
        rows.append((label, p50, p95, n))

    ret = series["retrieval"]
    inj = series["injection"]
    aud = series["audit"]
    con = series["consolidate"]
    wri = series["write"]
    row("① 检索 core.search(limit=8)", f"{ret['p50_ms']:.2f}", f"{ret['p95_ms']:.2f}", str(ret["n"]))
    row("② 注入 core.inject_finalize", f"{inj['p50_ms']:.2f}", f"{inj['p95_ms']:.2f}", str(inj["n"]))
    row("③ 注入·审计开（默认）", f"{aud['on']['p50_ms']:.2f}", f"{aud['on']['p95_ms']:.2f}", str(aud["on"]["n"]))
    row("   注入·审计关", f"{aud['off']['p50_ms']:.2f}", f"{aud['off']['p95_ms']:.2f}", str(aud["off"]["n"]))
    row("   审计开销 △p50", f"{aud['delta_p50_ms']:.2f}", "—", "—")
    row("④ 固化·守卫开", f"{con['guards_on']['p50_ms']:.2f}", f"{con['guards_on']['p95_ms']:.2f}",
        str(con["guards_on"]["n"]))
    row("   固化·守卫关", f"{con['guards_off']['p50_ms']:.2f}", f"{con['guards_off']['p95_ms']:.2f}",
        str(con["guards_off"]["n"]))
    row("   守卫开销 △p50", f"{con['delta_p50_ms']:.2f}", "—", "—")
    row("⑤ 写入 core.write（含索引同步）", f"{wri['p50_ms']:.2f}", f"{wri['p95_ms']:.2f}", str(wri["n"]))
    arm_txt = "；".join(f"{a['name']} {a['passed']}/{a['total']}" for a in eval_info["arms"])
    row("⑥ 任务成功率（10 题 demo 口径）", arm_txt, "—", "—")

    w1, w2, w3, w4 = 34, 12, 12, 8
    print(_pad("序列", w1) + _pad("p50(ms)", w2, "right") + _pad("p95(ms)", w3, "right") + _pad("n", w4, "right"))
    print("-" * (w1 + w2 + w3 + w4))
    for label, p50, p95, n in rows:
        print(_pad(label, w1) + _pad(p50, w2, "right") + _pad(p95, w3, "right") + _pad(n, w4, "right"))
    print()
    tgt, got = fixture["target"], fixture["achieved"]
    print("fixture 规模（目标 vs 实测）：")
    for key in ("memories", "entities", "relations", "episodes"):
        flag = "✓" if got[key] == tgt[key] else "✗"
        print(f"  {flag} {key:<10} 目标 {tgt[key]:>6}  实测 {got[key]:>6}")
    print(f"  记忆类型分布：{fixture['kind_mix']}；实体-实体 RELATED_TO：{fixture['entity_entity_relations']} 条")
    print(f"  向量索引重建：{fixture['rebuild_index']}；索引健康：{fixture['index_health']}")
    after = fixture.get("achieved_after_series")
    if after:
        print(f"  ⑤ 跑完后（该库被 {wri['count']} 次写入撑大）：{after}")
        print(f"     → ⑤ 的库规模区间：记忆 {got['memories']} → {after['memories']} 条；"
              f"④ 的库规模：记忆 {con['stats_before']['memories']} → {con['stats_after']['memories']} 条")
    print()
    print(f"⑥ 打印行：{eval_info['printed_head_lines']}")


def _print_boundary(
    args, fixture: dict, series: dict, eval_info: dict, machine: dict, tier: dict, elapsed: float
) -> list[str]:
    con = series["consolidate"]
    wri = series["write"]
    aud = series["audit"]
    ch = series["retrieval"].get("channels_last", {})
    lines = [
        "口径/边界（这些数字只对本机本次运行成立）：",
        f"  机器：{machine['platform']} / {machine['machine']} / {machine['processor']} / "
        f"逻辑核 {machine['cpu_logical']} / Python {machine['python']}",
        f"  离线档：HIPPOCAMPUS_OFFLINE={os.environ.get('HIPPOCAMPUS_OFFLINE')}；"
        f"已删除全部 API key 环境变量（清单同 tests/conftest.py）；"
        f"模型端点可用={tier.get('llm_available')}；嵌入档={tier.get('embedding_model')}",
        f"  fixture：合成库 {TARGET}（实测见上）；home={fixture['home']}；account={fixture['account']}；"
        f"重建={fixture['rebuilt']}",
        f"  迭代：热身 {args.warmup} 次（丢弃）+ 计时 {args.iters} 次／序列（写入序列 {args.writes} 次）；"
        f"分位数=最近秩法；查询集 {args.queries} 条中文查询轮转",
        "  检索通道（最后一次检索）："
        f"实体 {ch.get('entities_found')}；语义 {ch.get('semantic_hits')}；"
        f"BM25 {ch.get('bm25_hits')}；图 {ch.get('graph_hits')}；最终 {ch.get('final_count')}",
        f"  ③ 审计开关走活跃快照 param_snapshots.audit_enabled（回读验证 + 旁路文件行数佐证："
        f"开 {aud['on_calls']} 次调用 +{aud['audit_jsonl_lines_added_on']} 行／"
        f"关 5 次调用 +{aud['audit_jsonl_lines_added_off']} 行）；两臂交错测量",
        f"  ④ 守卫关=进程内 monkeypatch MemoryCore._run_turn_guards（已恢复={con['guards_restored']}）；"
        f"独立库 account={con['account']}（{con['stats_before']['memories']}→"
        f"{con['stats_after']['memories']} 条记忆）；固化期间 stdout 静音",
        f"  ⑤ 写入 account={wri['account']}（大规模库本体，{wri['count']} 次写，"
        f"合计 {wri['total_s']}s，{wri['throughput_per_s']} 次/秒）；**每次写含一次全量索引同步**；"
        f"写入期间该库记忆从 {fixture['achieved']['memories']} 长到 "
        f"{fixture.get('achieved_after_series', {}).get('memories', '?')} 条",
        f"  ⑥ 复用 eval.runner.run_bundle（与 CLI `demo --memories --questions 10` 同路径）；"
        f"home={eval_info['home']}；库内 {eval_info['stats_after']}",
        "  边界：离线规则抽取/规则作答，不含模型推理；合成数据；数字随机器、磁盘、"
        "Python 版本、嵌入档变化，不作普适承诺。",
        f"  本次总耗时 {elapsed:.1f}s。",
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hippocampus 规模档基准（合成库 1154/2406/6035/497）")
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="fixture 数据根（默认 D:/tmp/hc-bench/scale）")
    parser.add_argument("--eval-home", default="", help="⑥ 号序列的数据根（默认 <home> 同级的 eval）")
    parser.add_argument("--keep", action="store_true", help="复用既有库，跳过重建（打印盘上计数）")
    parser.add_argument("--json", default="", help="把结果写成 JSON 到该路径")
    parser.add_argument("--iters", type=int, default=100, help="每序列计时次数（默认 100）")
    parser.add_argument("--warmup", type=int, default=10, help="每序列热身次数（默认 10，丢弃）")
    parser.add_argument("--queries", type=int, default=30, help="查询集条数（默认 30）")
    parser.add_argument("--writes", type=int, default=200, help="写入序列次数（默认 200）")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help=f"大规模库 scope（默认 {DEFAULT_ACCOUNT}）")
    args = parser.parse_args(argv)

    t_start = time.perf_counter()
    token = _run_token()
    home = Path(args.home).expanduser().resolve()
    # ⑥ 号序列的落点默认取 <home>-eval（与大规模库并列，都在 temp 目录里）
    eval_home = Path(args.eval_home).expanduser().resolve() if args.eval_home else home.parent / f"{home.name}-eval"
    home.mkdir(parents=True, exist_ok=True)
    runtime.set_data_root(home)  # 让审计/账号目录解析都落在 temp 根（不碰 ~/.hippocampus）

    print("=" * 96)
    print("Hippocampus 规模档基准（合成库）")
    print("=" * 96)

    # ---- fixture ----
    if args.keep:
        rebuilt = False
        print(f"[fixture] --keep：不重建，直接用 {home}")
        probe_db = home / "accounts" / args.account / "memory.db"
        if not probe_db.exists():
            print(f"[fixture] --keep 但库不存在：{probe_db}（先不带 --keep 跑一次）", file=sys.stderr)
            return 2
        probe = db.connect(probe_db)
        try:
            achieved = db.count_stats(probe)
        finally:
            probe.close()
        kind_rows = None
        fixture_extra = {"entity_entity_relations": None}
    else:
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)
        if eval_home.exists():
            shutil.rmtree(eval_home, ignore_errors=True)
        home.mkdir(parents=True, exist_ok=True)
        print(f"[fixture] 重建合成库 → {home}")
        t0 = time.perf_counter()
        built = build_fixture(home, args.account, seed=20260917)
        build_s = time.perf_counter() - t0
        achieved = built["counts"]
        kind_rows = built["kind_mix"]
        fixture_extra = {"entity_entity_relations": built["entity_entity_relations"]}
        print(f"[fixture] 直接行级写入完成：{achieved}（{build_s:.2f}s）")
        rebuilt = True

    scope = Scope(account=args.account, session="bench", source="user")
    core = MemoryCore(home=home)
    queries = build_queries(20260917, args.queries)
    try:
        if args.keep:
            health = core.index_health(scope)
            if health["collection_count"] != health["active_memories"]:
                print(f"[fixture] 索引与库不一致（{health}）→ 重建向量索引")
                core.rebuild_index(scope, pool="all")
            rebuild = {"skipped": True}
        else:
            # 直接行级写入绕过了索引同步：一次性把向量索引对齐到 DB（与 `index rebuild` 同路径）
            t0 = time.perf_counter()
            rebuild = core.rebuild_index(scope, pool="all")
            print(f"[fixture] rebuild_index(pool='all') → {rebuild}（{time.perf_counter() - t0:.2f}s）")
        health = core.index_health(scope)
        counts_now = core.stats(scope)
        if kind_rows is None:
            rows = core._session(scope).conn.execute(  # noqa: SLF001 - 基准确认盘上数据分布
                "SELECT type, COUNT(*) AS c FROM memories GROUP BY type ORDER BY type"
            ).fetchall()
            kind_rows = {r["type"]: r["c"] for r in rows}
        fixture = {
            "home": str(home),
            "account": args.account,
            "rebuilt": rebuilt,
            "target": dict(TARGET),
            "achieved": {k: counts_now[k] for k in ("memories", "entities", "relations", "episodes")},
            "kind_mix": kind_rows,
            "rebuild_index": rebuild,
            "index_health": health,
            **fixture_extra,
        }

        # ---- ①②③ ----
        print("[bench] ①②③ 检索／注入／审计开销 …")
        series = series_retrieval_injection_audit(core, scope, queries, args.iters, args.warmup, home)

        # ---- ④ ----
        print("[bench] ④ 每轮固化（守卫开/关）…")
        series["consolidate"] = series_consolidate(core, args.iters, args.warmup, token)

        # ---- ⑤ ----
        print(f"[bench] ⑤ 写入吞吐（{args.writes} 次，含索引同步；大规模库上跑，耐心等）…")
        series["write"] = series_write(core, Scope(account=args.account, session=WRITE_SESSION, source="user"),
                                       args.writes, token)
        # ⑤ 会撑大它自己测的那条库：把收尾计数一并记下，读表的人能看到规模区间
        fixture["achieved_after_series"] = {
            k: core.stats(scope)[k] for k in ("memories", "entities", "relations", "episodes")
        }
    finally:
        core.close()

    # ---- ⑥ ----
    print("[bench] ⑥ 记忆开/关任务成功率（eval.runner, 10 题）…")
    eval_info = series_eval(eval_home, EVAL_ACCOUNT, questions=10, rebuilt=not args.keep)

    tier = {}
    try:
        from hippocampus.settings import llm_available

        tier["llm_available"] = bool(llm_available())
        probe_core = MemoryCore(home=home)
        try:
            tier["embedding_model"] = probe_core.embedding_tier()["model"]
        finally:
            probe_core.close()
    except Exception as e:  # 软失败：口径行缺一项也不该让基准挂掉
        tier["error"] = f"{type(e).__name__}: {e}"
    machine = _machine_info()
    elapsed = time.perf_counter() - t_start

    print()
    _print_table(series, fixture, eval_info)
    print()
    boundary_lines = _print_boundary(args, fixture, series, eval_info, machine, tier, elapsed)
    for line in boundary_lines:
        print(line)

    if args.json:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run_token": token,
            "machine": machine,
            "tier": tier,
            "iterations": {"iters": args.iters, "warmup": args.warmup, "queries": args.queries,
                           "writes": args.writes},
            "fixture": fixture,
            "series": series,
            "eval": eval_info,
            "boundary_lines": boundary_lines,
            "elapsed_s": round(elapsed, 2),
        }
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
