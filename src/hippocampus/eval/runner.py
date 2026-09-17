"""评测执行：跑题 → 判分 → 记忆开/关对照 → 标签双来源一致率 →（N8）pass^k／模型臂／关键词基线。

两条臂（对照实验）：
- **记忆开**：正常走（检索 → 注入 → 作答／动作）；
- **记忆关**：`关闭记忆` 开关（只关注入，不关固化——开关语义来自前身），
  同一批题再跑一遍。

N8 升级（C1–C4）：
- **pass^k**：每题重复 k 次（默认 k=1；k≥3 时全过才算过），报 pass^k 分数；
- **模型臂**：有模型端点时（非离线）同一套题走模型作答器（ModelPolicy）；
- **关键词基线**：只按 BM25 关键词直查库（无四通道融合、无注入），作对照；
- **来源分列**：报告按 synthetic／real-jd 分列题数（C2）。

判分不是"像不像"，是**要点命中**：题目真值里列出的记忆要点是否出现在作答／动作产物里。
失败按四类归因（检索未召回／参数错／未及时终止／工具错）。

诚实边界写在报告里（report.boundary，含 k 与模型名），随结果一起打印。
"""

from __future__ import annotations

from typing import Any

from hippocampus.agent.runner import run_task
from hippocampus.core import MemoryCore, Scope
from hippocampus.eval.questions import Question, questions_for
from hippocampus.eval.report import ArmResult, EvalReport, QuestionResult
from hippocampus.memory import retrieval as rt


def _ensure_seeded(core: MemoryCore, scope: Scope) -> dict[str, Any]:
    stats = core.stats(scope)
    if stats["memories"] == 0:
        from hippocampus.seed import seed

        return seed(core, scope)
    return {"count": 0, "stats": stats}


def _artifact_of(run: Any, tool: str) -> dict[str, Any] | None:
    """从轨迹里取出某次工具调用的成功结果（动作题的产物在这里）。"""
    for step in run.trace.get("steps", []):
        if step.get("node") == "act" and step.get("tool") == tool and (step.get("result") or {}).get("ok"):
            return step["result"].get("result") or {}
    return None


def _judge(core: MemoryCore, scope: Scope, question: Question, run: Any) -> tuple[bool, list[str], list[str], str]:
    """判分。返回（是否通过, 命中要点, 未命中要点, 判分说明）。

    动作题看**产物**而不是回答文本：写没写进库、文件里有没有这条内容——
    只看回答文本会把"已记下。"这种正确行为判成失败。
    """
    text = run.answer
    if question.check == "refusal":
        refused = ("没有相关" in text) or ("无法确认" in text) or ("不知道" in text)
        return (refused and run.exit == "cannot_complete"), [], [], "拒答" if refused else "该拒答却作答了"

    if question.check == "memory_exists":
        found: list[str] = []
        missing: list[str] = []
        items = core.list_memories(scope, limit=200, status=None, include_shadow=True)
        contents = [i.content for i in items]
        for e in question.expect:
            (found if any(e in c for c in contents) else missing).append(e)
        return (not missing), found, missing, "库内已存在"

    if question.check == "file_written":
        artifact = _artifact_of(run, "write_file")
        path = (artifact or {}).get("path", "")
        body = ""
        if path:
            try:
                from pathlib import Path

                body = Path(path).read_text(encoding="utf-8")
            except OSError:
                body = ""
        found = [e for e in question.expect if e in body]
        missing = [e for e in question.expect if e not in body]
        return (bool(found) and not missing), found, missing, f"产物 {path or '（无）'}"

    found = [e for e in question.expect if e in text]
    missing = [e for e in question.expect if e not in text]
    return (bool(question.expect) and not missing), found, missing, "作答文本"


def _apply_setup(core: MemoryCore, scope: Scope, question: Question) -> None:
    """建立题目前置条件。

    为什么需要：有些题测的是"某个机制生效**之后**"的行为（如 q04 测裁决后的取值），
    前置状态必须显式建立，否则测的是"机制生效前"——那是另一道题。
    """
    if question.setup != "resolve_pending":
        return
    pending = core.pending(scope)
    candidate = next((p for p in pending if p["is_new"]), None)
    if candidate is not None:
        core.confirm(scope, f"确认{candidate['num']}")


def _run_question(
    core: MemoryCore,
    scope: Scope,
    question: Question,
    *,
    max_steps: int,
    workdir: Any,
    offline: bool = True,
) -> QuestionResult:
    _apply_setup(core, scope, question)
    run = run_task(core, scope, question.text, offline=offline, max_steps=max_steps, workdir=workdir)
    auto_label = [line for line in run.answer.split("；") if line.startswith("[")]
    ok, hit, missed, how = _judge(core, scope, question, run)

    steps = run.steps
    failure = ""
    if not ok:
        if run.exit == "needs_human":
            failure = "工具错"
        elif steps >= max_steps:
            failure = "未及时终止"
        else:
            tool_errs = [
                t for t in run.trace.get("steps", []) if t.get("node") == "act" and not (t.get("result") or {}).get("ok")
            ]
            if tool_errs:
                cat = (tool_errs[0].get("result") or {}).get("category", "")
                failure = "参数错" if cat == "参数错" else "工具错"
            else:
                failure = "检索未召回"

    return QuestionResult(
        qid=question.qid,
        kind=question.kind,
        ok=ok,
        steps=steps,
        failure=failure,
        injected=run.injected_ids,
        hit_expect=hit,
        missed_expect=missed,
        auto_label=auto_label,
        answer=f"{run.answer}  [判分依据：{how}]",
        note=question.note,
        source=question.source,
    )


def _label_agreement(results: list[QuestionResult], questions: dict[str, Question]) -> tuple[float, int]:
    """标签双来源一致率：人工标签（题干真值）↔ 自动标签（本轮实际注入）。

    逐题算 Jaccard（题干要点被作答覆盖的比例），全题平均。0 题可比 → 0.0（单列报出）。
    """
    if not results:
        return 0.0, 0
    scores = []
    for r in results:
        expect = set(questions[r.qid].expect)
        if not expect:
            continue
        got = set(r.hit_expect)
        scores.append(len(got & expect) / len(expect | got))
    if not scores:
        return 0.0, 0
    return sum(scores) / len(scores), len(scores)


def _keyword_baseline_arm(core: MemoryCore, scope: Scope, qs: list[Question]) -> ArmResult:
    """关键词基线臂（C4/A19 对照）：只按 BM25 关键词直查库，无四通道融合、无注入。

    对每题：BM25 检索题干 → 取 active 且非 shadow 的记忆 top-8 内容拼成"作答"，
    判分＝要点子串覆盖。拒答题：无命中才算对。它量化"单纯关键词 vs 四通道融合"的增益。
    """
    arm = ArmResult(name="关键词基线")
    session = core._session(scope)  # noqa: SLF001 - 评测是内部工具
    idx = rt.build_bm25(session.conn)
    for q in qs:
        hits = rt.bm25_search(idx, q.text)
        contents: list[str] = []
        for doc_id in hits:
            if str(doc_id).startswith("ep_"):
                continue
            row = session.conn.execute(
                "SELECT content, status, shadow FROM memories WHERE id=?", (doc_id,)
            ).fetchone()
            if row and row["status"] == "active" and not (row["shadow"] or 0):
                contents.append(row["content"])
        text = "\n".join(contents)
        if q.check == "refusal":
            ok = not contents
        else:
            ok = bool(q.expect) and all(e in text for e in q.expect)
        arm.results.append(
            QuestionResult(
                qid=q.qid,
                kind=q.kind,
                source=q.source,
                ok=ok,
                steps=1,
                answer=f"[关键词基线]\n{text[:200]}",
                note="仅 BM25 关键词直查库（无四通道融合）",
            )
        )
        arm.total += 1
        arm.steps += 1
        if ok:
            arm.passed += 1
    arm.pass_k_score = arm.success_rate
    return arm


def run_bundle(
    core: MemoryCore,
    scope: Scope,
    *,
    questions: int = 10,
    memories: bool = False,
    offline: bool = True,
    max_steps: int = 6,
    workdir: Any = None,
    pass_k: int = 1,
    model_arm: bool = False,
    baseline: bool = False,
) -> EvalReport:
    """跑一批题。`memories=True` 时额外跑"记忆关"对照臂。

    N8（C1–C4）：
    - `pass_k`：每题重复 k 次（默认 1），全过才算过，报 pass^k 分数；
    - `model_arm`：有模型端点（非离线）时同一套题走模型作答器；
    - `baseline`：加"关键词基线"对照臂（仅 BM25 直查库）。
    """
    qs = questions_for(questions)
    qmap = {q.qid: q for q in qs}
    seed_info = _ensure_seeded(core, scope)
    report = EvalReport(mode="offline" if offline else "model", pass_k=max(int(pass_k), 1),
                        baseline=baseline, model_arm=model_arm)
    report.notes.append(f"示例数据：本次新灌 {seed_info.get('count', 0)} 条（合成数据，含已知真值）")

    def _arm(name: str, use_offline: bool = True) -> ArmResult:
        arm = ArmResult(name=name, pass_k=max(int(pass_k), 1))
        for q in qs:
            runs: list[QuestionResult] = []
            for i in range(max(int(pass_k), 1)):
                q_scope = Scope(account=scope.account, session=f"eval-{q.qid}-{i}")
                runs.append(_run_question(core, q_scope, q, max_steps=max_steps, workdir=workdir, offline=use_offline))
            ok_all = all(r.ok for r in runs)
            result = runs[0]
            result.ok = ok_all
            if arm.pass_k > 1:
                result.note = (result.note + f" | pass^{arm.pass_k}: {sum(r.ok for r in runs)}/{len(runs)}").strip()
            arm.results.append(result)
            arm.total += 1
            arm.steps += result.steps
            if ok_all:
                arm.passed += 1
            else:
                arm.failures[result.failure or "未归类"] = arm.failures.get(result.failure or "未归类", 0) + 1
        arm.pass_k_score = arm.success_rate
        return arm

    on_arm = _arm("记忆开")
    report.arms.append(on_arm)
    report.label_agreement, report.label_pairs = _label_agreement(on_arm.results, qmap)

    if memories:
        core.set_switch(scope, "关闭记忆")
        try:
            off_arm = _arm("记忆关")
        finally:
            core.set_switch(scope, "打开记忆")
        report.arms.append(off_arm)

    # N8：关键词基线对照（C4/A19）
    if baseline:
        report.arms.append(_keyword_baseline_arm(core, scope, qs))

    # N8：模型臂（C3）——有端点且非离线时才跑；请求了但条件不满足就明说原因
    if model_arm:
        if offline:
            report.notes.append("模型臂跳过：离线档（--offline）")
        else:
            from hippocampus.settings import endpoint_model, llm_available

            if llm_available():
                report.model = endpoint_model()
                report.arms.append(_arm(f"模型臂（{report.model}）", use_offline=False))
            else:
                report.notes.append("模型臂跳过：未配置模型端点（无凭据）")

    # 边界声明动态化：含 k 与模型名（C1 口径：边界声明必须含 k 与模型名）
    model_part = f"模型臂={report.model or '无（离线规则作答器）'}"
    report.boundary = (
        f"边界声明：题量 {len(qs)}（{sum(1 for q in qs if q.kind == 'qa')} 问 "
        f"{sum(1 for q in qs if q.kind == 'tool')} 动作）；pass^k 的 k={report.pass_k}；{model_part}；"
        "来源分列：synthetic=合成示例数据（证方法可复现），real-jd=真实岗位 JD 派生场景"
        "（**脱敏**，不含私人 JD 原文）；效果结论不作普适承诺。"
    )
    return report


__all__ = ["run_bundle"]
