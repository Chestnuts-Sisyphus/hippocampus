"""离线句式抽取的两条硬边界：**疑问句不入库、祈使句不入库**。

为什么单独立一条测试（真实事故）：切句时把"？"当分隔符丢掉了，于是
「我投简历有什么要求？」被剥成陈述句、命中"我+投"句式、**被当成偏好存进记忆**。
下一轮用户问同一句话时，这条记忆与查询逐字相似（similarity 0.95）→ 独占语义通道
（断崖截断后只剩它一条）→ 又被"防重复注入已见内容"过滤 → **注入被挤成 0**，
表现为"代理明明有记忆却检索不到"。链路：抽取器误判 → 检索中毒 → 注入为空。
"""

from __future__ import annotations

from hippocampus.memory.extract import extract_by_rules


def _kinds(text: str) -> list[tuple[str, str]]:
    return [(m["type"], m["content"]) for m in extract_by_rules(text)["memories"]]


def test_question_with_trailing_mark_is_not_stored():
    """带问号的疑问句不入库（事故原形）。"""
    assert _kinds("我投简历有什么要求？") == []


def test_question_without_mark_is_not_stored():
    """疑问词在句中（且没有问号）也不入库——不能只查句首。"""
    assert _kinds("我投简历有什么要求") == []
    assert _kinds("我找岗位时有哪些硬性限制") == []


def test_interrogative_words_anywhere_are_rejected():
    for text in ("我该怎么选岗位", "我投哪些岗位合适", "我是不是应该先刷题", "我最近投了多少家"):
        assert _kinds(text) == [], text


def test_imperative_sentences_are_not_stored():
    """祈使句是任务指令，不是关于用户的陈述。"""
    for text in ("帮我看看这个岗位怎么样", "把我的偏好写成文件", "列出我的全部记忆", "导出成 markdown"):
        assert _kinds(text) == [], text


def test_declaratives_are_still_stored():
    """反例：真正的陈述句必须照常入库（别把边界收得太紧）。"""
    assert _kinds("我只投含 MCP 的岗位。") == [("preference", "我只投含 MCP 的岗位")]
    assert _kinds("我讨厌打补丁式设计") == [("preference", "我讨厌打补丁式设计")]
    assert _kinds("我的期望城市是北京。") == [("fact", "我的期望城市是北京")]
    assert _kinds("我现在在准备实习申请。") == [("status", "我现在在准备实习申请")]


def test_mixed_message_keeps_only_the_statement():
    """一句话里既有陈述又有问题：只留陈述部分（且入库内容不带句末标点）。"""
    got = _kinds("我不看外包。我投简历有什么要求？")
    assert got == [("preference", "我不看外包")]


def test_stored_content_has_no_trailing_punctuation():
    got = _kinds("我只看允许远程的岗位。")
    assert got and not got[0][1].endswith(("。", "！", "？"))


def test_question_shaped_memory_cannot_zero_out_injection(core, scope):
    """端到端：问过的问题不会变成记忆、也就不会把后续注入挤空。

    直接复现事故链路——先"问一轮"（走固化的句式抽取），再检索同一句话，
    断言仍能命中**真正的偏好**（而不是被一条问句形状的记忆顶掉）。
    """
    from hippocampus.seed import seed

    seed(core, scope)
    question = "我投简历有什么要求？"

    # 第一轮：模拟代理/Agent 把这个问句送去固化（离线档走句式规则抽取）
    turn = core.consolidate(scope, user_text=question)
    stored = [m.content for m in core.list_memories(scope, limit=50)]
    assert question.rstrip("？") not in stored, "问句不该被当成记忆存下来"

    # 第二轮：同一句话再检索，真正的偏好仍应出现在注入里
    injection = core.inject_finalize(scope, question)
    contents = [i.content for i in injection.items]
    assert any("错别字" in c for c in contents), f"注入被挤空了：{contents}"
    assert turn is not None  # 固化本身不报错
