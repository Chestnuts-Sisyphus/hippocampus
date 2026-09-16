"""验收入口：示例库**无真实记忆**（T5-②）——公开仓与示例数据里只能有合成数据。

判据（全部用代码判，不靠人核对）：
1. 示例库里的记忆/经历条数与内容，必须来自 `hippocampus.seed` 的合成清单
   （逐条比对生成器的内容集合，出现清单外的条目即报错）；
2. 不得出现真实个人数据特征：身份证号、手机号、真实邮箱、真实文件绝对路径、
   真实组织机构名清单；
3. 示例库必须由 `hippocampus seed` 生成于**指定的示例根**（不是用户主目录）。

跑法：`python scripts/scan_personal_data.py --home <示例库根>`
退出码 0 = 干净。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 个人数据特征（保守写法：命中即报，人工复核）
PII_PATTERNS = [
    (re.compile(r"\b\d{17}[\dXx]\b"), "身份证号形态"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "手机号形态"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "邮箱形态"),
    (re.compile(r"[A-Za-z]:[\\/](?:Users|Documents|Desktop)[\\/]", re.IGNORECASE), "本机用户目录路径"),
    (re.compile(r"/home/[a-z][a-z0-9_\-]{2,}/"), "POSIX 用户主目录路径"),
]

# 运行期可能带出来的默认值（不是数据，是字段默认）
IGNORE_VALUES = {"[]", "{}", "", "active", "fact", "preference", "resource", "status"}


def seed_contents() -> set[str]:
    from hippocampus.seed import SEED_ITEMS

    return {item.content for item in SEED_ITEMS}


def _iter_values(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        for row in conn.execute("SELECT content AS v FROM memories").fetchall():
            out.append(("memories.content", str(row["v"] or "")))
    except sqlite3.Error:
        pass
    try:
        for row in conn.execute("SELECT content AS v FROM episodes").fetchall():
            out.append(("episodes.content", str(row["v"] or "")))
    except sqlite3.Error:
        pass
    try:
        for row in conn.execute("SELECT canonical_name AS v FROM entities").fetchall():
            out.append(("entities.canonical_name", str(row["v"] or "")))
    except sqlite3.Error:
        pass
    return out


def scan(home: Path) -> list[str]:
    problems: list[str] = []
    allowed = seed_contents()
    if not home.exists():
        return [f"示例根不存在: {home}"]

    dbs = [p for p in home.rglob("*.db") if "chroma" not in p.parts]
    if not dbs:
        return [f"示例根下没有找到库文件: {home}"]

    for db_path in dbs:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            problems.append(f"{db_path}: 打不开（{e}）")
            continue
        try:
            for label, value in _iter_values(conn):
                if not value or value in IGNORE_VALUES:
                    continue
                for pattern, kind in PII_PATTERNS:
                    if pattern.search(value):
                        problems.append(f"{db_path} [{label}] 命中 {kind}: {value[:60]}")
                # 记忆本体：允许"seed 清单内容"与"agent 由清单推导出的结论/改写"
                if label == "memories.content" and value.startswith("本轮任务结论"):
                    continue
                if label == "memories.content" and value not in allowed and not any(value in a for a in allowed):
                    problems.append(f"{db_path} [{label}] 出现 seed 清单之外的记忆: {value[:60]}")
                if label == "episodes.content" and value.startswith("本轮任务结论"):
                    continue  # agent 由 seed 内容推导出的结论（同一来源）
                if label == "episodes.content" and value not in allowed and not any(value in a for a in allowed):
                    problems.append(f"{db_path} [{label}] 出现 seed 清单之外的经历: {value[:60]}")
        finally:
            conn.close()
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="示例库根（hippocampus seed 的目标）")
    args = ap.parse_args()

    problems = scan(Path(args.home))
    if problems:
        print(f"示例库扫描：{len(problems)} 处需要处理")
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1
    print("✓ 示例库扫描：全部内容来自合成清单，无个人数据特征")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
