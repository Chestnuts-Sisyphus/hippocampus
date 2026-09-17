"""T2 小修批量-A（缺口 A10/E2/E3/E5/E6）的验收测试。

每条对应一个缺口：
- A10：explain 审计事件按 run_id 贯通（同 query 两步不串）；
- E3：bench_diag 同会话/邻居判据＝整句精确匹配（短前缀高估被修）；
- E5：bench_ab --reuse-import 判定看 memories+episodes 双表；
- E6：embedding 下载失败清理 .tmp 与空子目录，错误信息带"已重试镜像"；
- E2：docs/benchmark.md 补 bench_diag 用法（文本断言，防回退）。
"""

from __future__ import annotations

import pytest

from hippocampus.core import Scope

# ---------- A10：explain run_id 贯通 ----------


def _fake_audit_event(run_id: str, injected_ids: list[str]) -> dict:
    return {
        "event": "audit.retrieval",
        "ts": 0,
        "query": "同一句话",
        "candidates": [{"doc_id": i, "kind": "memory", "channel": "semantic", "score": 1.0,
                        "injected": True, "dropped": False, "reason": ""} for i in injected_ids],
        "injected_ids": injected_ids,
        "total_candidates": len(injected_ids),
        "capped": 0,
        "run_id": run_id,
    }


def test_inject_finalize_stamps_distinct_run_id(core, scope):
    """同 query 两次注入 → 两次 audit 事件带**不同** run_id，且与注入结果一致。"""
    from hippocampus.memory import audit as audit_mod

    core.write(scope, "我只看允许远程的岗位", kind="preference")
    inj1 = core.inject_finalize(scope, "我只看允许远程的岗位")
    inj2 = core.inject_finalize(scope, "我只看允许远程的岗位")
    assert inj1.run_id and inj2.run_id
    assert inj1.run_id != inj2.run_id

    events = audit_mod.load_events(None, account_id="test")
    assert len(events) == 2
    run_ids = [ev.get("run_id") for ev in events]
    assert inj1.run_id in run_ids and inj2.run_id in run_ids


def test_explain_matches_audit_by_run_id_not_query(core, scope):
    """同 query 两步：run_id 各归各位，不串步（A10 的核心验收）。"""
    from hippocampus.explain import _match_audit_events

    ev1 = _fake_audit_event("r1", ["m1"])
    ev2 = _fake_audit_event("r2", ["m2"])

    step1 = {"run_id": "r1", "injected_ids": ["m1"], "query": "同一句话"}
    step2 = {"run_id": "r2", "injected_ids": ["m2"], "query": "同一句话"}
    matched1 = _match_audit_events(step1, [ev1, ev2], "同一句话")
    matched2 = _match_audit_events(step2, [ev1, ev2], "同一句话")
    assert [e["run_id"] for e in matched1] == ["r1"]
    assert [e["run_id"] for e in matched2] == ["r2"]


def test_explain_falls_back_to_heuristic_for_legacy_events(core, scope):
    """旧版审计事件（无 run_id）仍走启发式兜底：query 相同照常匹配（不破坏旧数据复盘）。"""
    from hippocampus.explain import _match_audit_events

    legacy = _fake_audit_event("", ["m1"])
    legacy["run_id"] = ""
    step = {"run_id": "", "injected_ids": ["m1"]}
    matched = _match_audit_events(step, [legacy], "同一句话")
    assert matched, "旧事件启发式兜底不能丢"


# ---------- E3：bench_diag 整句精确判据 ----------


def test_classify_miss_prefix_collision_is_not_counted():
    """短前缀撞上别的轮不再算"同会话在上下文"（E3/G8 修正：宁可低估不高估）。"""
    from scripts.bench_diag import classify_miss  # noqa: E402

    turn_norms = [
        "今天开会讨论了季度目标和全年预算",
        "我们把预算砍半了，工资也一起减了",
        "结论是下季度再评估",
    ]
    # ctx 里只有 turn0 的前缀（9 字）作为另一段文本的一部分，任何轮**整句**都不在
    ctx = "今天开会讨论了季度目标的问题（别人说的）"
    nb_hit, sess_hit = classify_miss(turn_norms, ctx, evidence_turn_idxs=[2])
    assert nb_hit is False and sess_hit is False
    # 非相邻轮整句在 → 同会话其它轮成立，但证据轮邻居（idx 1/出界）不成立
    nb_hit, sess_hit = classify_miss(
        turn_norms, ctx + " 今天开会讨论了季度目标和全年预算", evidence_turn_idxs=[2]
    )
    assert nb_hit is False and sess_hit is True


def test_classify_miss_neighbor_whole_turn():
    """邻居判据＝整句在上下文；前缀命中不算邻居。"""
    from scripts.bench_diag import classify_miss  # noqa: E402

    turns = ["第一轮开始", "证据轮内容很长的一句话", "下一轮收尾"]
    nb, sess = classify_miss(turns, "下一轮收尾", evidence_turn_idxs=[1])
    assert nb is True  # 下一轮整句在
    nb, sess = classify_miss(turns, "下一轮收", evidence_turn_idxs=[1])
    assert nb is False  # 只有 4 字前缀 → 不算


# ---------- E5：reuse-import 双表 ----------


def test_reuse_import_counts_episodes_table(core, scope):
    """只有经历层（episodes）没有记忆条的账户，也算"已导入"（E5/G10）。"""
    from scripts.bench_ab import _already_ingested  # noqa: E402

    core.ingest_history(
        scope,
        [
            {
                "text": "好的，我帮你整理一下",
                "role": "assistant",
                "as_memory": False,  # 只落经历层（split_pools 同款口径）
                "as_episode": True,
            }
        ],
    )
    assert _already_ingested(core, scope) is True

    fresh = Scope(account="untouched", session="bench", source="user")
    assert _already_ingested(core, fresh) is False


# ---------- E6：下载失败清理与错误信息 ----------


def test_download_failure_cleans_tmp_and_says_mirror(home, monkeypatch):
    """下载失败：.tmp 与空子目录被清掉；错误信息带"已重试镜像"（E6/G11）。"""
    from hippocampus.memory import embedding_models as em

    target = em._model_dir("Xenova/bge-small-en-v1.5")  # noqa: SLF001
    target.mkdir(parents=True, exist_ok=True)
    onnx_dir = target / "onnx"
    onnx_dir.mkdir(parents=True, exist_ok=True)
    (onnx_dir / "model.onnx.tmp").write_bytes(b"partial")
    (target / "tokenizer.json.tmp").write_bytes(b"partial")

    monkeypatch.setattr(em, "_fetch_lfs_sha", lambda ep, repo: {})
    em.set_http_fetcher(lambda url, timeout: (_ for _ in ()).throw(RuntimeError("网络中断")))

    with pytest.raises(RuntimeError) as err:
        em._download_repo("Xenova/bge-small-en-v1.5")
    assert "已重试镜像" in str(err.value)
    leftovers = list(target.rglob("*.tmp"))
    assert leftovers == [], f"残留 .tmp: {leftovers}"
    assert not onnx_dir.exists(), "空子目录 onnx/ 应被清理"


def test_benchmark_docs_mention_bench_diag_usage():
    """E2/G7：docs/benchmark.md 含 bench_diag 用法一节（防回退断言）。"""
    text = (__import__("pathlib").Path("docs/benchmark.md")).read_text(encoding="utf-8")
    assert "### 二·二b 失败归因（`bench_diag`）" in text
    assert "scripts/bench_diag.py" in text
    assert "--json D:/tmp/hc-bench/diag_neighbors.json" in text
