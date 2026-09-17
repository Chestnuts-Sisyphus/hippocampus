"""N21 embedding 函数进程级 memo（B1 真 blocker 的回归测试）。

背景（实测）：ONNX 档每实例化一次就多一份 onnxruntime `InferenceSession`（每份数十 MB
常驻内存）。"一题一账号"的基准（LongMemEval-oracle 抽 200 题＝200 个 MemorySession）
在**未 memo** 时会在第 N 个账号加载模型处被 onnxruntime 的 Rust 侧
`memory allocation of 2097152 bytes failed` 直接炸掉进程（异常 catch 不到）。

本文件钉住三件事：
① 同模型名重复解析 → 同一个对象（不再新建 session）；
② 不同模型名 → 不同对象（memo 按名键控，不串味）；
③ `invalidate_embedding_cache()` 清空 memo（切配置后必须拿到新实例）。
"""

from __future__ import annotations

from hippocampus.memory import retrieval as rt


def test_same_model_returns_same_instance():
    a = rt._resolve_embedding_function("builtin-hash")  # noqa: SLF001
    b = rt._resolve_embedding_function("builtin-hash")  # noqa: SLF001
    assert a is b


def test_memo_is_keyed_by_model_name():
    default_fn = rt._resolve_embedding_function("builtin-hash")  # noqa: SLF001
    other_fn = rt._resolve_embedding_function("onnx:not-a-real-org/not-a-real-model")  # noqa: SLF001
    assert default_fn is not other_fn
    # 二次解析仍各自回到自己那份
    assert rt._resolve_embedding_function("builtin-hash") is default_fn  # noqa: SLF001
    assert rt._resolve_embedding_function("onnx:not-a-real-org/not-a-real-model") is other_fn  # noqa: SLF001


def test_invalidate_clears_memo():
    before = rt._resolve_embedding_function("builtin-hash")  # noqa: SLF001
    rt.invalidate_embedding_cache()
    after = rt._resolve_embedding_function("builtin-hash")  # noqa: SLF001
    assert before is not after


def test_two_sessions_share_one_embedding_function(tmp_path):
    """两个账号（两个数据根）用同一个模型名 → 共用同一个 EF 实例。

    这就是 LME 一题一账号能跑完的原因：200 个会话只加载 **一份** ONNX 会话。
    """
    from hippocampus.memory.memory_bridge import MemorySession

    s1 = MemorySession("acct-1", data_dir=tmp_path / "a1")
    s2 = MemorySession("acct-2", data_dir=tmp_path / "a2")
    try:
        assert s1._embed_fn is not None  # noqa: SLF001
        assert s1._embed_fn is s2._embed_fn  # noqa: SLF001
    finally:
        for s in (s1, s2):
            close = getattr(s, "close", None)
            if callable(close):
                close()
