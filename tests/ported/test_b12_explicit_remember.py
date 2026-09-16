# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点4（B1-2 显式「记住」指令）隔离单测。

「记住X」→ 存一条记忆并确认；「忘掉X」→ 相关记忆降权（superseded，不物理删除）。
自验：显式记住端到端用例（user 语料 → 记忆 → 确认反馈）。
绝不碰真实库：账户目录 monkeypatch 到 tmp_path。
"""

import pytest

from hippocampus.memory import extract
from hippocampus.memory import memory_bridge as mb


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """临时账户 MemorySession（monkeypatch 账户目录，绝不碰真实账户）。
    零 LLM 确定性：extract.chat_json mock 为空 → 记住实体走机械兜底（jieba）。"""
    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    monkeypatch.setattr(
        extract, "chat_json",
        lambda system, user, max_tokens: {"entities": [], "memories": []},
    )
    sess = mb.MemorySession(f"b12_{tmp_path.name[-8:]}")
    yield sess
    mb.drop_bridge(sess.account_id)
    import shutil

    shutil.rmtree(str(tmp_path / f"acct_{sess.account_id}"), ignore_errors=True)


def test_remember_preference_end_to_end(session):
    """端到端：说「记住我讨厌香菜」→ preference 入库 + 确认反馈（经 handle_confirmation
    = proxy 在线入口，无需改 proxy 即生效）。"""
    msg = mb.handle_confirmation(session, "记住我讨厌香菜")
    assert msg is not None and "已记住" in msg and "香菜" in msg
    row = session.conn.execute(
        "SELECT * FROM memories WHERE content LIKE '%香菜%' AND status='active'"
    ).fetchone()
    assert row is not None
    assert row["type"] == "preference"  # 含「讨厌」→ 行为约束类


def test_remember_fact(session):
    """「记住项目代号是海马」→ fact 入库（无行为约束词）。"""
    msg = mb.handle_confirmation(session, "记住项目代号是海马")
    assert msg is not None and "已记住" in msg
    row = session.conn.execute(
        "SELECT * FROM memories WHERE content LIKE '%海马%' AND status='active'"
    ).fetchone()
    assert row is not None
    assert row["type"] == "fact"


def test_remember_duplicate_skipped(session):
    """重复记住同一内容 → 不制造副本（去重），返回「已记住（重复）」反馈。"""
    mb.handle_confirmation(session, "记住我讨厌香菜")
    msg2 = mb.handle_confirmation(session, "记住我讨厌香菜")
    assert "重复" in msg2
    n = session.conn.execute("SELECT COUNT(*) FROM memories WHERE content LIKE '%香菜%'").fetchone()[0]
    assert n == 1


def test_remember_with_colon_and_whitespace(session):
    """「记住：X」「记住 X」变体都命中。"""
    assert mb.handle_confirmation(session, "记住：备份要带 sha256") is not None
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%sha256%'").fetchone()
    assert row is not None


def test_forget_downgrades_memory(session):
    """「忘掉X」→ 相关 active 记忆标 superseded（降权不注入；不物理删除）。"""
    mb.handle_confirmation(session, "记住数据库用 SQLite")
    msg = mb.handle_confirmation(session, "忘掉数据库")
    assert msg is not None and "已忘掉" in msg and "1 条" in msg
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%SQLite%'").fetchone()
    assert row is not None  # 不物理删除
    assert row["status"] == "superseded"  # 降权


def test_forget_no_match(session):
    """忘掉不存在的内容 → 友好反馈，无副作用。"""
    msg = mb.handle_confirmation(session, "忘掉不存在的主题词xyz")
    assert msg is not None and "未找到" in msg
    assert session.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_normal_message_not_intercepted(session):
    """普通对话不被显式记忆指令拦截（返回 None 走正常对话）。"""
    assert mb.handle_confirmation(session, "今天天气不错") is None
    assert mb.handle_confirmation(session, "记得买牛奶") is None  # 含「记得」但不以「记住」开头
    assert mb.handle_confirmation(session, "我不想吃香菜") is None  # 不是指令句式


def test_explicit_remember_direct_api(session):
    """handle_explicit_memory 纯 DB 入口（cli_chat 等调用方可用）。"""
    msg = mb.handle_explicit_memory(session.conn, "记住我喜欢简洁的回复")
    assert msg is not None and "已记住" in msg
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%简洁%'").fetchone()
    assert row is not None and row["type"] == "preference"


# ── 打回补丁（08-16）：短语气词/助词不能当记住/忘掉对象 ────────────────────


def test_forget_particle_only_noop(session):
    """整句「忘掉了」不是指令：关键词剥语气词后为空 → 不改任何已有记忆
    （含「了」的中文记忆不得被 LIKE %了% 误标 superseded）。"""
    mb.handle_confirmation(session, "记住数据库用 SQLite")
    mb.handle_confirmation(session, "记住我喜欢简洁的回复")
    before = session.conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
    assert before == 2
    assert mb.handle_confirmation(session, "忘掉了") is None
    after = session.conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
    assert after == before, "「忘掉了」不得改动任何记忆"


def test_remember_particle_only_no_insert(session):
    """整句「记住了」不是指令：不入库垃圾事实（内容为「了」）。"""
    assert mb.handle_confirmation(session, "记住了") is None
    assert session.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_forget_particle_ba_noop(session):
    """「忘掉吧」不是指令：不改库。"""
    mb.handle_confirmation(session, "记住数据库用 SQLite")
    assert mb.handle_confirmation(session, "忘掉吧") is None
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%SQLite%'").fetchone()
    assert row is not None and row["status"] == "active"


def test_wo_wangdiao_not_command(session):
    """「我忘掉了」仍不是指令（不以「忘掉」开头，行为保持）。"""
    mb.handle_confirmation(session, "记住数据库用 SQLite")
    assert mb.handle_confirmation(session, "我忘掉了") is None
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%SQLite%'").fetchone()
    assert row is not None and row["status"] == "active"


def test_remember_ji_de_not_command(session):
    """「记得买牛奶」仍不是指令（不以「记住」开头，行为保持）。"""
    assert mb.handle_confirmation(session, "记得买牛奶") is None
    assert session.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_legal_commands_still_work(session):
    """合法指令仍可用：记住/忘掉带实义内容正常生效（含剥语气词后的尾助词变体）。"""
    # 记住我讨厌香菜
    assert mb.handle_confirmation(session, "记住我讨厌香菜") is not None
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%香菜%'").fetchone()
    assert row is not None and row["status"] == "active"
    # 记住数据库用 SQLite
    assert mb.handle_confirmation(session, "记住数据库用 SQLite") is not None
    # 忘掉数据库（含尾助词变体「忘掉数据库了」剥为「数据库」→ 只命中 SQLite 记忆）
    msg = mb.handle_confirmation(session, "忘掉数据库了")
    assert msg is not None and "已忘掉" in msg
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%SQLite%'").fetchone()
    assert row["status"] == "superseded"
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%香菜%'").fetchone()
    assert row["status"] == "active"
