"""校准表三态护栏测试（X7）：验证 force=False/True/None 下的"换档不重标定"行为。

以及参数级标定清单（X10）：UNCALIBRATED_TIERS 填充具体档位，添加参数级告警。

我替你定的：
- 测试路径按现有模式放在 tests/，文件名 test_n48 延续编号序列
- 使用内存 SQLite 数据库，避免文件系统依赖
- 覆盖三种场景：首次应用（force=False）、同档重复（force=False）、换档（force=True）
- X10 新增对 PARAMETER_TIER_MAP 和参数级告警的验证
"""

import json
import sqlite3

import pytest

from hippocampus.memory import calibration, database


@pytest.fixture
def conn():
    """内存数据库 fixture，初始化 param_snapshots。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 手动执行建表和初始化（与真实启动流程一致）
    conn.executescript(database.SCHEMA)
    database.seed_initial_snapshot(conn)
    yield conn
    conn.close()


def test_apply_tier_params_force_false_first_time(conn):
    """force=False 首次应用 → 应该覆盖写入档位参数。

    逻辑：首次进入时 prev_tier=None，公式 `overwrite = force or prev_tier is None or prev_tier != model`
    得出 overwrite=True，因此会写入参数。
    """
    params = calibration.apply_tier_params(conn, "onnx:Xenova/bge-small-zh-v1.5", force=False)

    assert params["embedding_tier"] == "onnx:Xenova/bge-small-zh-v1.5"
    assert params["answer_floor"] == 0.465
    assert params["absolute_floor"] == 0.50
    # 验证数据库已持久化
    row = conn.execute("SELECT params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    loaded = json.loads(row["params"])
    assert loaded["embedding_tier"] == "onnx:Xenova/bge-small-zh-v1.5"
    assert loaded["answer_floor"] == 0.465


def test_apply_tier_params_force_false_same_tier_no_overwrite(conn):
    """force=False 同档重复进入 → 人工调过的键应保留，只补缺。

    场景：先应用一次，然后手动修改 answer_floor，再调用 force=False，
    人工修改的值不应被覆盖。
    """
    # 首次应用
    calibration.apply_tier_params(conn, "onnx:Xenova/bge-small-zh-v1.5", force=False)
    # 模拟人工修改
    row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    params = json.loads(row["params"])
    params["answer_floor"] = 0.50  # 人工上调
    conn.execute(
        "UPDATE param_snapshots SET params=? WHERE id=?",
        (json.dumps(params, ensure_ascii=False), row["id"]),
    )
    conn.commit()
    # 再次调用 force=False
    params2 = calibration.apply_tier_params(conn, "onnx:Xenova/bge-small-zh-v1.5", force=False)

    # 人工修改的 answer_floor 应保留
    assert params2["answer_floor"] == 0.50
    # 其他未改的参数仍来自档位表
    assert params2["absolute_floor"] == 0.50
    assert params2["embedding_tier"] == "onnx:Xenova/bge-small-zh-v1.5"


def test_apply_tier_params_force_true_switch_tier(conn):
    """force=True 换档 → 必须覆盖重写，换嵌入档必须重新标定。

    这是核心护栏：防止拿旧量纲的阈值去比新量纲的相似度。
    """
    # 先用中文档
    calibration.apply_tier_params(conn, "onnx:Xenova/bge-small-zh-v1.5", force=False)
    row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    params_zh = json.loads(row["params"])
    assert params_zh["answer_floor"] == 0.465

    # force=True 切换到英文档
    params_en = calibration.apply_tier_params(conn, "onnx:Xenova/bge-small-en-v1.5", force=True)

    # 必须完全覆盖
    assert params_en["embedding_tier"] == "onnx:Xenova/bge-small-en-v1.5"
    assert params_en["answer_floor"] == 0.649  # 英文档值
    assert params_en["absolute_floor"] == 0.50
    # 验证中文档参数被清除
    row = conn.execute("SELECT params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    loaded = json.loads(row["params"])
    assert loaded["answer_floor"] == 0.649


def test_apply_tier_params_force_none_switch_tier(conn):
    """force=None（默认）换档 → 也应覆盖重写，与 force=True 行为一致。

    公式中 `prev_tier != model` 为真时 overwrite=True，因此无需显式 force=True。
    """
    # 先用内置档
    calibration.apply_tier_params(conn, "builtin-hash", force=False)
    row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    params_builtin = json.loads(row["params"])
    assert params_builtin["answer_floor"] == 0.17

    # force=None（不传或传 None）切换到神经档
    params_neural = calibration.apply_tier_params(conn, "onnx:Xenova/bge-small-zh-v1.5", force=None)

    # 必须覆盖
    assert params_neural["embedding_tier"] == "onnx:Xenova/bge-small-zh-v1.5"
    assert params_neural["answer_floor"] == 0.465
    # 验证内置档参数被清除
    row = conn.execute("SELECT params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    loaded = json.loads(row["params"])
    assert loaded["answer_floor"] == 0.465


def test_apply_tier_params_unknown_tier_fallback(conn, capsys):
    """未知档 → 回退到内置档参数，并打印 stderr 告警。

    验证 UNCALIBRATED_TIERS 机制：未知档不是静默失败。
    """
    params = calibration.apply_tier_params(conn, "unknown:custom-model", force=False)

    # 回退到内置档
    assert params["embedding_tier"] == "unknown:custom-model"
    assert params["answer_floor"] == 0.17  # builtin-hash 的值
    # 检查 stderr 告警
    captured = capsys.readouterr()
    assert "[warning]" in captured.err
    assert "unknown:custom-model" in captured.err
    assert "无标定值" in captured.err
    # X10 参数级告警：应列出缺失的具体参数名
    assert "缺失参数" in captured.err
    assert "answer_floor" in captured.err


def test_parameter_tier_map_completeness(conn):
    """X10：验证 PARAMETER_TIER_MAP 与 TIER_PARAMS 的键一致性。

    每个已知档在两个表中都有对应的完整参数列表。
    """
    for tier_name in calibration.TIER_PARAMS:
        assert tier_name in calibration.PARAMETER_TIER_MAP, f"{tier_name} 应在 PARAMETER_TIER_MAP 中"
        # 验证参数数量一致
        tier_params = calibration.TIER_PARAMS[tier_name]
        param_list = calibration.PARAMETER_TIER_MAP[tier_name]
        expected_keys = set(tier_params.keys())
        actual_keys = {k for k, _ in param_list}
        assert expected_keys == actual_keys, f"{tier_name} 的参数键不一致"


def test_params_for_tier_returns_copy(conn):
    """验证 params_for_tier 返回的是副本，避免外部修改污染原始数据。"""
    params1 = calibration.params_for_tier("builtin-hash")
    params1["answer_floor"] = 999.0  # 尝试修改

    params2 = calibration.params_for_tier("builtin-hash")
    assert params2["answer_floor"] == 0.17  # 原始值不应被污染
