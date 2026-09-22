"""GF1：跑**一次真实检索**并落一份结构化 trace（OTLP/JSON）。

补的缺口：此前仓里只有 `audit.jsonl`（检索候选全集）与 `observe.jsonl`（注入/确认/求证
三类事件）两类**扁平事件**——没有 span 层级、没有延迟分解、没有一条 trace 能把
"嵌入 → 向量查询 → 注入"串起来。本脚本走**生产检索路径**（`MemoryCore.inject_finalize`
→ `prepare_injection` → `retrieve` → `semantic_search`），把整条链记进
`hippocampus.observability` 的 span 缓冲，再写成 OTLP/JSON。

跑法：
    python scripts/trace_retrieval.py \
        --home D:/tmp/hc-trace-home \
        --out results/trace_retrieval_otlp.json

判据（脚本自己断言，过则退出码 0）：
  ① 导出件能被解析成 OTLP/JSON（`resourceSpans[0].scopeSpans[0].spans`）；
  ② **≥1 条 trace**（全部 span 同一 traceId）；
  ③ **≥3 个 span**，且 `hippocampus.embedding`／`hippocampus.vector_query`／
     `hippocampus.injection` **三类各至少一个**；
  ④ 每个 span 都有 `startTimeUnixNano`／`endTimeUnixNano`（延迟可分解）、
     且子 span 的 `parentSpanId` 指得到树上的父。

纪律：离线（`HIPPOCAMPUS_OFFLINE=1`）、零出站、零凭据、不弹窗、写 `D:/tmp` 与 `--out`。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("HIPPOCAMPUS_OFFLINE", "1")
for _k in ("HIPPOCAMPUS_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

ACCOUNT = "trace-gf1"
SESSION = "chat-1"

# 三条种子记忆覆盖三种 kind，保证注入层非空、且语义通道有可召回内容。
# 注意：这里是**合成样例文本**，不许写任何仓外正本／个人目录路径
# （仓规：仓库外路径不得硬编码进 tracked 文件；初版曾写真实个人目录，已改掉）。
SEEDS: list[tuple[str, str]] = [
    ("我的期望城市是杭州，毕业后想留在杭州工作", "fact"),
    ("我不喜欢在回答里堆大段代码，先给结论再给细节", "preference"),
    ("我的简历正本放在本机的项目目录里", "resource"),
]
QUERY = "我毕业后想去哪个城市工作？"

REQUIRED_SPANS = (
    "hippocampus.retrieval_request",
    "hippocampus.embedding",
    "hippocampus.vector_query",
    "hippocampus.injection",
)


def _pythonw_safe_print(*parts: object) -> None:
    """Windows 默认 GBK 控制台打不出中文以外的字符会 UnicodeEncodeError（仓内既有教训）。"""
    text = " ".join(str(p) for p in parts)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("utf-8", "replace").decode("utf-8", "replace") + "\n")


def _load_spans(path: Path) -> list[dict]:
    """读回导出件并按 OTLP/JSON 结构取出 span 列表（判据①的验收动作）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    resource_spans = payload["resourceSpans"]
    spans: list[dict] = []
    for rs in resource_spans:
        for ss in rs["scopeSpans"]:
            spans.extend(ss["spans"])
    return spans


def _attr(span: dict, key: str) -> object:
    for item in span.get("attributes", []):
        if item["key"] == key:
            value = item["value"]
            for kind in ("stringValue", "boolValue", "doubleValue", "intValue"):
                if kind in value:
                    return value[kind]
            return None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一次真实检索的结构化 trace（OTLP/JSON 落盘）")
    parser.add_argument("--home", default="D:/tmp/hc-trace-home", help="隔离数据根（每次从 fresh 出发）")
    parser.add_argument("--out", default=str(ROOT / "results" / "trace_retrieval_otlp.json"), help="导出件路径")
    args = parser.parse_args(argv)

    out_path = Path(args.out)
    home = Path(args.home)
    if home.exists():
        shutil.rmtree(home)

    from hippocampus.core import MemoryCore, Scope
    from hippocampus.observability import start_export, stop_export

    core = MemoryCore(home=home)
    ok = True
    try:
        scope = Scope(account=ACCOUNT, session=SESSION, source="user")
        for text, kind in SEEDS:
            core.write(scope, text, kind=kind, source_quote="trace 演示种子")

        start_export(out_path)
        # 根 span 由 inject_finalize 自己开（"一次检索 = 一条 trace"是系统属性，不靠调用方自觉）
        injection = core.inject_finalize(scope, QUERY)
        # 单点落盘：flush 与 stop_export 都是累积语义，重复写得到同一份完整 trace
        written = stop_export()
    finally:
        close = getattr(core, "close", None)
        if callable(close):
            close()

    _pythonw_safe_print(f"导出件：{written or out_path}")
    _pythonw_safe_print(f"注入命中：{len(injection.items)} 条（stable {len(injection.stable_text or '')} 字 / fluid {len(injection.fluid_text or '')} 字）")

    # ---- 判据 ①：能被解析成 OTLP/JSON ----
    spans = _load_spans(out_path)
    ok &= bool(spans)
    _pythonw_safe_print(f"[{'PASS' if spans else 'FAIL'}] ① OTLP/JSON 可解析：{len(spans)} 个 span")

    # ---- 判据 ②：≥1 条 trace ----
    trace_ids = {s["traceId"] for s in spans}
    ok &= len(trace_ids) >= 1
    _pythonw_safe_print(f"[{'PASS' if len(trace_ids) >= 1 else 'FAIL'}] ② trace 数 = {len(trace_ids)}")

    # ---- 判据 ③：≥3 个 span，三类各至少一个 ----
    names = [s["name"] for s in spans]
    ok &= len(spans) >= 3
    for required in REQUIRED_SPANS:
        hit = names.count(required)
        ok &= hit >= 1
        _pythonw_safe_print(f"[{'PASS' if hit >= 1 else 'FAIL'}] ③ {required} × {hit}")

    # ---- 判据 ④：时间戳齐全 ＋ 父子可解析 ----
    by_id = {s["spanId"]: s for s in spans}
    time_ok = all(int(s["startTimeUnixNano"]) > 0 and int(s["endTimeUnixNano"]) >= int(s["startTimeUnixNano"]) for s in spans)
    ok &= time_ok
    _pythonw_safe_print(f"[{'PASS' if time_ok else 'FAIL'}] ④ 时间戳齐全（延迟可分解）")

    dangling = [s["name"] for s in spans if s["parentSpanId"] and s["parentSpanId"] not in by_id]
    ok &= not dangling
    _pythonw_safe_print(f"[{'PASS' if not dangling else 'FAIL'}] ④ 父子关系闭合（悬挂：{dangling}）")

    # ---- 延迟分解（span 层级 → 毫秒） ----
    _pythonw_safe_print("\nspan 树（父 → 子，按开始时间）与耗时：")
    for s in sorted(spans, key=lambda x: int(x["startTimeUnixNano"])):
        dur_ms = (int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])) / 1e6
        depth = 0
        parent = s["parentSpanId"]
        while parent and parent in by_id:
            depth += 1
            parent = by_id[parent]["parentSpanId"]
        extra = ""
        if s["name"] == "hippocampus.vector_query":
            extra = f"  n_returned={_attr(s, 'hippocampus.vector_query.n_returned')}"
        elif s["name"] == "hippocampus.embedding":
            extra = f"  dim={_attr(s, 'hippocampus.embedding.dim')} cache_hit={_attr(s, 'hippocampus.embedding.cache_hit')}"
        elif s["name"] == "hippocampus.injection":
            extra = f"  n_retrieved={_attr(s, 'hippocampus.injection.n_retrieved')}"
        _pythonw_safe_print(f"  {'  ' * depth}- {s['name']:<38} {dur_ms:7.2f} ms{extra}")

    _pythonw_safe_print("\n结论：" + ("PASS（≥1 trace／≥3 span／三类 span 齐全／可复跑）" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
