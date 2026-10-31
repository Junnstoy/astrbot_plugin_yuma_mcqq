"""Pure local-calendar eligibility and reward rules; no clock, RNG or storage I/O."""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_SIGNIN_TIME = {"enabled": False, "start": "00:00", "end": "23:59"}
MAX_BASE_REWARD = 1_000_000
MAX_SIGNIN_REWARD = 1_000_000_000


def _integer(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} 必须是 {low}–{high} 之间的整数")
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("奖励活动日期必须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("奖励活动日期无效") from exc


def _multiplier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("活动倍率必须是 1–1000 的十进制数，最多 6 位小数")
    text = str(value)
    if len(text) > 32:
        raise ValueError("活动倍率文本过长")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("活动倍率必须是有效的十进制数") from exc
    if not number.is_finite() or not Decimal(1) <= number <= Decimal(1000) or number.normalize().as_tuple().exponent < -6:
        raise ValueError("活动倍率必须在 1–1000 之间，最多 6 位小数")
    return format(number.normalize(), "f")


def normalize_signin_policy(route):
    """Return a new canonical policy without mutating a route or its nested lists."""
    if not isinstance(route, dict):
        raise ValueError("签到策略必须是对象")
    minimum = _integer(route.get("points_min", 10), 0, MAX_BASE_REWARD, "签到最小点数")
    maximum = _integer(route.get("points_max", 10), minimum, MAX_BASE_REWARD, "签到最大点数")
    window = copy.deepcopy(route.get("signin_time", DEFAULT_SIGNIN_TIME))
    if not isinstance(window, dict) or set(window) - set(DEFAULT_SIGNIN_TIME):
        raise ValueError("签到时段只能包含 enabled、start 和 end")
    for key, value in DEFAULT_SIGNIN_TIME.items():
        window.setdefault(key, value)
    if not isinstance(window["enabled"], bool):
        raise ValueError("签到时段 enabled 必须为布尔值")
    for key in ("start", "end"):
        if not isinstance(window[key], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", window[key]):
            raise ValueError("签到时段必须是 HH:MM，范围 00:00–23:59")
    source = route.get("reward_events", [])
    if not isinstance(source, list) or len(source) > 200:
        raise ValueError("奖励活动必须是数组，最多 200 项")
    events, ids = [], set()
    allowed = {"id", "name", "enabled", "start_date", "end_date", "mode", "priority", "multiplier", "points_min", "points_max"}
    for raw in source:
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError("奖励活动包含未知字段或不是对象")
        event = copy.deepcopy(raw)
        event_id = event.get("id")
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", event_id) or event_id in ids:
            raise ValueError("奖励活动 ID 必须唯一，且只含 1–64 个字母、数字、下划线或横线")
        ids.add(event_id)
        event.setdefault("name", event_id)
        event.setdefault("enabled", True)
        event.setdefault("priority", 0)
        if not isinstance(event["name"], str) or not event["name"].strip() or len(event["name"]) > 100 or any(ord(c) < 32 for c in event["name"]):
            raise ValueError("奖励活动名称须为 1–100 个字符的单行文字")
        event["name"] = event["name"].strip()
        if not isinstance(event["enabled"], bool):
            raise ValueError("奖励活动 enabled 必须为布尔值")
        _integer(event["priority"], -1_000_000, 1_000_000, "活动优先级")
        start, end = _date(event.get("start_date")), _date(event.get("end_date"))
        if start > end:
            raise ValueError("奖励活动开始日期不能晚于结束日期")
        if event.get("mode") not in {"multiplier", "range"}:
            raise ValueError("奖励活动模式必须为 multiplier 或 range")
        event["multiplier"] = _multiplier(event.get("multiplier", "1"))
        event["points_min"] = _integer(event.get("points_min", minimum), 0, MAX_BASE_REWARD, "活动最小点数")
        event["points_max"] = _integer(event.get("points_max", maximum), event["points_min"], MAX_BASE_REWARD, "活动最大点数")
        if event["enabled"]:
            for other in events:
                if (other["enabled"] and other["priority"] == event["priority"]
                        and event["start_date"] <= other["end_date"] and other["start_date"] <= event["end_date"]):
                    raise ValueError("启用的奖励活动日期重叠且优先级相同，请调整日期或优先级")
        events.append(event)
    return {"points_min": minimum, "points_max": maximum, "signin_time": window, "reward_events": events}


def policy_local_time(at, zone):
    """A naive UI datetime is local wall time; offset-bearing input is converted."""
    try:
        zone = ZoneInfo(zone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("预览时区必须是有效的 IANA 时区") from exc
    if isinstance(at, str):
        if len(at) > 64:
            raise ValueError("预览时间过长")
        try:
            at = datetime.fromisoformat(at)
        except ValueError as exc:
            raise ValueError("预览时间必须为 ISO8601 日期时间") from exc
    if not isinstance(at, datetime):
        raise ValueError("预览时间必须为日期时间")
    if at.tzinfo is None:
        local = at.replace(tzinfo=zone)
        if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != at:
            raise ValueError("该当地时间因夏令时切换不存在，请选择有效时间或填写 UTC 偏移")
        return local
    return at.astimezone(zone)


def reward_from_draw(policy, value):
    bounds = policy["draw_range"]
    _integer(value, bounds["min"], bounds["max"], "模拟抽样点数")
    amount = int((Decimal(value) * Decimal(policy["multiplier"])).to_integral_value(rounding=ROUND_FLOOR))
    return _integer(amount, 0, MAX_SIGNIN_REWARD, "最终签到点数")


def preview_signin_policy(route, at, base_points=None):
    policy = normalize_signin_policy(route)
    zone = route.get("timezone") or "Asia/Shanghai"
    local = policy_local_time(at, zone)
    day, minute = local.date().isoformat(), local.strftime("%H:%M")
    window = policy["signin_time"]
    allowed = (not window["enabled"] or
               (window["start"] <= minute <= window["end"] if window["start"] <= window["end"]
                else minute >= window["start"] or minute <= window["end"]))
    active = [event for event in policy["reward_events"] if event["enabled"] and event["start_date"] <= day <= event["end_date"]]
    event = max(active, key=lambda item: item["priority"]) if active else None
    base_range = {"min": policy["points_min"], "max": policy["points_max"]}
    draw_range = ({"min": event["points_min"], "max": event["points_max"]}
                  if event and event["mode"] == "range" else dict(base_range))
    multiplier = event["multiplier"] if event and event["mode"] == "multiplier" else "1"
    result = {"allowed": allowed, "day": day, "local_time": local.isoformat(), "timezone": zone,
              "window": window, "event": event, "base_range": base_range, "draw_range": draw_range,
              "multiplier": multiplier, "base_points": base_points, "final_points": None}
    result["effective_range"] = {"min": reward_from_draw(result, draw_range["min"]),
                                  "max": reward_from_draw(result, draw_range["max"])}
    if base_points is not None:
        result["final_points"] = reward_from_draw(result, base_points)
    return result
