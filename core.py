"""Pure validation and scheduling rules for session check-ins."""
from __future__ import annotations

import copy
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from string import Formatter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_SETTINGS = {
    "enabled": False,
    "timezone": "Asia/Shanghai",
    "min_points": 10,
    "max_points": 30,
    "windows": [{"start": "00:00", "end": "23:59"}],
    "date_rules": [],
    "mc_enabled": False,
    "server_name": "",
    "commands": [],
    "require_binding": True,
}


def _clean_string(value, name: str, limit: int = 2048) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} 必须是长度不超过 {limit} 的文本")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError(f"{name} 不允许换行或控制字符")
    return value.strip()


def _minute(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("签到时间格式必须为 HH:MM（00:00 至 23:59）")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def validate_settings(settings: dict) -> dict:
    """Return a complete JSON-safe settings object, or raise ValueError."""
    if not isinstance(settings, dict):
        raise ValueError("会话设置必须为对象")
    result = {key: copy.deepcopy(settings.get(key, value))
              for key, value in DEFAULT_SETTINGS.items()}
    for key in ("enabled", "mc_enabled", "require_binding"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} 必须为布尔值")
    result["timezone"] = _clean_string(result["timezone"], "时区", 100)
    try:
        ZoneInfo(result["timezone"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("无效时区，请使用 Asia/Shanghai 等 IANA 时区名称") from exc
    for key in ("min_points", "max_points"):
        if type(result[key]) is not int or not 0 <= result[key] <= 1_000_000_000:
            raise ValueError("积分范围必须为 0 至 1000000000 的整数")
    if result["max_points"] < result["min_points"]:
        raise ValueError("最大积分不能小于最小积分")
    if not isinstance(result["windows"], list) or not 1 <= len(result["windows"]) <= 100:
        raise ValueError("请配置 1 至 100 个签到时间段")
    windows = []
    for window in result["windows"]:
        if not isinstance(window, dict):
            raise ValueError("签到时间段必须包含 start 和 end")
        start, end = window.get("start"), window.get("end")
        _minute(start)
        _minute(end)
        windows.append({"start": start, "end": end})
    result["windows"] = windows
    if not isinstance(result["date_rules"], list) or len(result["date_rules"]) > 1000:
        raise ValueError("日期倍率必须为列表，最多 1000 条")
    rules, seen = [], set()
    for rule in result["date_rules"]:
        if not isinstance(rule, dict):
            raise ValueError("日期倍率必须包含 date 和 multiplier")
        day = rule.get("date")
        if not isinstance(day, str) or not re.fullmatch(r"(?:\d{4}-)?\d{2}-\d{2}", day):
            raise ValueError("倍率日期必须为 YYYY-MM-DD 或 MM-DD")
        try:
            date.fromisoformat(day if len(day) == 10 else "2000-" + day)
        except ValueError as exc:
            raise ValueError(f"无效倍率日期：{day}") from exc
        if day in seen:
            raise ValueError(f"重复倍率日期：{day}")
        seen.add(day)
        try:
            multiplier = Decimal(str(rule.get("multiplier")))
        except InvalidOperation as exc:
            raise ValueError("倍率必须为数字") from exc
        if not multiplier.is_finite() or not 0 < multiplier <= 1000:
            raise ValueError("倍率必须大于 0 且不超过 1000")
        if float(multiplier) == 0:
            raise ValueError("倍率过小，无法保存为有效数字")
        rules.append({"date": day, "multiplier": float(multiplier)})
    result["date_rules"] = rules
    result["server_name"] = _clean_string(result["server_name"], "MC 服务器名称", 255)
    if not isinstance(result["commands"], list) or len(result["commands"]) > 100:
        raise ValueError("MC 指令必须为列表，最多 100 条")
    commands = []
    for raw in result["commands"]:
        command = _clean_string(raw, "MC 指令")
        if not command:
            raise ValueError("MC 指令不能为空")
        try:
            for _, field, spec, conversion in Formatter().parse(command):
                if field is not None and (field not in {"player", "points", "user_id"} or spec or conversion):
                    raise ValueError("MC 指令仅支持 {player}、{points}、{user_id} 占位符")
        except ValueError as exc:
            raise ValueError(f"MC 指令模板格式错误：{exc}") from exc
        commands.append(command)
    result["commands"] = commands
    if result["mc_enabled"] and (not result["server_name"] or not commands):
        raise ValueError("开启 MC 联动前，请选择服务器并填写签到指令")
    return result


def local_now(settings: dict, now: datetime | None = None) -> datetime:
    timezone = ZoneInfo(settings["timezone"])
    if now is None:
        return datetime.now(timezone)
    # Naive inputs are explicitly interpreted as local session time, for callers/tests.
    return now.replace(tzinfo=timezone) if now.tzinfo is None else now.astimezone(timezone)


def in_window(settings: dict, now: datetime) -> bool:
    current = now.hour * 60 + now.minute
    for window in settings["windows"]:
        start, end = _minute(window["start"]), _minute(window["end"])
        if (start <= current <= end if start <= end else current >= start or current <= end):
            return True
    return False


def date_multiplier(settings: dict, day: date) -> Decimal:
    by_date = {rule["date"]: rule["multiplier"] for rule in settings["date_rules"]}
    return Decimal(str(by_date.get(day.isoformat(), by_date.get(day.strftime("%m-%d"), 1))))


def award_points(base: int, multiplier: Decimal) -> int:
    return int((Decimal(base) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def render_commands(settings: dict, player: str, points: int, user_id: str) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
        raise ValueError("MC 玩家名必须为 1 至 16 位字母、数字或下划线")
    if any(field == "user_id" for command in settings["commands"]
           for _, field, _, _ in Formatter().parse(command)):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", user_id):
            raise ValueError("用户 ID 包含无法安全填入 MC 指令的字符")
    return [command.format(player=player, points=points, user_id=user_id)
            for command in settings["commands"]]
