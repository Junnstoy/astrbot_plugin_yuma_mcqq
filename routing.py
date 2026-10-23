"""Pure session command parsing and reply rendering; no framework imports."""
from __future__ import annotations

from .core import DEFAULT_TEMPLATES, TEMPLATE_FIELDS


COMMANDS = frozenset({
    "签到", "积分", "查询", "修改", "增加", "减少", "排行榜", "商店列表",
    "商店", "兑换", "补领", "帮助",
})
ALIASES = {"checkin": "签到", "points": "积分"}
MAX_REPLY_LENGTH = 20_000


def parse_command(text: str, prefix: str, woken: bool) -> tuple[str, list[str]] | None:
    """Parse text after AstrBot has removed its wake prefix.

    The single bare confirmation word is a candidate only: callers must verify
    an active purchase before consuming it. All other messages require wake.
    Arguments are kept for the command handler to validate, including mistakes.
    """
    words = text.split()
    if words == ["兑换"]:
        return "兑换", []
    if not woken or not words:
        return None
    prefix = prefix.strip()
    if prefix:
        # A configured prefix is one complete token, never a startswith match.
        if words[0] != prefix:
            return None
        words = words[1:]
        if not words:
            return None
    command = ALIASES.get(words[0].lower(), words[0])
    if command not in COMMANDS:
        return None
    return command, words[1:]


def render_reply(settings: dict, key: str, **values) -> str:
    """Render a validated template, keeping reward data private by construction.

    Never truncate a code or credential. Oversized replies fail before sending,
    so callers can retain the same order/reward for recovery.
    """
    if key not in DEFAULT_TEMPLATES:
        raise ValueError(f"未知回复模板：{key}")
    template = settings.get("templates", {}).get(key, DEFAULT_TEMPLATES[key])
    fields = dict.fromkeys(TEMPLATE_FIELDS, "")
    fields["currency_name"] = settings.get("currency_name", "积分")
    fields.update({name: value if value is not None else ""
                   for name, value in values.items() if name in TEMPLATE_FIELDS})
    if key != "purchase_private":
        fields["reward"] = ""
    rendered = template.format_map(fields)
    if len(rendered) > MAX_REPLY_LENGTH:
        raise ValueError("回复超过 20000 字符，请缩短模板或奖励内容；未截断发送")
    return rendered
