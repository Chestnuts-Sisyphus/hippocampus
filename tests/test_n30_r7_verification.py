"""七轮 T2 验收锚点 A42–A45：可求证机制（`memory/verification.py` 正本 §三-4 施工）。

四条锚点对应设计稿 §五-6，逐条钉住"三态处置"，缺一条都算机制没落地：

- **A42** 路径不存在的模型资源 → 丢弃（不注入、不入正式记忆，观察日志有痕）；
- **A43** 路径不存在的用户资源 → 挂起一次询问，**不静默丢**；裁决后按用户选择落库；
- **A44** 不可求证内容（"我是班长"）→ **直接存储**，且**不带**"可疑"标记；
- **A45** L3 关闭时**零出站请求**。

外加：verified 侧的字段落库（四列）、非法 URL／不存在日历日的否证、观察轨判据已收敛到统一入口。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.memory import verification as vmod


def _rows(core: MemoryCore, scope: Scope) -> dict[str, dict[str, object]]:
    items = core.list_memories(scope, limit=100, status=None, include_shadow=True)
    return {i.content: {"status": i.status, "id": i.id} for i in items}


def _verification_row(core: MemoryCore, scope: Scope, content: str) -> dict:
    session = core._session(scope)  # noqa: SLF001
    row = session.conn.execute("SELECT * FROM memories WHERE content=?", (content,)).fetchone()
    return dict(row) if row else {}


def _observe_events(home: Path, event: str) -> list[dict]:
    events = []
    for path in Path(home).rglob("observe.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if entry.get("event") == event:
                    events.append(entry)
    return events


# ---------------- A42：模型幻觉资源 ----------------


def test_a42_model_hallucinated_resource_dropped_with_trace(core, scope, home):
    """模型轨声明一个本机不存在的路径 → 不进正式记忆，但 `observe.jsonl` 必须有痕。"""
    content = "导出脚本在 D:/nonexistent_dir_xyz/hallucinated_tool.py 里"
    result = core.write(scope, content, kind="resource", source="model")

    assert not result.ids, "幻觉资源进了正式/观察记忆"
    assert any(d.stage == "verification" for d in result.skipped), "丢弃原因未记进 skipped"
    assert content not in _rows(core, scope)

    events = _observe_events(home, "verification")
    assert any(e["dropped"] and e["status"] == "refuted" for e in events), "观察日志没有否证留痕"


# ---------------- A43：用户来源挂起询问 ----------------


def test_a43_user_refuted_resource_holds_then_confirms(core, scope):
    """用户说的路径本机找不到 → 挂起一次询问（不丢），确认后转正。"""
    content = "我的配置文件在 D:/nonexistent_dir_xyz/my.conf"
    result = core.write(scope, content, kind="resource")

    assert result.ids, "用户来源被静默丢弃（A43 回归）"
    assert result.pending, "应挂起一次询问"
    assert _rows(core, scope)[content]["status"] == "candidate"

    pending = core.pending(scope)
    assert pending, "挂起后没有可裁决的确认块"
    candidate = next((p for p in pending if p["is_new"]), None)
    assert candidate is not None
    outcome = core.confirm(scope, f"确认{candidate['num']}")
    assert outcome is not None and outcome.decision == "confirm"
    assert _rows(core, scope)[content]["status"] == "active", "确认后没转正"


def test_a43_veto_keeps_it_out_of_active(core, scope):
    """否决这条询问 → 不进 active（用户自己说"算了我记错了"），但记录仍在库里可追溯。"""
    content = "我的密钥文件在 D:/nonexistent_dir_xyz/secret.conf"
    core.write(scope, content, kind="resource")
    assert core.pending(scope), "应先挂起一条询问"
    outcome = core.confirm(scope, "否决")
    assert outcome is not None and outcome.decision == "veto"
    assert _rows(core, scope)[content]["status"] == "candidate"


# ---------------- A44：不可求证直存 ----------------


def test_a44_unverifiable_identity_stored_without_suspicion(core, scope):
    """"我是班长"：本机没有权威数据源 → 直接存储，且**不打可疑标记**。"""
    content = "我是班长，也在天大软件学院念书"
    result = core.write(scope, content, kind="fact")

    assert result.ids and result.pending == []
    row = _verification_row(core, scope, content)
    assert row["status"] == "active"
    assert row["verification_status"] == "unverifiable"
    assert int(row["security_flag"]) == 0, "不可求证被误当成可疑"


def test_preference_never_verified(core, scope):
    """偏好（价值判断）不归求证机制管：即便内容里带路径也不否证，交给确认轨。"""
    content = "我更喜欢把笔记放在 D:/notes 目录"
    result = core.write(scope, content, kind="preference")
    assert result.ids
    assert vmod.classify(content, "preference")["verifiable"] is False


# ---------------- A45：L3 关闭时零出站 ----------------


def test_a45_no_outbound_when_external_off(core, scope, monkeypatch):
    """默认（L3 关）：内容里有合法 URL，也不许发出任何探测请求。"""
    calls: list[str] = []
    monkeypatch.setattr(vmod, "_probe_url", lambda url, timeout_s=5.0: calls.append(url) or 200)

    content = "文档在 https://93.184.216.34/guide 上"
    result = core.write(scope, content, kind="resource")
    assert result.ids
    assert calls == [], "L3 未开启却出了站"


def test_l3_used_only_when_external_on(monkeypatch):
    """显式开 L3：才探测，且失败只记"未取证"，绝不记成"假"。

    URL 用公网 IP 形式——`validate_outbound_url` 对域名要过 DNS，测试不该依赖解析。"""
    monkeypatch.setattr(vmod, "_probe_url", lambda url, timeout_s=5.0: 404)
    vmod.clear_external_cache()
    res = vmod.verify("见 https://93.184.216.34/x", "resource", external=True)
    assert res.status == vmod.REFUTED and "4xx/5xx" in res.evidence

    monkeypatch.setattr(vmod, "_probe_url", lambda url, timeout_s=5.0: None)
    vmod.clear_external_cache()
    res2 = vmod.verify("见 https://93.184.216.34/y", "resource", external=True)
    assert res2.status == vmod.VERIFIED and "未取证" in res2.evidence
    vmod.clear_external_cache()


def test_l3_rejects_loopback_and_stays_offline(monkeypatch):
    """L3 的出站闸与 A41 同口径：环回/私有地址不开出站。"""
    calls: list[str] = []
    monkeypatch.setattr(vmod, "_probe_url", lambda url, timeout_s=5.0: calls.append(url) or 200)
    vmod.clear_external_cache()
    assert vmod.probe_external("http://127.0.0.1:8764/x") is None
    assert calls == []


# ---------------- verified 侧与判据 ----------------


def test_verified_resource_records_four_columns(core, scope, tmp_path):
    real = tmp_path / "truth.txt"
    real.write_text("存在", encoding="utf-8")
    content = f"报告在 {real.as_posix()}"
    result = core.write(scope, content, kind="resource")
    assert result.ids

    row = _verification_row(core, scope, content)
    assert row["verification_status"] == "verified"
    assert "L1:path_exists" in row["verification_method"]
    assert int(row["verified_at"]) > 0
    assert row["verification_evidence"]


@pytest.mark.parametrize(
    ("content", "kind", "method"),
    [
        ("数据在 D:/a/b/missing_file_xyz.py", "resource", "L1:path_exists"),
        ("环回服务在 http://127.0.0.1:9/x 里", "resource", "L1:url_valid"),
        ("计划定在 2026年2月30日", "fact", "L1:date_possible"),
        ("命中率是 180%", "fact", "L1:number_in_range"),
    ],
)
def test_refuting_judgements(content, kind, method):
    res = vmod.verify(content, kind)
    assert res.status == vmod.REFUTED, f"{content} 应被否证"
    assert method in res.method


def test_verification_can_be_switched_off(core, scope):
    """开关（活跃参数）：关掉求证后，幻觉资源照旧入正式记忆（不静默改变别的语义）。"""
    from hippocampus.memory import database as db
    from hippocampus.memory import retrieval as rt

    session = core._session(scope)  # noqa: SLF001
    db.set_active_params(session.conn, {"verification_enabled": False}, reason="测试：关闭求证")
    assert rt.get_active_params(session.conn)["verification_enabled"] is False

    content = "导出脚本在 D:/nonexistent_dir_xyz/again.py 里"
    result = core.write(scope, content, kind="resource")
    assert result.ids, "求证开关没生效"


def test_observe_track_shares_one_path_judge():
    """观察轨（前身 P2 幻觉校验）已收敛到统一入口，两处判据不得再各写一套。"""
    from hippocampus.memory import memory_bridge

    assert memory_bridge._resource_plausible("代码在 nonexistent_module_xyz.py 里")  # noqa: SLF001
    assert not memory_bridge._resource_plausible("文件在 D:/nonexistent_dir_xyz/h.py")  # noqa: SLF001
    assert vmod.path_claims_plausible("文件在 D:/nonexistent_dir_xyz/h.py") is False


def test_verification_fields_are_append_only_defaults(home):
    """v1"只追加"契约：四列都带默认值，老库补列后既有行读出默认，不破坏既有语义。"""
    from hippocampus.memory import database as db
    from hippocampus.memory import retrieval as rt

    core = MemoryCore(home=home)
    try:
        scope = Scope(account="legacy", session="s")
        conn = core._session(scope).conn  # noqa: SLF001
        conn.execute(
            "INSERT INTO memories (id, type, content, created_at, updated_at) VALUES ('m_old','fact','旧行',1,1)"
        )
        conn.commit()
        added = db.ensure_verification_schema(conn)
        assert not any(added.values()), "列应已存在（幂等）"
        row = conn.execute("SELECT verification_status FROM memories WHERE id='m_old'").fetchone()
        assert row["verification_status"] == "unverifiable"
        assert db.VERIFICATION_PARAM_DEFAULTS["verification_external"] is False
        params = rt.get_active_params(conn)
        assert params["verification_enabled"] is True and params["verification_external"] is False
    finally:
        core.close()


# ---------------- 八轮 V10：L3 探测的状态码语义（真机验证逮到的缺陷回归） ----------------

PROBE_URL = "https://example.com/some/page"


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _stub_urlopen(monkeypatch, results: list):
    """按调用顺序吐出响应状态码或异常；同时记录请求方法，便于验证"HEAD 被挡→退 GET"。"""
    calls: list[str] = []

    def fake(url, timeout=None):  # noqa: ANN001
        calls.append(getattr(url, "method", "?"))
        outcome = results[len(calls) - 1] if len(calls) <= len(results) else results[-1]
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(outcome)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return calls


def test_http_error_status_is_taken_as_evidence(monkeypatch):
    """4xx/5xx 是"取到证了"，必须带回状态码——不能和"没取到证"混成同一个 None。"""
    import urllib.error

    http_404 = urllib.error.HTTPError(PROBE_URL, 404, "Not Found", {}, None)  # type: ignore[arg-type]
    calls = _stub_urlopen(monkeypatch, [http_404])
    vmod.clear_external_cache()
    try:
        assert vmod.probe_external(PROBE_URL) == 404
        assert calls == ["HEAD"]
    finally:
        vmod.clear_external_cache()


def test_transport_error_stays_unprobed_not_refuted(monkeypatch):
    """DNS／连接失败这类传输层异常 → None＝未取证，**不得**因此判假。"""
    import urllib.error

    _stub_urlopen(monkeypatch, [urllib.error.URLError("name not resolved")])
    vmod.clear_external_cache()
    try:
        assert vmod.probe_external(PROBE_URL) is None
        result = vmod.verify(f"资料位置见 {PROBE_URL}", "resource", source="model", external=True)
        assert result.status != vmod.REFUTED, "未取证被判成假（违反硬边界）"
        assert "未取证" in result.evidence
    finally:
        vmod.clear_external_cache()


def test_head_blocked_url_is_retried_with_get(monkeypatch):
    """HEAD 被挡（405）不代表链接是死的：退回 GET 复核后按 GET 的结果定性，避免假判假。"""
    calls = _stub_urlopen(monkeypatch, [405, 200])
    vmod.clear_external_cache()
    try:
        assert vmod.probe_external(PROBE_URL) == 200
        assert calls == ["HEAD", "GET"]
    finally:
        vmod.clear_external_cache()


def test_dead_url_refutes_when_external_is_on(monkeypatch):
    """正反对照的另一半：真 404 必须判 refuted（只有"未取证"才宽容）。"""
    import urllib.error

    _stub_urlopen(monkeypatch, [urllib.error.HTTPError(PROBE_URL, 404, "Not Found", {}, None)])  # type: ignore[arg-type]
    vmod.clear_external_cache()
    try:
        result = vmod.verify(f"资料位置见 {PROBE_URL}", "resource", source="model", external=True)
        assert result.status == vmod.REFUTED
        assert "L3:external_probe" in result.method
    finally:
        vmod.clear_external_cache()
