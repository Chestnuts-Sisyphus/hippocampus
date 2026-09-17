"""公开基准适配层：LoCoMo / LongMemEval（**离线可跑、可复跑、口径写在脸上**）。

## 为什么是这两个基准
记忆领域最主流的两个公开基准：**LoCoMo**（ACL 2024，Mem0／Letta 同族都用它报分）与
**LongMemEval**（ICLR 2025，长历史问答 + 六类题型）。本模块把它们**只读**加载成本项目的
"对话历史 → 记忆层 → 检索/注入"输入，让记忆层在公开语料上跑一遍，产出**可比的数字**。

## 本模块报什么（以及不报什么）
- 报：`answer_in_context`（参考答案是否出现在注入上下文里）、`evidence_in_context`
  （金标准证据原文是否进上下文）、`token_f1`（离线作答器输出与参考答案的词面 F1，
  SQuAD 口径）、`abstain`（无依据时是否弃答，对抗题看这个）、上下文 token 数、
  单题检索+装配延迟 p50／p95。
- **官方分要显式开关**：LoCoMo／LongMemEval 的官方口径要用大模型作答 +（LME）LLM 判分；
默认离线档不跑（`model_arm=False`，行为与旧版完全一致）；显式 `--model-arm` 才出站，
  实现与口径见 `hippocampus.eval.model_arm`（prompt 逐一钉官方仓库 revision）。
  默认档给的仍是检索/词面口径，不是官方分。

## 边界（写进结果表里，别让读者自己猜）
- 语料是**英文**，而本项目的分词/停用词/阈值是**为中文标定**的 → 英文召回偏弱；
  这一条是"实测值偏低"的主因之一，不美化。
- 抽样：`--limit` 控制每题量；默认跑全量（LoCoMo 1986 题／LongMemEval 500 题）时按题量算时间。
- 每题一个独立 scope（account＝题号）：题目之间不互相污染（LongMemEval 每题自带 haystack；
  LoCoMo 每题共享同一段对话 → 用 **对话级 scope**，同一对话的题共享上下文，与官方设置一致）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hippocampus.core import MemoryCore, Scope

# ----------------------------------------------------------------------
# 词面比较（SQuAD 口径的 F1 + 归一化包含判断）
# ----------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff\s]+", re.UNICODE)


def normalize_text(text: str) -> str:
    """小写 + 去标点 + 压缩空白（词面比较的口径，与官方 F1 前的归一化同款）。"""
    t = (text or "").lower()
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def token_f1(pred: str, gold: str) -> float:
    """词面 F1（SQuAD 口径，单参考答案）。"""
    p_toks = normalize_text(pred).split()
    g_toks = normalize_text(gold).split()
    if not p_toks and not g_toks:
        return 1.0
    if not p_toks or not g_toks:
        return 0.0
    from collections import Counter

    common = Counter(p_toks) & Counter(g_toks)
    same = sum(common.values())
    if same == 0:
        return 0.0
    precision = same / len(p_toks)
    recall = same / len(g_toks)
    return 2 * precision * recall / (precision + recall)


_DATE_FORMATS = (
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%B %d %Y",
    "%B %Y",
    "%b %Y",
    "%Y-%m-%d",
    "%b %d, %Y",
)


def answer_variants(answer: str) -> set[str]:
    """参考答案的**等价写法**集合（日期题尤其需要）。

    为什么要有：LoCoMo 的时间题答案来自**会话时间戳**（"7 May 2023"），而语料里我们按
    ISO 记时（`[2023-05-07]`）；不做这层归一化，等于对全部时间题系统性误判为"没答对"。
    非日期答案就是归一化原串。
    """
    out: set[str] = set()
    norm = normalize_text(answer)
    if norm:
        out.add(norm)
    text = (answer or "").strip()
    import datetime as _dt

    for fmt in _DATE_FORMATS:
        try:
            dt = _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        out.add(f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}")
        out.add(f"{dt.year:04d}-{dt.month:02d}")
        out.add(f"{dt.year:04d}")
        break
    return {v for v in out if v}


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------


@dataclass
class Turn:
    text: str
    role: str = "user"
    ts_ms: int | None = None
    session_id: str = ""
    speaker: str = ""


@dataclass
class Item:
    """一道评测题：问题 + 参考答案 + 金标准证据 + 该题的对话历史。"""

    qid: str
    question: str
    answers: list[str]
    category: str = ""
    evidence_texts: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    group: str = ""  # 共享上下文的组名（LoCoMo＝对话 id；LongMemEval＝题号）
    meta: dict[str, Any] = field(default_factory=dict)  # 数据自带字段（如 question_date），模型臂用


# ----------------------------------------------------------------------
# 数据加载
# ----------------------------------------------------------------------


def _parse_ts(raw: str) -> int | None:
    """数据集里的时间串 → epoch ms。两种格式都认：
    LoCoMo `1:56 pm on 8 May, 2023`／LongMemEval `2023/04/10 (Mon) 23:07`。解析不了返回 None。"""
    if not raw:
        return None
    text = str(raw).strip()
    import datetime as _dt

    patterns = (
        ("%I:%M %p on %d %B, %Y", text),
        ("%Y/%m/%d (%a) %H:%M", text),
        ("%Y/%m/%d", text),
    )
    for fmt, candidate in patterns:
        try:
            dt = _dt.datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=_dt.UTC).timestamp() * 1000)
    return None


def load_locomo(path: str | Path, *, limit_conversations: int = 0) -> list[Item]:
    """LoCoMo-10 → 每题一个 Item（同一对话的题共享 `group`＝对话 id）。

    数据结构（官方 `data/locomo10.json`）：10 段对话，每段 `conversation`＝
    `session_N`（轮列表，含 speaker/dia_id/text）+ `session_N_date_time`；
    `qa`＝{question, answer, evidence:[dia_id], category}。1986 题（cat 1/2/3/4/5）。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[Item] = []
    convs = data[:limit_conversations] if limit_conversations else data
    for conv in convs:
        cid = str(conv.get("sample_id") or "conv")
        conversation = conv.get("conversation") or {}
        turns: list[Turn] = []
        dia_index: dict[str, str] = {}
        n = 1
        while f"session_{n}" in conversation:
            sess = conversation.get(f"session_{n}") or []
            ts = _parse_ts(conversation.get(f"session_{n}_date_time") or "")
            sid = f"{cid}/session_{n}"
            for turn in sess:
                text = str((turn or {}).get("text") or "").strip()
                if not text:
                    continue
                dia = str(turn.get("dia_id") or "")
                if dia:
                    dia_index[dia] = text
                turns.append(Turn(text=text, role="user", ts_ms=ts, session_id=sid, speaker=turn.get("speaker") or ""))
            n += 1
        for i, qa in enumerate(conv.get("qa") or []):
            question = str(qa.get("question") or "").strip()
            if not question:
                continue
            answer = qa.get("answer")
            answers = [str(a) for a in (answer if isinstance(answer, list) else [answer]) if str(a).strip()]
            evidence = [dia_index[d] for d in (qa.get("evidence") or []) if d in dia_index]
            out.append(
                Item(
                    qid=f"{cid}-q{i}",
                    question=question,
                    answers=answers,
                    category=f"cat{qa.get('category')}",
                    evidence_texts=evidence,
                    turns=turns,
                    group=cid,
                    meta={"adversarial_answer": str(qa.get("adversarial_answer") or "")},  # cat5 对抗题选项字段
                )
            )
    return out


def load_longmemeval(path: str | Path, *, limit: int = 0, offset: int = 0) -> list[Item]:
    """LongMemEval（oracle／s_cleaned 同构）→ 每题一个 Item（每题自带 haystack）。

    `haystack_sessions`＝会话列表（每会话是轮列表，轮有 role/content），
    `haystack_dates`／`haystack_session_ids` 与之一一对应；`answer_session_ids`
    指向"证据所在会话"（金标准证据）。六类题型见 `question_type`。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data[offset : (offset + limit) if limit else None]
    out: list[Item] = []
    for row in items:
        sessions = row.get("haystack_sessions") or []
        dates = row.get("haystack_dates") or []
        sids = row.get("haystack_session_ids") or []
        answer_sids = set(row.get("answer_session_ids") or [])
        turns: list[Turn] = []
        evidence: list[str] = []
        for idx, sess in enumerate(sessions):
            sid = str(sids[idx] if idx < len(sids) else f"s{idx}")
            ts = _parse_ts(dates[idx] if idx < len(dates) else "")
            for turn in sess or []:
                content = str((turn or {}).get("content") or "").strip()
                if not content:
                    continue
                role = str((turn or {}).get("role") or "user")
                turns.append(Turn(text=content, role=role, ts_ms=ts, session_id=sid))
                if sid in answer_sids:
                    evidence.append(content)
        out.append(
            Item(
                qid=str(row.get("question_id") or f"lme-{len(out)}"),
                question=str(row.get("question") or "").strip(),
                answers=[str(row.get("answer") or "")],
                category=str(row.get("question_type") or ""),
                evidence_texts=evidence,
                turns=turns,
                group=str(row.get("question_id") or ""),
                meta={"question_date": str(row.get("question_date") or "")},
            )
        )
    return out


# ----------------------------------------------------------------------
# 跑一件事：导入 → 逐题检索/注入 → 打分
# ----------------------------------------------------------------------


def sha256_file(path: str | Path) -> str:
    """数据集文件的 sha256（结果表里记版本，保证"这组数字对应哪份数据"可复核）。"""
    import hashlib

    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _pct(values: list[float]) -> float:
    return round(100.0 * (sum(values) / len(values)), 2) if values else 0.0


def _pctl(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _iso_date(ts_ms: int | None) -> str:
    if not ts_ms:
        return ""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.UTC).strftime("%Y-%m-%d")


def _turn_dict(turn: Turn, date_prefix: bool, split_pools: bool = True) -> dict[str, Any]:
    """Turn → `ingest_history` 的输入。`date_prefix=True` 时把会话日期写进内容
    （`[2023-05-07] …`）——公开基准的会话都带时间戳，记忆系统存"这件事发生在哪天"
    才可能回答时间题（Mem0 等同类实现同样把 message 的 date_time 一起喂进上下文）。

    `split_pools=True`（默认）：**每轮只落一个池**——用户轮→经验层（记忆条），
    助手轮→经历层（事件）。理由是注入条数有上限：同一句同时落两处会让 8 个位子
    实际只覆盖 ~4 句（实测证据命中率差 ~9 个百分点，见 `docs/benchmark.md`）。
    `False` 时回到"两处都落"的旧口径（消融对照用）。
    """
    text = turn.text
    iso = _iso_date(turn.ts_ms) if date_prefix else ""
    if iso:
        text = f"[{iso}] {text}"
    out = {
        "text": text,
        "role": turn.role,
        "ts_ms": turn.ts_ms,
        "session_id": turn.session_id,
        "speaker": turn.speaker,
    }
    if split_pools:
        is_user = (turn.role or "user").lower() == "user"
        out["as_memory"] = is_user
        out["as_episode"] = not is_user
    return out


def run_items(
    core: MemoryCore,
    items: list[Item],
    *,
    account_prefix: str = "bench",
    progress_every: int = 200,
    date_prefix: bool = True,
    as_memories: bool = True,
    inject_max_items: int | None = None,
    shadow_log: bool = False,
    split_pools: bool = True,
    capture_context: bool = False,
) -> dict[str, Any]:
    """跑一批题：**按 group 分组**（同组共享上下文，导入一次），逐题检索/注入并打分。

    返回报告 dict：总表 + 分类表 + 逐题明细（明细只在 `detail=True` 的调用方用）。
    `capture_context=True` 时把每题注入上下文原文写进明细（模型臂用；默认关，零变化）。
    """
    groups: dict[str, list[Item]] = {}
    for item in items:
        groups.setdefault(item.group or item.qid, []).append(item)
    rows: list[dict[str, Any]] = []
    ingested: dict[str, dict[str, int]] = {}
    for gi, (group, group_items) in enumerate(groups.items(), 1):
        scope = Scope(account=f"{account_prefix}-{gi}", session="bench", source="user")
        counts = core.ingest_history(
            scope,
            [_turn_dict(t, date_prefix, split_pools=split_pools) for t in group_items[0].turns],
            as_memories=as_memories,
        )
        # 跑批口径（全部走项目自己的"人工标定"入口，不手写 SQL）：
        #  - 关掉逐题 shadow 调试行（纯 stderr 诊断，跑批时干扰读数）；
        #  - 需要时改注入条数上限（消融对照用）。
        from hippocampus.memory import database as _db

        overrides: dict[str, Any] = {"shadow_log_enabled": bool(shadow_log)}
        if inject_max_items is not None:
            overrides["injection_max_items"] = int(inject_max_items)
        with core._session(scope).lock:  # noqa: SLF001 - 基准工具，直接拿会话改活跃参数
            _db.set_active_params(core._session(scope).conn, overrides, reason=f"公开基准口径 {overrides}")  # noqa: SLF001
        ingested[group] = counts
        for item in group_items:
            t0 = time.perf_counter()
            injection = core.inject_finalize(scope, item.question)
            latency_ms = (time.perf_counter() - t0) * 1000.0
            context = injection.text or ""
            norm_ctx = normalize_text(context)
            # 命中判据：参考答案的任一等价写法（含日期 ISO 变体）出现在注入上下文里
            answer_hit = any(v and v in norm_ctx for a in item.answers for v in answer_variants(a))
            evidence_hit = any(normalize_text(e)[:80] in norm_ctx for e in item.evidence_texts if normalize_text(e))
            pred = injection.items[0].content if injection.items else ""
            tokens = _est_context_tokens(context)
            rows.append(
                {
                    "qid": item.qid,
                    "group": group,
                    "category": item.category,
                    "question": item.question,
                    "gold": item.answers,
                    "answer_in_context": bool(answer_hit),
                    "evidence_in_context": bool(evidence_hit),
                    "abstained": not injection.items,
                    "f1": round(token_f1(pred, item.answers[0] if item.answers else ""), 4),
                    "tokens": tokens,
                    "latency_ms": round(latency_ms, 2),
                    "n_injected": len(injection.items),
                    "injected_kinds": sorted({i.kind for i in injection.items}),
                    **({"context": context} if capture_context else {}),
                }
            )
        if progress_every and gi % progress_every == 0:
            print(f"  …已完成 {gi}/{len(groups)} 组（{len(rows)} 题）")

    def _agg(subset: list[dict[str, Any]]) -> dict[str, Any]:
        lat = [r["latency_ms"] for r in subset]
        return {
            "n": len(subset),
            "answer_in_context": _pct([1.0 if r["answer_in_context"] else 0.0 for r in subset]),
            "evidence_in_context": _pct([1.0 if r["evidence_in_context"] else 0.0 for r in subset]),
            "token_f1": _pct([float(r["f1"]) for r in subset]),
            "abstain_rate": _pct([1.0 if r["abstained"] else 0.0 for r in subset]),
            "tokens_mean": round(sum(r["tokens"] for r in subset) / len(subset), 1) if subset else 0.0,
            "latency_p50_ms": _pctl(lat, 0.50),
            "latency_p95_ms": _pctl(lat, 0.95),
        }

    by_cat: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_cat.setdefault(row["category"] or "unknown", []).append(row)
    return {
        "overall": _agg(rows),
        "by_category": {k: _agg(v) for k, v in sorted(by_cat.items())},
        "groups": len(groups),
        "ingested": ingested,
        "rows": rows,
    }


def _est_context_tokens(context: str) -> int:
    """注入上下文的 token 估算（与检索链同一口径：len//2 + 40）。"""
    return len(context or "") // 2 + 40 if context else 0


# ----------------------------------------------------------------------
# CLI 入口（`hippocampus bench …` 也走这里）
# ----------------------------------------------------------------------


def run(
    *,
    bench: str,
    data: str | Path,
    home: str | Path,
    limit: int = 0,
    offset: int = 0,
    json_out: str | Path | None = None,
    account_prefix: str = "bench",
    date_prefix: bool = True,
    as_memories: bool = True,
    inject_max_items: int | None = None,
    shadow_log: bool = False,
    allow_online: bool = False,
    split_pools: bool = True,
    model_arm: bool = False,
    budget_yuan: float = 30.0,
    concurrency: int = 16,
    retries: int = 2,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """跑一个基准（LoCoMo／LongMemEval），返回报告 dict。

    **默认必须离线档**（`HIPPOCAMPUS_OFFLINE=1`）：否则记忆层的维护/固话链会去调模型端点——
    实测过一次"有 key 环境里跑基准，205 次请求全 401"的教训（既不可复现，也可能烧钱）。
    确实要跑模型臂时显式 `allow_online=True`。
    `model_arm=True`：在检索/注入跑完后接**官方判分臂**（模型作答 + LME 判分），
    作答输入＝每题注入上下文（top-8），不是全文；护栏与口径见 `hippocampus.eval.model_arm`。
    """
    from hippocampus import settings as _settings

    if not _settings.is_offline() and not allow_online:
        raise RuntimeError(
            "基准评测默认要求离线档（不发任何出站请求）：请设 HIPPOCAMPUS_OFFLINE=1，"
            "或显式传 allow_online=True（会使用环境里的模型端点与凭据）"
        )
    if model_arm and _settings.is_offline():
        raise RuntimeError("模型臂要求非离线档：跑官方判分前请去掉 HIPPOCAMPUS_OFFLINE=1（显式开关才出站）")
    if model_arm and not _settings.endpoint_ready():
        raise RuntimeError("模型臂要求配好模型端点与凭据（HIPPOCAMPUS_BASE_URL / HIPPOCAMPUS_API_KEY 或密钥服务）")
    bench_key = (bench or "").lower()
    if bench_key in ("locomo", "locomo10"):
        items = load_locomo(data, limit_conversations=0)
        if limit:
            items = items[:limit]
    elif bench_key in ("longmemeval", "lme", "longmemeval_oracle", "longmemeval_s"):
        items = load_longmemeval(data, limit=limit, offset=offset)
    else:
        raise ValueError(f"未知基准: {bench}（可选 locomo／longmemeval）")

    core = MemoryCore(home=home)
    try:
        report = run_items(
            core,
            items,
            account_prefix=account_prefix,
            date_prefix=date_prefix,
            as_memories=as_memories,
            inject_max_items=inject_max_items,
            shadow_log=shadow_log,
            split_pools=split_pools,
            capture_context=model_arm,
        )
    finally:
        core.close()
    report["bench"] = bench_key
    report["data"] = str(data)
    report["home"] = str(home)
    report["generated_at"] = int(time.time())
    report["dataset_sha256"] = sha256_file(data)
    report["protocol"] = {
        "date_prefix": bool(date_prefix),
        "as_memories": bool(as_memories),
        "split_pools": bool(split_pools),
        "inject_max_items": inject_max_items if inject_max_items is not None else "默认（8）",
        "shadow_log": bool(shadow_log),
        "offline": True,
        "model_arm": "未跑（离线档：不用大模型作答、不做 LLM 判分）",
        "scope": "一组一个 account（LoCoMo=一段对话；LongMemEval=一题）",
        "answer_hit": "参考答案的等价写法（含日期 ISO 变体）出现在注入上下文里",
        "evidence_hit": "金标准证据原文进注入上下文",
        "f1": "离线作答器＝注入里分数最高的条目原文；无模型生成，故 F1 只是诊断值",
    }
    if model_arm:
        from hippocampus.eval import model_arm as _arm

        arm_items = [
            _arm.ArmItem(
                qid=item.qid,
                category=item.category,
                question=item.question,
                answers=item.answers,
                context=row.get("context", ""),
                extra=item.meta,
            )
            for item, row in zip(items, report["rows"], strict=False)
        ]
        print("\n[模型臂] 官方判分（作答输入＝注入上下文 top-8，非全文）……")
        arm_report = _arm.run_official(
            bench_key,
            arm_items,
            concurrency=int(concurrency),
            retries=int(retries),
            timeout_s=float(timeout_s),
            budget_yuan=float(budget_yuan),
        )
        # 官方指标提一层（accuracy/f1/ci95/by_category），弹道层（calls/tokens/余额/rows）留在 official 里
        report["official"] = {**arm_report.pop("official"), **arm_report}
        report["protocol"]["offline"] = False
        report["protocol"]["model_arm"] = (
            "官方判分臂：模型作答（temperature 0）＋ LME LLM 判分；"
            "作答输入＝记忆层注入上下文 top-8（inject_finalize 原文），不是全文；"
            f"护栏＝并发 {concurrency}／重试 {retries}／超时 {timeout_s}s／预算 ¥{budget_yuan} 硬停"
        )
    if json_out:
        Path(json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def render(report: dict[str, Any]) -> str:
    """人读表：总表 + 分类表（数字口径写在表头）。"""
    lines = [
        f"基准 {report.get('bench')}  数据 {report.get('data')}",
        f"题量 {report['overall']['n']}  组 {report['groups']}（同组共享上下文）",
        "",
        "口径：answer_in_context／evidence_in_context／token_f1／abstain 均为检索/词面口径"
        "（无模型生成；官方分见下方 official 段，需显式 --model-arm）",
        "",
        f"{'分类':<22}{'题量':>6}{'答在文内':>10}{'证据在文内':>12}{'F1':>8}{'弃答率':>9}{'tokens':>9}{'p50ms':>8}{'p95ms':>8}",
    ]

    def row(name: str, agg: dict[str, Any]) -> str:
        return (
            f"{name:<22}{agg['n']:>6}{agg['answer_in_context']:>10}{agg['evidence_in_context']:>12}"
            f"{agg['token_f1']:>8}{agg['abstain_rate']:>9}{agg['tokens_mean']:>9}"
            f"{agg['latency_p50_ms']:>8}{agg['latency_p95_ms']:>8}"
        )

    lines.append(row("总体", report["overall"]))
    for cat, agg in report["by_category"].items():
        lines.append(row(cat, agg))
    official = report.get("official")
    if official:
        lines.append("")
        lines.append(f"官方判分（模型臂：{official.get('model')}，temperature 0）：{official.get('input_spec')}")
        lines.append(f"  {official.get('metric')}")
        if "f1" in official:
            lines.append(f"  总体 F1 = {official['f1']}（95% CI {official['ci95']}，n={official['n']}）")
            for cat, agg in official["by_category"].items():
                lines.append(f"    {cat}: F1 {agg['score']}（n={agg['n']}）")
        if "accuracy" in official:
            lines.append(f"  总体准确率 = {official['accuracy']}（95% CI {official['ci95']}，n={official['n']}）")
            for cat, agg in official["by_category"].items():
                lines.append(f"    {cat}: {agg['accuracy']}（n={agg['n']}）")
        lines.append(
            f"  调用 {official['calls']['answer'] + official['calls']['judge']} 次"
            f"（作答 {official['calls']['answer']}/判分 {official['calls']['judge']}），"
            f"估算 ¥{official['cost_est_yuan']}，实际花费 ¥{official['spend_yuan']}"
            f"（跑前 ¥{official['balance_before']} → 跑后 ¥{official['balance_after']}）"
        )
        if official.get("n_failed") or official.get("n_skipped"):
            lines.append(
                f"  ⚠ 未完成：失败 {official['n_failed']}／跳过 {official['n_skipped']}（上面的数不是全量，如实报）"
            )
    return "\n".join(lines)


__all__ = [
    "Item",
    "answer_variants",
    "Turn",
    "load_locomo",
    "load_longmemeval",
    "normalize_text",
    "render",
    "run",
    "sha256_file",
    "run_items",
    "token_f1",
]
