"""N22 官方判分臂（model_arm）：prompt 保真、判分对拍、护栏与聚合。

全部**离线可跑**（不发起任何模型调用）：模型调用路径 mock 掉，只测
①官方 prompt/判分口径是否钉得住 ②护栏（重试/预算/余额）符合 T2
③run_official 的聚合与失败/跳过上报。判分与官方仓库的对拍（全量抽样）
是开发期验证过的（与 snap-research/locomo @3eb6f2c 的 evaluation.py
在 400 题上逐题一致），这里钉住代表值防回归。
"""

from __future__ import annotations

import pytest

from hippocampus.eval import model_arm as ma

# ----------------------------------------------------------------------
# LoCoMo 官方判分（port 保真）
# ----------------------------------------------------------------------


def test_loco_normalize_answer_matches_official():
    # 官方 remove_punc 是"删除"标点（join），不是替换为空格：it's -> its
    assert ma.loco_normalize_answer("Yes; it's classical music") == "yes its classical music"
    assert ma.loco_normalize_answer("Hello, World!") == "hello world"
    assert ma.loco_normalize_answer("THE CAT and THE DOG") == "cat dog"  # a/an/the/and 按词边界删
    assert ma.loco_normalize_answer("a") == ""  # 单字母 a 也是冠词
    assert ma.loco_normalize_answer("") == ""


def test_loco_f1_score_known_values():
    assert ma.loco_f1_score("7 May 2023", "7 May 2023") == pytest.approx(1.0)
    assert ma.loco_f1_score("The week before 9 June 2023", "9 June 2023") == pytest.approx(0.75)
    assert ma.loco_f1_score("Transgender woman", "woman") == pytest.approx(2 / 3)
    assert ma.loco_f1_score("", "x") == 0.0
    assert ma.loco_f1_score("A B", "A B") == 1.0  # 冠词 a 被删后 f1('a','a')=0 的官方怪癖不在这


def test_loco_f1_multi_matches_official_quirk():
    # 官方多答案：拆逗号两两取 max；'A' 被当冠词删掉 -> f1('A','A')=0
    assert ma.loco_f1_multi("A, B", "A, B") == pytest.approx(0.5)
    assert ma.loco_f1_multi("A", "A, B") == 0.0
    assert ma.loco_f1_multi("X, Y", "Y") == pytest.approx(1.0)
    assert ma.loco_f1_multi("", "A") == 0.0
    assert ma.loco_f1_multi(
        "Psychology, counseling certification", "Psychology, counseling certification"
    ) == pytest.approx(1.0)


def test_loco_cat5_correct():
    assert ma.loco_cat5_correct("Not mentioned in the conversation") == 1.0
    assert ma.loco_cat5_correct("There is no information available.") == 1.0
    assert ma.loco_cat5_correct("self-care is important") == 0.0
    assert ma.loco_cat5_correct("") == 0.0


def test_loco_question_prompt_cat2_and_cat5():
    # cat2：日期后缀追加在 question 里（官方 gpt_utils.py 先拼 question 再 QAQ_PROMPT）
    prompt, key = ma.loco_question_prompt(2, "When did X?", "gold", ma._Lcg(1))
    assert prompt.endswith(
        "Question: When did X? Use DATE of CONVERSATION to answer with an approximate date. Short answer:"
    )
    assert key == {}
    # cat5：变成 (a)/(b) 选项题，两选项固定是"Not mentioned" + 对抗答案
    rng = ma._Lcg(1234)
    prompt, key = ma.loco_question_prompt(5, "Q?", "distractor", rng)
    assert set(key.values()) == {"Not mentioned in the conversation", "distractor"}
    assert "Select the correct answer: (a) " in prompt and "(b) " in prompt
    # 同种子可复现（官方用 random，这里用固定 LCG，跨进程一致）
    p2, k2 = ma.loco_question_prompt(5, "Q?", "distractor", ma._Lcg(1234))
    assert prompt == p2 and key == k2
    # cat1/3/4：无选项包装
    prompt, key = ma.loco_question_prompt(1, "Q?", "g", ma._Lcg(1))
    assert "Select the correct answer" not in prompt


def test_loco_get_cat5_answer_mapping():
    key = {"a": "Not mentioned in the conversation", "b": "distractor"}
    assert ma.loco_get_cat5_answer("a", key) == key["a"]
    assert ma.loco_get_cat5_answer("B", key) == key["b"]
    assert ma.loco_get_cat5_answer(" (a) ", key) == key["a"]
    assert ma.loco_get_cat5_answer("the answer is b", key) == "the answer is b"  # 长文本原样返回


def test_loco_score_one_category3_splits_at_semicolon():
    # 官方 eval_question_answering：cat3 先取 ';' 前第一段再判分
    gold = "LIkely no; though she likes reading"
    assert ma.loco_score_one(3, "LIkely no", gold) == pytest.approx(1.0)
    # cat1 走多答案；cat5 走拒答判据
    assert ma.loco_score_one(1, "A, B", "A, B") == pytest.approx(0.5)
    assert ma.loco_score_one(5, "not mentioned", "anything") == 1.0


# ----------------------------------------------------------------------
# LongMemEval 官方 judge prompt / 标签
# ----------------------------------------------------------------------


def test_lme_judge_prompt_type_templates():
    p = ma.lme_judge_prompt("temporal-reasoning", "Q?", "A", "R", abstention=False)
    assert "off-by-one errors" in p and "Question: Q?\n\nCorrect Answer: A\n\nModel Response: R" in p
    p = ma.lme_judge_prompt("knowledge-update", "Q?", "A", "R", abstention=False)
    assert "updated answer" in p
    p = ma.lme_judge_prompt("single-session-preference", "Q?", "A", "R", abstention=False)
    assert "Rubric: A" in p
    p = ma.lme_judge_prompt("single-session-user", "Q?", "A", "R", abstention=False)
    assert "Is the model response correct? Answer yes or no only." in p
    # 未知题型退回 short 模板（官方同样只有明确分支）
    p = ma.lme_judge_prompt("unknown-type", "Q?", "A", "R", abstention=False)
    assert "Is the model response correct?" in p


def test_lme_judge_prompt_abstention():
    p = ma.lme_judge_prompt("temporal-reasoning", "Q?", "EXP", "R", abstention=True)
    assert "unanswerable question" in p and "Explanation: EXP" in p
    assert "off-by-one" not in p  # abstention 走独立模板


def test_lme_label():
    assert ma.lme_label("Yes") is True
    assert ma.lme_label("yes.") is True
    assert ma.lme_label(" No ") is False
    assert ma.lme_label("Yesterday") is True  # 官方口径：'yes' in lower
    assert ma.lme_label("") is False


# ----------------------------------------------------------------------
# 统计（CI）
# ----------------------------------------------------------------------


def test_wilson_ci_known_values():
    lo, hi = ma.wilson_ci(200, 170)
    assert lo == pytest.approx(0.7939, abs=1e-4)
    assert hi == pytest.approx(0.8929, abs=1e-4)
    assert ma.wilson_ci(0, 0) == (0.0, 0.0)
    lo2, hi2 = ma.wilson_ci(200, 100)
    assert lo2 == pytest.approx(0.4314, abs=1e-4) and hi2 == pytest.approx(0.5686, abs=1e-4)


def test_bootstrap_ci_deterministic_and_contains_mean():
    values = [0.0] * 20 + [1.0] * 80
    a = ma.bootstrap_ci(values, seed=42)
    b = ma.bootstrap_ci(values, seed=42)
    assert a == b  # 固定种子可复跑
    assert a[0] <= 0.8 <= a[1]
    assert ma.bootstrap_ci([]) == (0.0, 0.0)


# ----------------------------------------------------------------------
# 调用链护栏（重试 / 预算）
# ----------------------------------------------------------------------


def test_call_stats_budget_hard_stop():
    stats = ma._CallStats(budget_yuan=1.0)
    assert stats.estimated_cost() == 0.0
    # 400K prompt tokens @ ¥2/M = ¥0.8 + 100 completion @ ¥8/M ≈ ¥0.0008 -> 未超
    stats.prompt_tokens = 400_000
    stats.completion_tokens = 100
    stats.check_budget()
    # 泵到 ≥ ¥1 -> 硬停
    stats.prompt_tokens = 500_000
    with pytest.raises(ma.BudgetExceeded):
        stats.check_budget()


def test_call_chat_retry_then_success(monkeypatch):
    calls = {"n": 0}

    def fake_post_json(payload, timeout_s=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return 500, {"error": "boom"}  # 第一次失败
        return 200, {
            "choices": [{"message": {"content": "42"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    monkeypatch.setattr("hippocampus.memory.llm.post_json", fake_post_json)
    stats = ma._CallStats(budget_yuan=30.0)
    out = ma.call_chat("hi", max_tokens=10, kind="answer", stats=stats, retries=2, timeout_s=5.0)
    assert out == "42"
    assert calls["n"] == 2  # 重试 1 次成功
    assert stats.answer_calls == 2
    assert stats.prompt_tokens == 10  # 失败那次不计 usage


def test_call_chat_fails_after_retries(monkeypatch):
    def fake_post_json(payload, timeout_s=None):
        return 429, {"error": "rate limit"}

    monkeypatch.setattr("hippocampus.memory.llm.post_json", fake_post_json)
    stats = ma._CallStats(budget_yuan=30.0)
    with pytest.raises(RuntimeError):
        ma.call_chat("hi", max_tokens=10, kind="judge", stats=stats, retries=2, timeout_s=5.0)
    assert stats.judge_calls == 3  # 初试 + 2 重试
    assert stats.failures  # 失败记档


# ----------------------------------------------------------------------
# run_official 聚合（mock 掉全部模型调用与余额查询）
# ----------------------------------------------------------------------


def _locomo_items(n=6):
    return [
        ma.ArmItem(
            qid=f"c-q{i}",
            category={0: "cat1", 1: "cat2", 2: "cat3", 3: "cat4", 4: "cat5", 5: "cat5"}[i % 6],
            question=f"Q{i}?",
            answers=["the cat sat on mat"],
            context="ctx " + str(i),
        )
        for i in range(n)
    ]


def test_run_official_locomo_aggregates(monkeypatch):
    # 模型回答：非 cat5 答参考答案、cat5（选项题）答"not mentioned" -> 官方 F1 应全 1.0
    def fake_call(user, **kw):
        return "not mentioned in the conversation" if "Select the correct answer" in user else "the cat sat on mat"

    monkeypatch.setattr(ma, "call_chat", fake_call)
    monkeypatch.setattr(ma, "fetch_balance_cny", lambda: 30.0)
    report = ma.run_official("locomo", _locomo_items(), budget_yuan=30.0)
    off = report["official"]
    assert off["f1"] == pytest.approx(1.0)
    assert off["n"] == 6
    assert report["n_done"] == 6 and report["n_failed"] == 0 and report["n_skipped"] == 0
    assert report["balance_before"] == 30.0 and report["spend_yuan"] == 0.0
    assert report["input_spec"] == "记忆层注入上下文（top-8，inject_finalize 原文）；不是全文"
    assert 0 <= off["ci95"][0] <= off["f1"] <= off["ci95"][1] <= 1.0


def test_run_official_lme_labels_and_accuracy(monkeypatch):
    # 作答任意、judge 一律 yes -> 准确率 100%；含 _abs abstention 题不崩
    def fake_call(user, **kw):
        return "Yes" if kw.get("kind") == "judge" else "I guess 42"

    monkeypatch.setattr(ma, "call_chat", fake_call)
    monkeypatch.setattr(ma, "fetch_balance_cny", lambda: 30.0)
    items = [
        ma.ArmItem(
            qid=f"gpt4_x{i}" + ("_abs" if i % 2 else ""),
            category=["temporal-reasoning", "knowledge-update", "single-session-user", "multi-session"][i % 4],
            question=f"Q{i}?",
            answers=["A"],
            context="ctx",
            extra={"question_date": "2023/04/10 (Mon) 23:07"},
        )
        for i in range(8)
    ]
    report = ma.run_official("longmemeval", items, budget_yuan=30.0)
    off = report["official"]
    assert off["accuracy"] == pytest.approx(1.0)
    assert off["n"] == 8
    assert report["n_done"] == 8
    assert off["ci95"][0] <= 1.0 <= off["ci95"][1]
    assert len(off["by_category"]) == 4  # 按题型分组


def test_run_official_reports_failures_honestly(monkeypatch):
    calls = {"n": 0}

    def flaky(user, **kw):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(ma, "call_chat", flaky)
    monkeypatch.setattr(ma, "fetch_balance_cny", lambda: None)  # 余额查询失败也如实
    items = _locomo_items(6)
    report = ma.run_official("locomo", items, retries=0, budget_yuan=30.0)
    assert report["n_failed"] == 6 and report["n_done"] == 0
    assert report["official"]["f1"] is None  # 不许拿空集冒充全量
    assert report["failures"]
    assert report["balance_before"] is None and report["spend_yuan"] is None


def test_run_official_budget_skip_path(monkeypatch):
    # 预算硬停语义：check_budget 抛 BudgetExceeded 时整题记 skipped，官方分数如实置 None
    def fake_call(user, **kw):
        raise ma.BudgetExceeded("预算硬停")

    monkeypatch.setattr(ma, "call_chat", fake_call)
    monkeypatch.setattr(ma, "fetch_balance_cny", lambda: 100.0)
    report = ma.run_official("locomo", _locomo_items(6), budget_yuan=0.0)
    assert report["n_skipped"] == 6
    assert report["n_done"] == 0
    assert report["official"]["f1"] is None


# ----------------------------------------------------------------------
# 显式开关：离线默认行为零变化
# ----------------------------------------------------------------------


def _tiny_locomo(tmp_path):
    import json

    data = [
        {
            "sample_id": "conv-x",
            "conversation": {
                "session_1_date_time": "1:56 pm on 8 May, 2023",
                "session_1": [
                    {"speaker": "A", "dia_id": "D1:1", "text": "I adopted a cat named Momo."},
                    {"speaker": "B", "dia_id": "D1:2", "text": "That is lovely."},
                ],
            },
            "qa": [{"question": "What is my cat called?", "answer": "Momo", "evidence": ["D1:1"], "category": 4}],
        }
    ]
    path = tmp_path / "locomo_tiny.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_capture_context_off_by_default(tmp_path):
    from hippocampus.core import MemoryCore
    from hippocampus.eval import public_bench as pb

    data = _tiny_locomo(tmp_path)
    core = MemoryCore(home=str(tmp_path / "home-default"))
    try:
        items = pb.load_locomo(data)
        report = pb.run_items(core, items)
        assert "context" not in report["rows"][0]  # 默认不加字段 => 行为零变化
    finally:
        core.close()
    # 显式开才带注入上下文（独立库避免重复导入串味）；口径指标应与默认完全一致
    core2 = MemoryCore(home=str(tmp_path / "home-capture"))
    try:
        report2 = pb.run_items(core2, items, capture_context=True)
        ctx = report2["rows"][0]["context"]
        assert isinstance(ctx, str) and len(ctx) > 0
        for key in ("n", "answer_in_context", "evidence_in_context", "token_f1", "abstain_rate"):
            assert report["overall"][key] == report2["overall"][key]
    finally:
        core2.close()


def test_model_arm_gates_offline_and_endpoint(monkeypatch, tmp_path):
    from hippocampus.eval import public_bench as pb

    data = _tiny_locomo(tmp_path)
    # 闸 1：离线档下开模型臂 = 拒绝（不许静默出站）
    monkeypatch.setenv("HIPPOCAMPUS_OFFLINE", "1")
    with pytest.raises(RuntimeError, match="模型臂要求非离线档"):
        pb.run(bench="locomo", data=data, home=str(tmp_path / "home-arm"), model_arm=True)
    # 闸 2：非离线但没配端点/凭据 = 拒绝
    monkeypatch.delenv("HIPPOCAMPUS_OFFLINE", raising=False)
    for k in ("HIPPOCAMPUS_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "HIPPOCAMPUS_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("hippocampus.memory.config._read_from_keyring", lambda: "")
    with pytest.raises(RuntimeError, match="配好模型端点与凭据"):
        pb.run(bench="locomo", data=data, home=str(tmp_path / "home-arm2"), model_arm=True, allow_online=True)
