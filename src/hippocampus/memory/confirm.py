# 由前身 hippocampus_prototype/confirm.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus B1 —— 确认机制（confirm.py）
对应设计文档 v2.1 第八节「变更确认四类型」，B1 简化实现：
- AI 回复尾附确认块：列出冲突记忆双方 + 编号（按时间排序，旧的编号小）
- 用户输入「确认X」→ 编号 X 的记忆胜出，对方记忆标 superseded，建 X SUPERSEDES 对方 关系
- 用户输入「否决」→ 新记忆标 superseded（旧记忆保持 active），输出固定文案
冲突检测复用 conflict.detect_conflicts（1 次轻量 LLM 调用，ADD-only 不写库）。
"""

import sqlite3

from hippocampus.memory import database as db


class ConfirmBlock:
    """一次确认块：涉及记忆列表 + 冲突对列表"""

    def __init__(self, entries, conflicts):
        # entries: [{"num": int, "mem": dict, "is_new": bool}, ...] 按 created_at 升序编号
        # conflicts: [(a_id, b_id, reason), ...]
        self.entries = entries
        self.conflicts = conflicts
        self.new_ids = {e["mem"]["id"] for e in entries if e["is_new"]}

    def render(self) -> str:
        lines = ["> 记忆·确认：我注意到你前后说的可能矛盾，需要确认哪个是对的："]
        for e in self.entries:
            tag = "更早的记录" if not e["is_new"] else "最近的记录"
            lines.append(f"> [{e['num']}]（{e['mem']['type']}）{e['mem']['content']} -- {tag}")
        nums = "".join(f"「确认{e['num']}」" for e in self.entries)
        lines.append(f"> 回复{nums}，或「否决」。")
        return "\n".join(lines)

    def entry_by_num(self, num: int):
        for e in self.entries:
            if e["num"] == num:
                return e
        return None


def build_confirm_block(
    conn: sqlite3.Connection, new_memory_ids: list[str], conflicts: list[tuple[str, str, str]]
) -> ConfirmBlock | None:
    """根据冲突对构建确认块。new_memory_ids：本轮新入库的记忆 id 列表。
    conflicts 为空或涉及记忆已不存在时返回 None（无确认块）。"""
    if not conflicts:
        return None
    involved = {}
    for a, b, _reason in conflicts:
        for mid in (a, b):
            if mid in involved:
                continue
            row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            if row:
                involved[mid] = dict(row)
    if not involved:
        return None
    new_set = set(new_memory_ids)
    entries = []
    for i, (mid, mem) in enumerate(sorted(involved.items(), key=lambda kv: kv[1]["created_at"]), start=1):
        entries.append({"num": i, "mem": mem, "is_new": mid in new_set})
    return ConfirmBlock(entries, conflicts)


def parse_confirmation(block: ConfirmBlock | None, user_input: str):
    """解析用户确认指令。
    返回 ('confirm', winner_id) / ('veto', None) / None（非确认指令，走正常对话）"""
    if block is None:
        return None
    text = (user_input or "").strip()
    if text.startswith("确认"):
        num_str = text[2:].strip()
        if num_str.isdigit():
            e = block.entry_by_num(int(num_str))
            if e is not None:
                return ("confirm", e["mem"]["id"])
        return None
    if text.startswith("否决"):
        return ("veto", None)
    return None


def apply_confirmation(conn: sqlite3.Connection, block: ConfirmBlock, decision) -> str:
    """执行确认落库，返回输出文案。
    decision: ('confirm', winner_id) 或 ('veto', None)"""
    if decision[0] == "confirm":
        winner_id = decision[1]
        loser_ids = [e["mem"]["id"] for e in block.entries if e["mem"]["id"] != winner_id]
        for lid in loser_ids:
            db.supersede_memory(conn, winner_id, lid)
        conn.commit()
        loser_kind = "旧记忆" if winner_id in block.new_ids else "新记忆"
        return f"已确认，{loser_kind}标记 superseded（记忆{winner_id} 生效）"
    # veto：新记忆全部标 superseded，旧记忆保持 active
    new_ids = [e["mem"]["id"] for e in block.entries if e["is_new"]]
    old_ids = [e["mem"]["id"] for e in block.entries if not e["is_new"]]
    for nid in new_ids:
        for oid in old_ids:
            db.supersede_memory(conn, oid, nid)
    conn.commit()
    return "已否决，新记忆标记 superseded（保留旧记录）"
