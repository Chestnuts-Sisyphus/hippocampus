"""八轮 V10：可求证 **L3 外站探测**的真机验证（一次性人工真机件，不进 CI）。

背景：七轮落 L3 时 CI 只钉得住"L3 关着时零出站"（A45），**打开以后**的取证三态、
超时与缓存命中从未真跑过。本脚本就是那次真跑：对**一个合法 https 站点**（pypi.org）
走四种输入，把三态与缓存各验一遍，并把结果如实记进 `docs/verification-design.md`。

四条断言（任一不满足退出非零）：
1. **verified**：可达链接 → 状态 `verified`，取证方法里出现 `L3:external_probe`，证据里写"可达"；
2. **refuted**：同站 404 链接 → 状态 `refuted`（真判假，不是网络抖动）；
3. **未取证不判假**：域名解析不了的合法 https 链接 → **不得**是 `refuted`
   （实现里网络异常返回 None，走"未取证"分支；这条正是硬边界"失败不得改成判假"）；
4. **缓存命中不重复出站**：同一 URL 连问两次，底层探测只发生一次。

外加一条默认口径：`external=False`（**默认**）时同一条 URL 一次出站都不发——
"开了才出站"必须是真的。

跑法（需要网络；零 API 花费，只发只读 HEAD）：
    python scripts/live_l3_probe.py [--json D:/tmp/out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

REACHABLE = "资料位置见 https://example.com/"
# 样本同站：example.com 对根路径回 200、对未知路径回 404，两个方向都稳定。
# （pypi.org 的 HEAD 从本机时通时不通——TLS 被干扰，拿它当"可达"样本会让验证变抖动）
DEAD = "资料位置见 https://example.com/hc-l3-probe-path-that-does-not-exist/"
UNRESOLVABLE = "资源存放位置见 https://hc-l3-probe-does-not-exist.invalid/"
NO_STRUCTURE = "我更喜欢下午面试"

# force-utf8 shim：Windows 控制台默认代码页（cp1252）打不出中文会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run_probe() -> dict:
    from hippocampus.memory import verification as vf

    probes: list[str] = []
    real_probe = vf._probe_url  # noqa: SLF001（本脚本就是来验这条私有实现的真实行为）

    def counting_probe(url: str, timeout_s: float = 5.0):
        probes.append(url)
        return real_probe(url, timeout_s)

    vf._probe_url = counting_probe  # noqa: SLF001
    try:
        # 0) 默认档：不开 external，一次都不该出站
        probes.clear()
        off = vf.verify(REACHABLE, "resource", source="user", external=False)
        off_egress = len(probes)

        # 1) verified
        probes.clear()
        ok = vf.verify(REACHABLE, "resource", source="user", external=True)
        verified_report = {"status": ok.status, "method": ok.method, "evidence": ok.evidence, "egress": len(probes)}

        # 2) refuted（真 404）
        probes.clear()
        dead = vf.verify(DEAD, "resource", source="user", external=True)
        refuted_report = {"status": dead.status, "method": dead.method, "evidence": dead.evidence, "egress": len(probes)}

        # 3) 未取证（DNS 失败）——不得判假
        probes.clear()
        unknown = vf.verify(UNRESOLVABLE, "resource", source="user", external=True)
        unknown_report = {"status": unknown.status, "method": unknown.method, "evidence": unknown.evidence, "egress": len(probes)}

        # 4) 缓存：先把该 URL 探到一次**真拿到状态码**（填热缓存），再连问两次——两次都不该再出站
        warm = None
        for _ in range(3):
            warm = vf.probe_external("https://example.com/")
            if warm is not None:
                break
        probes.clear()
        again = vf.verify(REACHABLE, "resource", source="user", external=True)
        third = vf.verify(REACHABLE, "resource", source="user", external=True)
        cache_report = {
            "warm": "ok" if warm is not None else "failed",
            "warm_code": warm,
            "statuses": [again.status, third.status],
            "egress": len(probes),
            "ttl_s": vf._L3_CACHE_TTL_S,  # noqa: SLF001
        }

        # 5) 无可校验结构 → unverifiable（直接存、不打可疑）
        probes.clear()
        plain = vf.verify(NO_STRUCTURE, "preference", source="user", external=True)
        no_struct_report = {"status": plain.status, "evidence": plain.evidence, "egress": len(probes)}
    finally:
        vf._probe_url = real_probe  # noqa: SLF001

    return {
        "checked_at": int(time.time()),
        "default_off": {"status": off.status, "egress": off_egress},
        "verified": verified_report,
        "refuted": refuted_report,
        "unverified_egress": unknown_report,
        "cache": cache_report,
        "no_structure": no_struct_report,
    }


def evaluate(report: dict) -> list[str]:
    bad: list[str] = []
    if report["default_off"]["egress"] != 0:
        bad.append(f"默认关时仍出站 {report['default_off']['egress']} 次")
    if report["verified"]["status"] != "verified" or "L3:external_probe" not in report["verified"]["method"]:
        bad.append("可达链接没有走到 L3 并判 verified")
    if "可达" not in report["verified"]["evidence"]:
        bad.append("verified 的证据里没写可达")
    if report["refuted"]["status"] != "refuted":
        bad.append("真 404 没有判 refuted")
    if report["unverified_egress"]["status"] == "refuted":
        bad.append("网络失败被判成假（违反硬边界：失败不得改成判假）")
    if "未取证" not in report["unverified_egress"]["evidence"]:
        bad.append("未取证的分支没在证据里留痕")
    if report["cache"]["warm"] != "ok":
        bad.append("缓存无从验证：三次探测都没拿到状态码（本机到该站的网络不通），不能算通过")
    elif report["cache"]["egress"] != 0:
        bad.append(f"缓存没生效：热缓存已建立，两次询问仍出站 {report['cache']['egress']} 次")
    if report["no_structure"]["status"] != "unverifiable":
        bad.append("无可校验结构的条目没有落到 unverifiable")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_out", help="把结果写到该路径（供文档记录与事后核对）")
    args = ap.parse_args()

    report = run_probe()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json_out}")
    bad = evaluate(report)
    if bad:
        for line in bad:
            print(f"  ✗ {line}")
        print("\nL3 真机验证：红")
        return 1
    print("\n✓ L3 真机验证：verified／refuted／未取证三态与缓存全部成立，默认档仍零出站")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
