"""Pure validation and scheduling rules for session check-ins."""
from __future__ import annotations

import copy
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from string import Formatter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CHECKIN_COMMANDS = frozenset({"签到", "点数", "签到排行榜", "重置签到", "补签"})

TEMPLATE_FIELDS = frozenset({
    "user_id", "player", "points", "total", "currency_name", "day", "mc_state",
    "minutes", "product_name", "product_id", "price", "order_id", "reward", "old",
    "delta", "target_id", "items", "stock", "rank", "command_list", "confirmation_command",
})
DEFAULT_TEMPLATES = {
    "checkin_success": "签到成功！获得 {points} {currency_name}，余额：{total} {currency_name}。",
    "checkin_pending": "签到正在等待 MC 确认，预计奖励 {points} {currency_name}，暂未入账。",
    "checkin_duplicate": "今天已经提交过签到，不会重复执行。余额：{total} {currency_name}，MC 状态：{mc_state}。",
    "checkin_disabled": "本会话尚未开启签到。",
    "checkin_outside_window": "当前不在本会话允许的签到时间内。",
    "checkin_unbound": "请先绑定 Minecraft 游戏名，再进行签到。",
    "checkin_mc_failed": "MC 签到失败，本次未获得 {currency_name}，请联系管理员查看记录。",
    "checkin_mc_unknown": "MC 签到结果未确认，本次暂未获得 {currency_name}，请联系管理员核实。",
    "balance": "你的余额：{total} {currency_name}。",
    "shop_list": "本会话商店：\n{items}",
    "shop_item": "{product_id}. {product_name} — {price} {currency_name}（库存：{stock}）",
    "purchase_pending": "是否消耗 {price}{currency_name}，来购买 {product_id} {product_name}？请在 {minutes} 分钟内回复“{confirmation_command}”。",
    "purchase_delivered": "订单 {order_id} 已完成，奖励已私聊发送。余额：{total} {currency_name}。",
    "purchase_private": "你购买的 {product_name}，订单 {order_id}：\n{reward}",
    "purchase_failed": "订单 {order_id} 的私聊发送失败，已扣款且奖励已保留。请在原会话回复“{confirmation_command}”，不会重复扣款。",
    "ranking": "本会话排行榜：\n{items}",
    "ranking_item": "{rank}. {player}（{user_id}）：{total} {currency_name}",
    "admin_adjust": "已调整 {target_id}：{old} → {total} {currency_name}（变化 {delta}）。",
    "admin_query": "{target_id} 的余额：{total} {currency_name}。",
    "admin_reset": "已重置 {target_id} 今天（{day}）的插件签到状态；已有{currency_name}不扣除，未发送 MC 重置命令。游戏签到状态请管理员自行核实。",
    "makeup_success": "已为 {target_id} 补签 {day}，获得 {points} {currency_name}，余额：{total} {currency_name}。",
    "makeup_duplicate": "{target_id} 在 {day} 已有签到记录，不会重复补签。余额：{total} {currency_name}。",
    "no_permission": "你没有本会话的{currency_name}管理权限。",
    "usage": "请先使用 AstrBot 唤醒头，再输入对应分组的前缀和命令（前缀为空时省略）。可用命令：\n{command_list}",
}

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
    "makeup_commands": ["signin click {date} {player}"],
    "leaderboard_command": "signin leaderboard",
    "require_binding": True,
    "currency_name": "点数",
    "command_prefix": "",
    "checkin_prefix": "",
    "admin_ids": [],
    "confirmation_minutes": 3,
    "mc_success_keywords": ["签到成功", "成功给玩家", "今日签到已完成", "sign in success", "signin success", "successfully signed in"],
    "mc_failure_keywords": ["失败", "不存在", "离线", "已经签到", "已签到", "请不要重复签到", "already", "has signed in today", "have signed in today", "don't sign in again", "already signed", "not exist", "not online", "not found", "error", "failed"],
    "templates": DEFAULT_TEMPLATES,
    "products": [],
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
            raise ValueError("点数范围必须为 0 至 1000000000 的整数")
    if result["max_points"] < result["min_points"]:
        raise ValueError("最大点数不能小于最小点数")
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
    for key in ("commands", "makeup_commands"):
        if not isinstance(result[key], list) or len(result[key]) > 100:
            raise ValueError("MC 指令必须为列表，最多 100 条")
        commands = []
        for raw in result[key]:
            command = _clean_string(raw, "MC 指令")
            if not command:
                raise ValueError("MC 指令不能为空")
            try:
                for _, field, spec, conversion in Formatter().parse(command):
                    if field is not None and (field not in {"player", "points", "user_id", "date"} or spec or conversion):
                        raise ValueError("MC 指令仅支持 {player}、{points}、{user_id}、{date} 占位符")
            except ValueError as exc:
                raise ValueError(f"MC 指令模板格式错误：{exc}") from exc
            commands.append(command)
        result[key] = commands
    result["leaderboard_command"] = _clean_string(result["leaderboard_command"], "MC 签到排行指令")
    if not result["leaderboard_command"] or any(char in result["leaderboard_command"] for char in "{}"):
        raise ValueError("MC 签到排行指令不能为空，且必须为不含占位符的固定指令")
    if result["mc_enabled"] and (not result["server_name"] or not result["commands"]):
        raise ValueError("开启 MC 联动前，请选择服务器并填写签到指令")
    result["currency_name"] = _clean_string(result["currency_name"], "点数名称", 30)
    if not result["currency_name"]:
        raise ValueError("点数名称不能为空")
    for key, label in (("command_prefix", "点数管理与商店前缀"), ("checkin_prefix", "签到指令前缀")):
        result[key] = _clean_string(result[key], label, 30)
        if any(char.isspace() for char in result[key]):
            raise ValueError(f"{label}不能包含空白")
    if not isinstance(result["admin_ids"], list) or len(result["admin_ids"]) > 1000:
        raise ValueError("管理员 ID 必须为列表")
    result["admin_ids"] = list(dict.fromkeys(_clean_string(value, "管理员 ID", 256) for value in result["admin_ids"]))
    if any(not value or any(char.isspace() for char in value) for value in result["admin_ids"]):
        raise ValueError("管理员 ID 不能为空或含空白")
    if type(result["confirmation_minutes"]) is not int or not 1 <= result["confirmation_minutes"] <= 1440:
        raise ValueError("购买确认时间必须为 1 至 1440 分钟")
    for key in ("mc_success_keywords", "mc_failure_keywords"):
        if not isinstance(result[key], list) or len(result[key]) > 100:
            raise ValueError("MC 判定关键词必须为列表，最多 100 条")
        result[key] = list(dict.fromkeys(_clean_string(value, "MC 判定关键词", 200) for value in result[key]))
        if any(not value for value in result[key]):
            raise ValueError("MC 判定关键词不能为空")
    templates = result["templates"]
    if not isinstance(templates, dict) or set(templates) - set(DEFAULT_TEMPLATES):
        raise ValueError("回复模板包含不支持的模板名称")
    result["templates"] = {**DEFAULT_TEMPLATES, **templates}
    for key, template in result["templates"].items():
        if not isinstance(template, str) or len(template) > 10000:
            raise ValueError(f"模板 {key} 必须为不超过 10000 字符的文本")
        try:
            used_fields = set()
            for _, field, spec, conversion in Formatter().parse(template):
                if field is not None and (field not in TEMPLATE_FIELDS or spec or conversion):
                    raise ValueError(f"不支持的占位符：{field}")
                if field is not None:
                    used_fields.add(field)
            if key == "purchase_private" and "reward" not in used_fields:
                raise ValueError("私聊奖励模板必须包含 {reward}，确保实际发出奖励内容")
        except ValueError as exc:
            raise ValueError(f"模板 {key} 格式错误：{exc}") from exc
    if not isinstance(result["products"], list) or len(result["products"]) > 1000:
        raise ValueError("商品必须为列表，最多 1000 件")
    products, identifiers = [], set()
    for raw in result["products"]:
        if not isinstance(raw, dict):
            raise ValueError("商品设置必须为对象")
        identifier = _clean_string(raw.get("id", ""), "商品编号", 64)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier) or identifier in identifiers:
            raise ValueError("商品编号必须唯一，且只能使用字母、数字、下划线或连字符")
        identifiers.add(identifier)
        name = _clean_string(raw.get("name", ""), "商品名称", 100)
        if not name:
            raise ValueError("商品名称不能为空")
        price = raw.get("price")
        if type(price) is not int or not 0 <= price <= 2**53 - 1:
            raise ValueError("商品价格必须为 0 至 9007199254740991 的整数，避免网页数字精度丢失")
        purchase_limit = raw.get("purchase_limit", 0)
        if type(purchase_limit) is not int or not 0 <= purchase_limit <= 2**53 - 1:
            raise ValueError("商品限购次数必须为 0 至 9007199254740991 的整数，0 表示不限购")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("商品启用状态必须为布尔值")
        reward_type = raw.get("reward_type", "text")
        if reward_type not in {"text", "codes"}:
            raise ValueError("奖励类型只能为 text 或 codes")
        reward_text = raw.get("reward_text", "")
        if not isinstance(reward_text, str) or len(reward_text) > 10000:
            raise ValueError("奖励文本必须为不超过 10000 字符的文本")
        if reward_type == "text" and not reward_text.strip():
            raise ValueError("文本商品的奖励内容不能为空")
        codes = raw.get("codes", [])
        if not isinstance(codes, list) or len(codes) > 10000:
            raise ValueError("兑换码库存必须为列表，最多 10000 条")
        codes = [_clean_string(code, "兑换码", 2000) for code in codes]
        if any(not code for code in codes) or len(set(codes)) != len(codes):
            raise ValueError("兑换码不能重复或为空")
        products.append(dict(id=identifier, name=name, price=price, enabled=enabled,
                             reward_type=reward_type, reward_text=reward_text, codes=codes,
                             purchase_limit=purchase_limit))
    result["products"] = products
    return result


def classify_mc_output(output, settings: dict) -> tuple[str, str]:
    clean = re.sub(r"§(?:#[0-9a-f]{6}|[0-9a-fk-orx])", "", str(output or ""), flags=re.I).strip()
    lowered = clean.casefold()
    if any(keyword.casefold() in lowered for keyword in settings.get("mc_failure_keywords", DEFAULT_SETTINGS["mc_failure_keywords"]) if keyword):
        return "failed", clean
    if any(keyword.casefold() in lowered for keyword in settings.get("mc_success_keywords", DEFAULT_SETTINGS["mc_success_keywords"]) if keyword):
        return "sent", clean
    return "unknown", clean


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


def validate_day(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期必须为 YYYY-MM-DD 格式")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期不存在，请检查年月日") from exc


def render_commands(settings: dict, player: str, points: int, user_id: str, date: str | None = None) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
        raise ValueError("MC 玩家名必须为 1 至 16 位字母、数字或下划线")
    fields = {field for command in settings["commands"] for _, field, _, _ in Formatter().parse(command)}
    if "user_id" in fields:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", user_id):
            raise ValueError("用户 ID 包含无法安全填入 MC 指令的字符")
    if date is not None or "date" in fields:
        validate_day(date)
    return [command.format(player=player, points=points, user_id=user_id, date=date or "")
            for command in settings["commands"]]
