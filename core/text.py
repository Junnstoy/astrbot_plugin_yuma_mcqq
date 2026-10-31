"""Deterministic text normalization, result recognition and safe interpolation."""
from __future__ import annotations

import re
import string
from datetime import date
from typing import Mapping

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HEX = re.compile(r"(?:§x(?:§[0-9a-f]){6}|&x(?:&[0-9a-f]){6}|[§&]#[0-9a-f]{6})", re.I)
LEGACY = re.compile(r"[§&][0-9a-fk-or]", re.I)
LOG = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")
FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
PLAYER = re.compile(r"[A-Za-z0-9_]{1,16}\Z")
SIGNIN_RULES = [
    {"status": "SIGNED", "pattern": r"成功给玩家\s+(?P<player>\S+)\s+签到[，,]\s*当前该玩家已连续签到\s+(?P<days>[0-9]+)\s+天[。.]?"},
    {"status": "ALREADY_SIGNED", "pattern": r"玩家\s+(?P<player>\S+)\s+今天已经签到了[，,]\s*请不要重复签到[。.]?"},
]


def clean_text(text: object, prefixes: tuple | list = ()) -> str:
    raw = str(text or "")
    if len(raw) > 65536:
        raise ValueError("响应超过 64 KiB，拒绝解析")
    raw = LEGACY.sub("", HEX.sub("", ANSI.sub("", raw)))
    raw = "".join(c for c in raw if c in "\n\r\t" or ord(c) >= 32)
    lines = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = LOG.sub("", line.strip())
        if line.startswith("Rcon:"):
            line = line[5:].strip()
        for prefix in prefixes:
            if prefix and line.startswith(str(prefix)):
                line = line[len(str(prefix)):].strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _fullmatch(pattern: str, value: str):
    # Third-party regex supplies a real execution deadline (thread timeouts do not).
    import regex
    try:
        return regex.fullmatch(pattern, value, timeout=0.05)
    except regex.error as exc:
        raise ValueError("无效的匹配表达式") from exc


def parse_rules(raw: object, rules: list, expected_player: str | None = None,
                prefixes: tuple | list = ()) -> dict:
    cleaned = clean_text(raw, prefixes)
    result = {"status": "UNKNOWN", "fields": {}, "cleaned": cleaned}
    if not cleaned or len(cleaned) > 8192:
        return result
    matches = []
    for rule in rules:
        try:
            match = _fullmatch(str(rule.get("pattern", "")), cleaned)
        except (TimeoutError, ValueError):
            result["reason"] = "invalid_or_slow_rule"
            return result
        if match:
            fields = {k: v for k, v in match.groupdict().items() if v is not None}
            if expected_player is not None and "player" in fields:
                if fields["player"].casefold() != expected_player.casefold():
                    result["reason"] = "player_mismatch"
                    return result
            matches.append((str(rule.get("status", "UNKNOWN")), fields))
    if len(matches) == 1:
        result.update(status=matches[0][0], fields=matches[0][1])
    elif len(matches) > 1:
        result["reason"] = "ambiguous_rules"
    return result


def parse_signin(raw: object, expected_player: str) -> dict:
    return parse_rules(raw, SIGNIN_RULES, expected_player, ("[手记人偶]",))


def validate_player(name: str) -> str:
    if not PLAYER.fullmatch(name):
        raise ValueError("玩家名需为 1–16 位字母、数字或下划线")
    return name


def template_fields(template: str) -> set[str]:
    found = set()
    if not isinstance(template, str) or len(template) > 16384:
        raise ValueError("模板必须是长度不超过 16 KiB 的文本")
    for _, field, spec, conversion in string.Formatter().parse(template):
        if field is not None:
            if not FIELD.fullmatch(field) or spec or conversion:
                raise ValueError("仅支持 {字段名}，不支持属性、表达式或格式转换")
            found.add(field)
    return found


def render_template(template: str, values: Mapping) -> str:
    fields = template_fields(template)
    safe = {field: str(values.get(field, "") if values.get(field) is not None else "") for field in fields}
    rendered = template.format_map(safe)
    if len(rendered) > 16384:
        raise ValueError("回复过长，请缩短模板或查询范围")
    return rendered


def render_command(template: str, values: Mapping) -> str:
    fields = template_fields(template)
    missing = fields - set(values)
    if missing:
        raise ValueError("缺少命令参数：" + ", ".join(sorted(missing)))
    for field in fields:
        value = str(values[field])
        if any(ord(c) < 32 for c in value):
            raise ValueError("命令参数不能包含换行或控制字符")
        if field in {"player", "player_name"}:
            validate_player(value)
        if field == "date":
            date.fromisoformat(value)
    command = render_template(template, values).strip().lstrip("/")
    if not command or len(command) > 2048 or any(ord(c) < 32 for c in command):
        raise ValueError("RCON 必须是一条不超过 2048 字符的控制台命令")
    return command
