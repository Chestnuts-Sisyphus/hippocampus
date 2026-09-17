"""命令行入口（`hippocampus …`）。

命令面（与方案 §1.5 对齐）：
    doctor    体检：数据根／端口／锁／索引／嵌入档／模型端点（不打印凭据）
    seed      灌入示例数据（合成，含已知真值）
    demo      一键跑评测题并打印结果表
    proxy     代理形态：OpenAI 兼容端点
    chat      Agent 形态：跑一个任务（--offline 时只做记忆管理与检索问答）
    replay    用记录重跑一个轨迹（不是播放录像）
    explain   解释某一步注入了什么、为什么没注入别的（含 top-50 审计候选）
    memory    list／pending／candidates／review（--pending/--candidates/--suspicious）／off
    learning  off／on（学习开关）

设计纪律：不弹窗、不抢焦点；所有输出走 stdout/stderr，长任务写文件而不是开窗口。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hippocampus import __version__
from hippocampus.console import force_utf8
from hippocampus.core import MemoryCore, Scope
from hippocampus.memory import account as account_mod
from hippocampus.memory import config as mem_config


def _scope(args: argparse.Namespace) -> Scope:
    return Scope(
        account=getattr(args, "account", None) or "default",
        session=getattr(args, "session", None) or "cli",
        source="user",
    )


def _core(args: argparse.Namespace) -> MemoryCore:
    home = getattr(args, "home", None)
    return MemoryCore(home=home)


# ----------------------------------------------------------------------
# doctor
# ----------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    core = _core(args)
    scope = _scope(args)
    print("Hippocampus", __version__)
    print("（本命令不读取、不打印任何凭据；只报告『有没有』，不报告『是什么』）")
    print()

    # 数据根
    root = core.home
    print(f"  数据根        {root}  {'（已存在）' if root.exists() else '（首次运行会创建）'}")
    data_dir = core._data_dir(scope.account)  # noqa: SLF001
    print(f"  记忆库        {data_dir / 'memory.db'}")

    # 端口
    proxy_cfg = mem_config.get_proxy_config()
    port = proxy_cfg["port"]
    print(f"  端口          {port}  {'空闲' if _port_free(proxy_cfg['host'], port) else '被占用'}（默认 8765，可配）")

    # 锁与索引
    status = core.lock_status(scope)
    if status["held_by_other"]:
        lock_state = "被其他进程持有"
    elif status["stale"]:
        lock_state = "僵尸锁（可 `doctor --unlock` 回收）"
    else:
        lock_state = "空闲"
    print(f"  写锁          {lock_state}  {status['path']}")

    stats = core.stats(scope)
    print(f"  索引          memories {stats['memories']} / entities {stats['entities']} / "
          f"episodes {stats['episodes']} / relations {stats['relations']}")
    print(f"  待确认        pending {stats['pending']} / candidates {stats['candidates']}")

    # 索引健康（B5）：chroma 可写性 + 集合条数 vs 库内 active 条数 + 同步错误
    health = core.index_health(scope)
    if health["collection_count"] is None:
        print("  索引健康      词法档（未启用向量集合，BM25 索引随写重建；无 chroma 一致性风险）")
    else:
        state = "健康" if health["healthy"] else "异常"
        parts = [
            f"状态 {state}",
            f"chroma 可写 {'是' if health['chroma_writable'] else '否（只读/磁盘满）'}",
            f"集合 {health['collection_count']} vs 库内 active {health['active_memories']}",
            f"索引积压队列 {health['embeddings_queue']}",
        ]
        if health["last_error"]:
            parts.append(f"上次同步错误 {health['last_error'][:80]}")
        print("  索引健康      " + "  /  ".join(parts))

    tier = core.embedding_tier()
    download = "需联网下载一次" if tier["needs_download"] else "零下载"
    print(f"  嵌入档        {tier['model']}（{tier['tier']}，{download}）")

    llm = mem_config.get_llm_config()
    if mem_config.is_offline():
        print("  模型端点      离线档（--offline / HIPPOCAMPUS_OFFLINE=1）：记忆纪律照常，多步推理不可用")
    elif llm["base_url"] and llm["api_key"]:
        print(f"  模型端点      {llm['base_url']}  模型 {llm['model']}  凭据 已提供（来自环境变量/密钥服务）")
    else:
        missing = []
        if not llm["base_url"]:
            missing.append("端点")
        if not llm["api_key"]:
            missing.append("凭据")
        print(f"  模型端点      未配置（缺 {'、'.join(missing)}）→ 可用 --offline 运行记忆纪律")

    # 实例令牌（A2）：只显前 8 位
    from hippocampus.settings import instance_token

    token = instance_token()
    if token:
        print(f"  实例令牌      已启用（前 8 位 {token[:8]}…，完整令牌见 <数据根>/instance_token；"
              "代理请求带 `Authorization: Bearer <令牌>`）")
    else:
        print("  实例令牌      未启用（`hippocampus proxy` 首次启动会自动生成）")

    if getattr(args, "unlock", False):
        done = core.unlock(scope)
        print()
        print("  僵尸锁回收    " + ("已完成" if done else "无需回收（锁未过期或正被持有）"))
    core.close()
    return 0


def _port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


# ----------------------------------------------------------------------
# seed / demo
# ----------------------------------------------------------------------


def cmd_seed(args: argparse.Namespace) -> int:
    from hippocampus.seed import seed

    core = _core(args)
    scope = _scope(args)
    print(f"灌入示例数据 → scope={scope.account}")
    result = seed(core, scope, verbose=True)
    print()
    print(f"完成：新增 {result['count']} 条；库内 {result['stats']}")
    print("示例数据是**合成的**，含已知真值（事实／冲突对／过期项／模型轨样本）。")
    core.close()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from hippocampus.eval.runner import run_bundle

    core = _core(args)
    scope = _scope(args)
    report = run_bundle(
        core,
        scope,
        questions=args.questions,
        memories=args.memories,
        offline=True,
        pass_k=args.pass_k,
        model_arm=args.model,
        baseline=args.baseline,
    )
    print(report.render())
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 结果已写入 {args.json}")
    core.close()
    return 0


# ----------------------------------------------------------------------
# proxy / chat / replay / explain
# ----------------------------------------------------------------------


def cmd_proxy(args: argparse.Namespace) -> int:
    from hippocampus.proxy.app import serve

    cfg = mem_config.get_proxy_config()
    port = args.port or cfg["port"]
    return serve(host=args.host or cfg["host"], port=port, home=args.home, confirm_block=not args.no_confirm_block)


def cmd_chat(args: argparse.Namespace) -> int:
    from hippocampus.agent.runner import run_task

    core = _core(args)
    scope = _scope(args)
    result = run_task(core, scope, args.task, offline=args.offline, max_steps=args.max_steps)
    print(result.render())
    if args.trace:
        Path(args.trace).write_text(json.dumps(result.trace, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n轨迹已写入 {args.trace}")
    core.close()
    return 0 if result.exit == "completed" else 1


def cmd_replay(args: argparse.Namespace) -> int:
    from hippocampus.agent.runner import replay

    path = Path(args.trace)
    data = json.loads(path.read_text(encoding="utf-8"))
    result = replay(data, times=args.times)
    print(result.render())
    return 0 if result.consistent else 1


def cmd_explain(args: argparse.Namespace) -> int:
    from hippocampus.explain import default_audit_path, explain_run, explain_step
    from hippocampus.memory import audit as audit_mod

    path = Path(args.run)
    data = json.loads(path.read_text(encoding="utf-8"))
    audit_path = Path(args.audit) if getattr(args, "audit", None) else default_audit_path(data)
    audit_events = audit_mod.load_events(audit_path)
    observe_events = audit_mod.load_events(
        Path(data.get("home") or ".") / "accounts" / ((data.get("scope") or {}).get("account") or "default")
        / "observe.jsonl"
    )
    if args.step:
        print(explain_step(data, args.step, audit_events))
    else:
        print(explain_run(data, audit_events, observe_events))
    return 0


# ----------------------------------------------------------------------
# memory / learning
# ----------------------------------------------------------------------


def cmd_memory(args: argparse.Namespace) -> int:
    core = _core(args)
    scope = _scope(args)
    action = args.action

    if action == "list":
        items = core.list_memories(
            scope,
            limit=args.limit,
            kind=args.kind,
            status=None if args.all_status else (args.status or "active"),
            include_shadow=args.include_shadow,
        )
        if not items:
            print("（无记忆）")
        for item in items:
            tag = f"[{item.kind}]"
            flags = []
            if item.shadow:
                flags.append("观察轨")
            if item.status != "active":
                flags.append(item.status)
            if item.lifecycle != "active":
                flags.append(item.lifecycle)
            suffix = ("  " + " ".join(flags)) if flags else ""
            print(f"  {tag} {item.content}{suffix}")
            print(f"        来源：{item.source_quote or '（无）'}    id={item.id}")
        return 0

    if action == "pending":
        rows = core.pending(scope)
        if not rows:
            print("（无未决确认）")
        for row in rows:
            whose = "新记录" if row["is_new"] else "旧记录"
            print(f"  [{row['num']}]（{row['kind']}）{row['content']}  -- {whose}   id={row['id']}")
        return 0

    if action == "candidates":
        items = core.list_memories(scope, limit=args.limit, status="candidate", include_shadow=True)
        if not items:
            print("（无可疑候选）")
        for item in items:
            print(f"  [{item.kind}] {item.content}   id={item.id}  （等确认，未参与注入/去重）")
        return 0

    if action == "review":
        if args.suspicious:
            rows = core.suspicious(scope)
            if not rows:
                print("（无可疑项）")
            for row in rows:
                flag = " / ".join(row["flags"]) if row["flags"] else "-"
                print(f"  [{row['kind']}] {row['content']}   id={row['id']}  （{flag}）")
            return 0
        if args.pending:
            blocks = core.pending_blocks(scope)
            if not blocks:
                print("（无未决确认块）")
            for blk in blocks:
                days_left = blk["ttl_remaining_ms"] / 86400000.0
                print(f"  块 {blk['block_id']}  创建于 {blk['created_at']}  TTL 剩余 {days_left:.1f} 天")
                for e in blk["entries"]:
                    whose = "新记录" if e["is_new"] else "旧记录"
                    print(f"    [{e['num']}]（{e['kind']}）{e['content']}  -- {whose}   id={e['id']}")
            return 0
        # 默认视图 = candidates（可疑候选）
        items = core.list_memories(scope, limit=args.limit, status="candidate", include_shadow=True)
        if not items:
            print("（无可疑候选）")
        for item in items:
            print(f"  [{item.kind}] {item.content}   id={item.id}  （等确认，未参与注入/去重）")
        return 0

    if action in ("delete", "forget"):
        ok = core.delete_memory(scope, args.id, reason=args.reason or "用户删除")
        print("已归档（软删，可追溯）" if ok else "未找到该记忆")
        return 0 if ok else 1

    if action == "update":
        result = core.update_memory(scope, args.id, content=args.content, reason=args.reason or "用户修改")
        if result.ids:
            print(f"已写入新条 {result.ids[0]}，旧条 {args.id} 标记 superseded（保留可追溯）")
            return 0
        print("修改未生效：", [d.reason for d in result.skipped])
        return 1

    if action == "off":
        print(core.set_switch(scope, "关闭记忆"))
        return 0

    if action == "on":
        print(core.set_switch(scope, "打开记忆"))
        return 0

    if action == "scopes":
        for name in account_mod.list_accounts():
            print(f"  {name}")
        return 0

    print(f"未知子命令: {action}", file=sys.stderr)
    return 2


def cmd_learning(args: argparse.Namespace) -> int:
    core = _core(args)
    scope = _scope(args)
    if args.action == "off":
        print(core.set_switch(scope, "停止学习"))
    else:
        print(core.set_switch(scope, "继续学习"))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from hippocampus.core.transfer import export_package

    core = _core(args)
    scope = _scope(args)
    manifest = export_package(core, scope, args.target)
    print(f"已导出 → {Path(args.target).resolve()}")
    print(f"  schema 版本 {manifest['schema_version']}／应用版本 {manifest['app_version']}／"
          f"scope {manifest['account']}")
    print(f"  库内计数 {manifest['counts']}")
    print("  （导出的是源真相 memory.db；向量索引在导入端重建）")
    core.close()
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from hippocampus.core.transfer import import_package

    core = _core(args)
    scope = _scope(args)
    try:
        result = import_package(core, scope, args.source, force=args.force)
    except (ValueError, FileExistsError) as e:
        print(f"导入失败：{e}", file=sys.stderr)
        core.close()
        return 1
    print(f"已导入 ← {Path(args.source).resolve()}")
    print(f"  包内账号 {result['manifest'].get('account')} → 当前 scope {scope.account}")
    print(f"  导入后计数 {result['stats']}")
    core.close()
    return 0


# ----------------------------------------------------------------------
# argparse 装配
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hippocampus", description="以记忆为核心的 agent 运行时")
    parser.add_argument("--version", action="version", version=f"hippocampus {__version__}")
    parser.add_argument("--home", help="数据根（默认 $HIPPOCAMPUS_HOME 或 ~/.hippocampus）")
    parser.add_argument("--account", help="作用域：account（默认 default）")
    parser.add_argument("--session", help="作用域：session（默认 cli）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="体检：数据根／端口／锁／索引／嵌入档／模型端点")
    p.add_argument("--unlock", action="store_true", help="回收僵尸写锁")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("seed", help="灌入示例数据（合成，含已知真值）")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("demo", help="一键跑评测题并打印结果表")
    p.add_argument("--questions", type=int, default=10, help="题目数（完整集 20）")
    p.add_argument("--memories", action="store_true", help="跑记忆开/关对照")
    p.add_argument("--pass-k", type=int, default=1, help="每题重复 k 次全过才算过（N8 pass^k）")
    p.add_argument("--model", action="store_true", help="加模型臂（需配置模型端点；离线档自动跳过）")
    p.add_argument("--baseline", action="store_true", help="加关键词基线对照臂（仅 BM25 直查库）")
    p.add_argument("--json", help="把结果写到该路径（JSON）")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("proxy", help="代理形态：OpenAI 兼容端点")
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    p.add_argument("--no-confirm-block", action="store_true", help="关闭确认块追加")
    p.set_defaults(func=cmd_proxy)

    p = sub.add_parser("chat", help="Agent 形态：跑一个任务")
    p.add_argument("task", nargs="?", default="", help="任务文本（留空则交互式输入）")
    p.add_argument("--offline", action="store_true", help="离线档：只做记忆管理与检索问答")
    p.add_argument("--max-steps", type=int, default=8)
    p.add_argument("--trace", help="把轨迹写到该路径")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("replay", help="用记录重跑轨迹（确定性）")
    p.add_argument("trace")
    p.add_argument("--times", type=int, default=2)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("explain", help="解释某一步的注入与剔除（含 top-50 审计候选）")
    p.add_argument("--run", required=True, help="轨迹 JSON 路径")
    p.add_argument("--step", type=int, default=0, help="解释第几步（缺省解释全部并合并观察视图）")
    p.add_argument("--audit", help="审计 JSONL 路径（缺省用轨迹 home 下的 audit.jsonl）")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("memory", help="记忆管理")
    p.add_argument(
        "action",
        choices=[
            "list",
            "pending",
            "candidates",
            "review",
            "delete",
            "forget",
            "update",
            "off",
            "on",
            "scopes",
        ],
    )
    p.add_argument("id", nargs="?", help="记忆 id（delete/forget/update 用）")
    p.add_argument("--content", help="新内容（update 用）")
    p.add_argument("--reason", help="修改/删除理由（写进来源，可追溯）")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--kind", choices=["preference", "fact", "resource", "status"])
    p.add_argument("--status")
    p.add_argument("--all-status", action="store_true", help="不过滤状态（含 superseded/archived）")
    p.add_argument("--include-shadow", action="store_true", help="含模型观察轨")
    p.add_argument("--pending", action="store_true", help="review：未决确认块（含 TTL 剩余）")
    p.add_argument("--suspicious", action="store_true", help="review：可疑项（安全标记/未决/TTL 冲突）")
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("learning", help="学习开关")
    p.add_argument("action", choices=["off", "on"])
    p.set_defaults(func=cmd_learning)

    p = sub.add_parser("export", help="导出记忆库为目录包（manifest + memory.db）")
    p.add_argument("target", help="导出目标目录")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="从目录包导入记忆库")
    p.add_argument("source", help="导出包目录")
    p.add_argument("--force", action="store_true", help="覆盖已存在的库（旧库留 .bak 副本）")
    p.set_defaults(func=cmd_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8()  # Windows 控制台默认非 UTF-8：不兜底就会因为打印中文/符号崩掉
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
