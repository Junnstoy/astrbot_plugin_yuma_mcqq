"""Safe reply template rendering; command routing is owned by AstrBot."""
from __future__ import annotations

from .core import DEFAULT_TEMPLATES, TEMPLATE_FIELDS


MAX_REPLY_LENGTH = 20_000


def render_reply(settings: dict, key: str, **values) -> str:
    """Render a validated template, keeping reward data private by construction.

    Never truncate a code or credential. Oversized replies fail before sending,
    so callers can retain the same order/reward for recovery.
    """
    if key not in DEFAULT_TEMPLATES:
        raise ValueError(f"未知回复模板：{key}")
    template = settings.get("templates", {}).get(key, DEFAULT_TEMPLATES[key])
    fields = dict.fromkeys(TEMPLATE_FIELDS, "")
    fields["currency_name"] = settings.get("currency_name", "点数")
    fields.update({name: value if value is not None else ""
                   for name, value in values.items() if name in TEMPLATE_FIELDS})
    if key != "purchase_private":
        fields["reward"] = ""
    rendered = template.format_map(fields)
    if len(rendered) > MAX_REPLY_LENGTH:
        raise ValueError("回复超过 20000 字符，请缩短模板或奖励内容；未截断发送")
    return rendered
