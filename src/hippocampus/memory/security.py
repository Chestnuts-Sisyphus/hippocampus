# 由前身 hippocampus_prototype/security.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
security.py — 规则级内容注入检测（任务书 P04：内容注入检测 + 注入前甄别）

五组内置规则（中文优先；A/B/D 行为不变，C/E 升级为提取前硬拦截）：
  A 指令注入（severity=2 高危）：忽略/无视/不要理会 + 指令/规则/要求/上面/以上
  B 身份仿冒（severity=2 高危）：你是/你现在是 + (chatgpt|claude|gpt|ai|系统|助手)
  C 敏感值（severity=1 可疑）：sk-开头密钥 / api_key: / 密码: / token: 等值模式
    + token 状态陈述（TOKEN 有效 / 额度）
    （只匹配「值」不匹配泛词——「密钥存在 config.yaml」这类正常 resource 不误伤）
  D 对话劫持（severity=1 可疑）：从现在(开始|起) / 请只回复 / 输出(格式|以下内容)
  E PII（severity=1 可疑）：18 位身份证 / 16-19 位卡号 / 11 位手机号 / 邮箱
    （只匹配具体值；「我办了张银行卡」「留个手机号呗」泛述不命中）

拦截语义（P0，2026-08-15）：
  - A/B/D：命中只标记不拦截（控制台可见可删，P04 原行为）
  - C/E：提取前硬拦截（不入库）。extract()/extract_response_items() 命中直接空返回；
    调用方对 E 组可跳过 extract 调用。C 组仍进入 extract() 以便 P04 mock 保持标记语义。

config（hippocampus_config.json）security 节：
  {"enabled": true, "extra_rules": [{"pattern": "...", "severity": 1}, ...]}
  - extra_rules 追加参与检测；pattern 编译失败 -> stderr 警告并跳过（不崩）
  - enabled=false -> check_text 恒返回 []（全链路跳过，写入侧 flag 恒 0）

check_text(text) -> [{"rule_id", "group", "severity"}, ...]；空命中返回 []
flag 值由调用方取 max(severity)（无命中=0），见 max_flag()。
should_hard_block(hits) -> C/E 是否应硬拦截。
"""

import re
import sys

SEV_HIGH = 2  # 高危（A/B 组）
SEV_SUSPECT = 1  # 可疑（C/D 组）

# 硬拦截组：C 密钥/敏感值 + E PII。A/B/D 维持「只标记不拦截」。
HARD_BLOCK_GROUPS = frozenset({"C", "E"})

# 内置规则库（A/B=高危 2，C/D/E=可疑 1）
# A/B/D：命中只标记不拦截，控制台可见可删
# C/E：提取前硬拦截（见 should_hard_block / extract 入口）
BUILTIN_RULES = [
    # ---- A 指令注入（高危）----
    {
        "rule_id": "A1",
        "group": "A",
        "severity": SEV_HIGH,
        "pattern": r"(?:忽略|无视|不要理会|别理会|不用理会)(?:上面|以上|之前|先前|前面|此前)?(?:的)?(?:所有|全部|任何|其它|其他)?(?:指令|规则|要求|指示|命令|设定|内容|消息|话)",
    },
    {
        "rule_id": "A2",
        "group": "A",
        "severity": SEV_HIGH,
        "pattern": r"(?:请|请你|记住|记得|务必)(?:忽略|无视|不要理会|别理会|不用理会)(?:上面|以上|之前)?(?:的)?(?:指令|规则|要求|指示|设定|内容)",
    },
    {
        "rule_id": "A3",
        "group": "A",
        "severity": SEV_HIGH,
        "pattern": r"(?:上面|以上|之前|先前|此前)(?:的)?(?:所有|全部|任何)?(?:指令|规则|要求|指示|设定)(?:都|均)?(?:不|别|勿|无需|不必)(?:需要|用|要|需)?(?:执行|遵守|遵循|理会|采纳|响应)",
    },
    {
        "rule_id": "A4",
        "group": "A",
        "severity": SEV_HIGH,
        "pattern": r"(?:不要|别|勿|无需|不必)(?:遵守|执行|遵循|理会|采纳|响应)(?:上面|以上|之前)?(?:的)?(?:指令|规则|要求|指示|设定|命令)",
    },
    {
        "rule_id": "A5",
        "group": "A",
        "severity": SEV_HIGH,
        "pattern": r"(?:忘记|忘掉|清除|清空)(?:上面|以上|之前)?(?:的)?(?:所有|全部|任何)?(?:的)?(?:指令|规则|要求|指示|设定)",
    },
    {
        "rule_id": "A6",
        "group": "A",
        "severity": SEV_HIGH,
        "pattern": r"(?:忽略|无视|不要理会)(?:(?:系统|原始|原有|最初|预设)(?:的)?){1,2}(?:指令|规则|提示|设定|内容)",
    },
    # ---- B 身份仿冒（高危）----
    {
        "rule_id": "B1",
        "group": "B",
        "severity": SEV_HIGH,
        "pattern": r"(?:你是|你现在是|你就是|你其实是)\s*(?:一个|一名|一位)?\s*(?:chatgpt|claude|gpt-?[0-9]?|ai|人工智能|ai助手|智能体)",
    },
    {
        "rule_id": "B2",
        "group": "B",
        "severity": SEV_HIGH,
        "pattern": r"(?:你是|你现在是|你就是|你其实是)\s*(?:一个|一名|一位)?\s*(?:系统|助手|助理|客服|机器人)",
    },
    {
        "rule_id": "B3",
        "group": "B",
        "severity": SEV_HIGH,
        "pattern": r"(?:从现在起|从现在开始|接下来|今后)\s*(?:,|，)?\s*(?:你|请你|请)\s*(?:就)?\s*(?:是|扮演|作为|变成)\s*(?:一个|一名|一位)?\s*(?:chatgpt|claude|gpt-?[0-9]?|ai|系统|助手|人工智能)",
    },
    {
        "rule_id": "B4",
        "group": "B",
        "severity": SEV_HIGH,
        "pattern": r"(?:扮演|假装|充当)\s*(?:一个|一名|一位)?\s*(?:chatgpt|claude|gpt-?[0-9]?|ai|系统|助手|机器人)",
    },
    {
        "rule_id": "B5",
        "group": "B",
        "severity": SEV_HIGH,
        "pattern": r"(?:你的|你现在的)\s*(?:身份|角色|名字|名称)\s*(?:是|改为|变成|设定为)",
    },
    {
        "rule_id": "B6",
        "group": "B",
        "severity": SEV_HIGH,
        "pattern": r"(?:我|我们)\s*(?:要求|命令|指示|希望)\s*你\s*(?:是|成为|扮演)\s*(?:一个)?\s*(?:chatgpt|claude|gpt-?[0-9]?|ai|系统|助手)",
    },
    # ---- C 敏感值（可疑：只匹配值模式，不匹配泛词）----
    {"rule_id": "C1", "group": "C", "severity": SEV_SUSPECT, "pattern": r"sk-[A-Za-z0-9]{16,}"},
    {"rule_id": "C2", "group": "C", "severity": SEV_SUSPECT, "pattern": r"(?:api[\s_-]*key|apikey)\s*[:：]\s*\S+"},
    {"rule_id": "C3", "group": "C", "severity": SEV_SUSPECT, "pattern": r"密码\s*[:：]\s*\S+"},
    {"rule_id": "C4", "group": "C", "severity": SEV_SUSPECT, "pattern": r"token\s*[:：]\s*\S+"},
    {
        "rule_id": "C5",
        "group": "C",
        "severity": SEV_SUSPECT,
        "pattern": r"(?:secret|密钥|访问密钥|access[_-]?key)\s*[:：]\s*\S+",
    },
    {
        "rule_id": "C6",
        "group": "C",
        "severity": SEV_SUSPECT,
        "pattern": r"(?:access_token|secret_key|private[_-]?key|password)\s*[:：]\s*\S+",
    },
    # token 状态陈述（真实泄漏句：GITHUB_TOKEN 有效 / 额度 / 账号）
    # 不匹配「token 无效」「token 环境变量」等泛述（\s*有效 不会吃掉「无」）
    {
        "rule_id": "C7",
        "group": "C",
        "severity": SEV_SUSPECT,
        "pattern": r"(?:[A-Za-z][A-Za-z0-9_]*)?token\s*有效",
    },
    {
        "rule_id": "C8",
        "group": "C",
        "severity": SEV_SUSPECT,
        "pattern": r"(?:[A-Za-z][A-Za-z0-9_]*)?token.{0,48}\d+\s*/\s*\d+\s*额度",
    },
    # ---- D 对话劫持（可疑）----
    {"rule_id": "D1", "group": "D", "severity": SEV_SUSPECT, "pattern": r"从现在(?:开始|起)"},
    {
        "rule_id": "D2",
        "group": "D",
        "severity": SEV_SUSPECT,
        "pattern": r"请只回复(?:以下|如下|指定|我写的|规定的)?(?:内容|文本|文字|话)?",
    },
    {
        "rule_id": "D3",
        "group": "D",
        "severity": SEV_SUSPECT,
        "pattern": r"输出(?:格式|以下内容|如下内容|内容为|指定格式|要求)",
    },
    {
        "rule_id": "D4",
        "group": "D",
        "severity": SEV_SUSPECT,
        "pattern": r"(?:只|仅)(?:输出|回复|回答)(?:以下|如下|指定|我)",
    },
    {
        "rule_id": "D5",
        "group": "D",
        "severity": SEV_SUSPECT,
        "pattern": r"(?:不要|别|禁止)(?:输出|回复|回答|提及|说)(?:任何|别的|其他|多余){1,2}(?:内容|信息|东西|字|文字)",
    },
    {"rule_id": "D6", "group": "D", "severity": SEV_SUSPECT, "pattern": r"(?:你|请)只(?:需|要|能)?(?:回复|输出|回答)"},
    # ---- E PII（可疑：只匹配具体值，不匹配泛词）----
    # 18 位身份证：区划 + 年(18/19/20xx) + 月日 + 顺序码 + 校验位；含校验位模式但不强制校验和
    {
        "rule_id": "E1",
        "group": "E",
        "severity": SEV_SUSPECT,
        "pattern": r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
    },
    # 银行卡：银联 62 开头 16-19 位 / Visa 4 开头 16 位 / Master 51-55 开头 16 位
    # 观察样本 6222021234567890（无 Luhn，故不强制校验）
    {
        "rule_id": "E2",
        "group": "E",
        "severity": SEV_SUSPECT,
        "pattern": r"(?<!\d)(?:62\d{14,17}|4\d{15}|5[1-5]\d{14})(?!\d)",
    },
    # 大陆 11 位手机号；前后不能是数字（避免吃进更长数字串）
    {
        "rule_id": "E3",
        "group": "E",
        "severity": SEV_SUSPECT,
        "pattern": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    },
    # 标准邮箱
    {
        "rule_id": "E4",
        "group": "E",
        "severity": SEV_SUSPECT,
        "pattern": r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![A-Za-z])",
    },
]

# 正则编译缓存（模块级，规则全集固定 + extra_rules 追加）
_PAT_CACHE: dict = {}


def _compile(pattern: str):
    """编译正则（带缓存）；失败返回 None 并 stderr 警告（不崩）。"""
    if pattern in _PAT_CACHE:
        return _PAT_CACHE[pattern]
    try:
        pat = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        sys.stderr.write(f"[security] 规则编译失败（跳过该条）: {e}\n")
        pat = None
    _PAT_CACHE[pattern] = pat
    return pat


def get_security_config() -> dict:
    """读取 security 配置节。缺省 {"enabled": True, "extra_rules": []}；
    配置文件异常时按缺省返回（安全兜底：默认开启检测）。"""
    try:
        from hippocampus.memory import config

        sec = config.get_config().get("security") or {}
    except Exception:
        sec = {}
    return {
        "enabled": bool(sec.get("enabled", True)),
        "extra_rules": sec.get("extra_rules") or [],
    }


def check_text(text) -> list:
    """检测文本，返回命中列表 [{rule_id, group, severity}, ...]；空命中返回 []。

    - 空文本 -> []
    - security.enabled=false -> []（全链路跳过，写入侧 flag 恒 0）
    - extra_rules 追加参与检测（每条 {pattern, severity}，编译失败跳过）
    """
    if not text:
        return []
    sec = get_security_config()
    if not sec["enabled"]:
        return []
    hits = []
    for rule in BUILTIN_RULES:
        pat = _compile(rule["pattern"])
        if pat is not None and pat.search(text):
            hits.append({"rule_id": rule["rule_id"], "group": rule["group"], "severity": rule["severity"]})
    for extra in sec["extra_rules"]:
        try:
            pattern = str(extra["pattern"])
            severity = int(extra.get("severity", SEV_SUSPECT))
        except (KeyError, TypeError, ValueError):
            sys.stderr.write(f"[security] extra_rule 格式非法（跳过）: {extra}\n")
            continue
        pat = _compile(pattern)
        if pat is not None and pat.search(text):
            hits.append({"rule_id": "X1", "group": "X", "severity": severity})
    return hits


def max_flag(hits) -> int:
    """命中列表 -> flag 值（max severity；空列表=0）。"""
    return max((h["severity"] for h in hits), default=0)


def should_hard_block(hits) -> bool:
    """C 组（密钥）与 E 组（PII）提取前硬拦截；A/B/D 只标记。"""
    return any(h.get("group") in HARD_BLOCK_GROUPS for h in (hits or []))


def precheck(text) -> tuple:
    """提取前检测。返回 (hits, flag, skip_extract_pii)。

    skip_extract_pii=True 仅当 E 组命中（PII 不调 extract）。
    C 组不在此跳过 extract 调用——交给 extract() 内部拦截，
    以便 P04 对 pipeline.extract / import_runner.extract 的 mock 仍能写入标记记忆。
    检测失败返回 ([], 0, False)（与现有写入侧软失败一致）。
    """
    try:
        hits = check_text(text)
        skip_pii = any(h.get("group") == "E" for h in hits)
        return hits, max_flag(hits), skip_pii
    except Exception:
        return [], 0, False


def hits_pii(text) -> bool:
    """文本是否命中 E 组 PII（写入侧纵深：丢弃已提取的 PII 记忆）。"""
    try:
        return any(h.get("group") == "E" for h in check_text(text))
    except Exception:
        return False
