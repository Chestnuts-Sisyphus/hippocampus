# 由前身 hippocampus_prototype/event_time.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 写入侧 -- 事件时间纯规则引擎（零 LLM、零外部调用、纯标准库）

从消息文本中解析「事件发生时间」的绝对毫秒时间戳。
解析不出返回 None（上层用记录时间兜底）。

解析顺序（按层扫完整消息，层内从前往后，首个命中即返回）：
  1. 绝对时间正则（完整日期 / 月日）
  2. 相对日历词表（昨天/前天/上周/去年...）
  3. 相对时长正则（3天前 / 5小时后...）
  4. 上下文锚点词表（那天/当时... -> session_anchors 最近一条）
  5. 模糊词表 -> 明确返回 None（小时候/以前/最近...）
  6. 未命中 -> None
"""

import calendar
import re
from datetime import date, datetime, timedelta

# ---- 层 2：相对日历词表 ----
# (关键词, 偏移类型) -> 在 _cal_offset 中处理
# 偏移类型含义：
#   "day_offset":  当日起 +N 天
#   "week_start":  本周/上周/上上周的周一
#   "month_start": 本月/上月/下月 1 日
#   "year_start":  今年/去年/前年 1 月 1 日
_CALENDAR_WORDS = [
    # 顺序敏感：长词在前防短词截断（如「大前天」含「前天」）
    ("大前天", "day_offset", -3),
    ("前天", "day_offset", -2),
    ("昨天", "day_offset", -1),
    ("昨日", "day_offset", -1),
    ("今天", "day_offset", 0),
    ("今日", "day_offset", 0),
    ("明天", "day_offset", 1),
    ("明日", "day_offset", 1),
    ("上上周", "week_start", -2),
    ("上上礼拜", "week_start", -2),
    ("上周", "week_start", -1),
    ("上个礼拜", "week_start", -1),
    ("上星期", "week_start", -1),
    ("本周", "week_start", 0),
    ("这周", "week_start", 0),
    ("本星期", "week_start", 0),
    ("这星期", "week_start", 0),
    ("下周", "week_start", 1),
    ("下星期", "week_start", 1),
    ("去年", "year_start", -1),
    ("前年", "year_start", -2),
    ("今年", "year_start", 0),
    ("上个月", "month_start", -1),
    ("上月", "month_start", -1),
    ("这个月", "month_start", 0),
    ("本月", "month_start", 0),
    ("下个月", "month_start", 1),
    ("下月", "month_start", 1),
]

# 下周+星期几（如「下周三」）
_NEXT_WEEK_RE = re.compile(r"下周([一二三四五六日天])")

# 层 4：上下文锚点词
_ANCHOR_WORDS = ["那天", "那次", "当时", "那会儿"]

# 层 5：模糊词（明确返回 None）
_FUZZY_WORDS = [
    "小时候",
    "大学时",
    "高中时",
    "初中时",
    "小学时",
    "以前",
    "之前",
    "最近",
    "前几天",
    "这几天",
    "将来",
    "以后",
    "周末",
]

# 中文数字 -> 阿拉伯（仅用于下周X 的星期几）
_CN_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

# 中文数字开头标志 -> 不硬算（宁缺毋滥）
_CN_NUM_PREFIX = ("几", "数", "半", "好")

# ---- 正则 ----

# 层 1a：完整日期 YYYY-MM-DD / YYYY年MM月DD日
_RE_FULL_DATE = re.compile(r"(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})[日号]?")

# 层 1b：月日（无年份）MM月DD日
_RE_MD_DATE = re.compile(r"(\d{1,2})月(\d{1,2})[日号]")

# 层 3：相对时长 N单位前/后
_RE_REL_DURATION = re.compile(r"(\d+)\s*(分钟|小时|天|日|周|个月|年)\s*(前|后)")


def _date_to_local_ms(d: date) -> int:
    """将 date 转为当日 00:00 本地时间戳（毫秒）。

    用 date 运算再转 timestamp，防夏令时坑：
    date 不含时区/时间，构造 datetime 时用 midnight，
    fromtimestamp 取本地时区。
    """
    # 构造本地时区 midnight datetime，再转 timestamp
    dt = datetime(d.year, d.month, d.day, 0, 0, 0)
    return int(dt.timestamp() * 1000)


def _msg_date(msg_time_ms: int) -> date:
    """从毫秒时间戳取本地日历日。"""
    return datetime.fromtimestamp(msg_time_ms / 1000).date()


def _monday_of_week(d: date, week_offset: int = 0) -> date:
    """取 d 所在周（+偏移周）的周一。

    weekday(): Monday=0 .. Sunday=6
    """
    monday = d - timedelta(days=d.weekday())
    return monday + timedelta(weeks=week_offset)


def _parse_layer1_full_date(text: str, msg_time_ms: int) -> int | None:
    """层 1a：完整日期 YYYY-MM-DD。"""
    m = _RE_FULL_DATE.search(text)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    return _date_to_local_ms(d)


def _parse_layer1_md_date(text: str, msg_time_ms: int) -> int | None:
    """层 1b：月日（无年份），取 msg_time 所在年；未来 -> 年份减一。"""
    m = _RE_MD_DATE.search(text)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    msg_d = _msg_date(msg_time_ms)
    try:
        d = date(msg_d.year, month, day)
    except ValueError:
        return None
    # 若结果 > msg_time+1 天（未来），年份减一
    result_ms = _date_to_local_ms(d)
    if result_ms > msg_time_ms + 86400_000:  # +1 天容差
        try:
            d = date(msg_d.year - 1, month, day)
        except ValueError:
            return None
        result_ms = _date_to_local_ms(d)
    return result_ms


def _parse_layer2_calendar(text: str, msg_time_ms: int) -> int | None:
    """层 2：相对日历词表。"""
    msg_d = _msg_date(msg_time_ms)

    # 先查下周X（如「下周三」）
    m = _NEXT_WEEK_RE.search(text)
    if m:
        wd = _CN_WEEKDAY.get(m.group(1))
        if wd is not None:
            monday = _monday_of_week(msg_d, 1)
            target = monday + timedelta(days=wd - 1)
            return _date_to_local_ms(target)

    # 查固定词表（长词优先）
    for word, offset_type, offset in _CALENDAR_WORDS:
        if word in text:
            if offset_type == "day_offset":
                d = msg_d + timedelta(days=offset)
            elif offset_type == "week_start":
                d = _monday_of_week(msg_d, offset)
            elif offset_type == "month_start":
                month = msg_d.month + offset
                year = msg_d.year
                while month < 1:
                    month += 12
                    year -= 1
                while month > 12:
                    month -= 12
                    year += 1
                try:
                    d = date(year, month, 1)
                except ValueError:
                    return None
            elif offset_type == "year_start":
                d = date(msg_d.year + offset, 1, 1)
            else:
                continue
            return _date_to_local_ms(d)

    return None


def _parse_layer3_duration(text: str, msg_time_ms: int) -> int | None:
    """层 3：相对时长 N单位前/后。"""
    m = _RE_REL_DURATION.search(text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    direction = m.group(3)

    sign = -1 if direction == "前" else 1
    n *= sign

    if unit == "分钟":
        return msg_time_ms + n * 60_000
    elif unit == "小时":
        return msg_time_ms + n * 3_600_000
    elif unit in ("天", "日"):
        d = _msg_date(msg_time_ms) + timedelta(days=n)
        return _date_to_local_ms(d)
    elif unit == "周":
        d = _msg_date(msg_time_ms) + timedelta(weeks=n)
        return _date_to_local_ms(d)
    elif unit == "个月":
        return _add_months(msg_time_ms, n)
    elif unit == "年":
        # 年单位取该年 1 月 1 日（与「去年」语义一致，拍板 5）
        return _add_months(msg_time_ms, n * 12, to_first_day=True)
    return None


def _add_months(msg_time_ms: int, months: int, to_first_day: bool = False) -> int:
    """按日历月偏移。

    to_first_day=False: 取目标月同日 00:00，同日不存在则取目标月末（个月单位用）
    to_first_day=True:  取目标年 1 月 1 日 00:00（年单位用，与「去年」语义一致）
    """
    d = _msg_date(msg_time_ms)
    month = d.month + months
    year = d.year
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    if to_first_day:
        target = date(year, 1, 1)
    else:
        # 目标日 = min(源日, 目标月天数)，月末兜底
        max_day = calendar.monthrange(year, month)[1]
        target = date(year, month, min(d.day, max_day))
    return _date_to_local_ms(target)


def _parse_layer4_anchor(text: str, msg_time_ms: int, session_anchors: list[int] | None) -> int | None:
    """层 4：上下文锚点词 -> session_anchors 最近一条。"""
    for word in _ANCHOR_WORDS:
        if word in text:
            if session_anchors:
                return session_anchors[-1]
            return None
    return None


def _is_fuzzy(text: str) -> bool:
    """层 5：模糊词检查。"""
    for word in _FUZZY_WORDS:
        if word in text:
            return True
    return False


def parse_time(text: str, msg_time_ms: int, session_anchors: list[int] | None = None) -> int | None:
    """从消息文本解析事件发生时间。

    Args:
        text: 消息文本
        msg_time_ms: 消息记录时间（毫秒时间戳，本地时区）
        session_anchors: 当前会话已解析的事件时间锚点列表（每条=此前解析出的 event_time）

    Returns:
        事件时间毫秒时间戳，或 None（解析不出，上层用记录时间兜底）
    """
    if not text or not text.strip():
        return None

    # 层 1：绝对时间（先扫全消息）
    result = _parse_layer1_full_date(text, msg_time_ms)
    if result is not None:
        return result
    result = _parse_layer1_md_date(text, msg_time_ms)
    if result is not None:
        return result

    # 层 2：相对日历词
    result = _parse_layer2_calendar(text, msg_time_ms)
    if result is not None:
        return result

    # 层 3：相对时长
    result = _parse_layer3_duration(text, msg_time_ms)
    if result is not None:
        return result

    # 层 4：上下文锚点
    result = _parse_layer4_anchor(text, msg_time_ms, session_anchors)
    if result is not None:
        return result

    # 层 5：模糊词 -> 明确 None
    if _is_fuzzy(text):
        return None

    # 层 6：未命中
    return None
