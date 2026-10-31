"""Structured OneBot ↔ vanilla Minecraft rendering without framework imports."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import string
import time
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..core.text import render_template, template_fields
from .media import MediaRejected, MediaUnavailable

log = logging.getLogger(__name__)
URL = re.compile(r"(?<![\w@])(?:https?://|www\.)[^\s<>\[\]§]+", re.I)
FORMAT = re.compile(r"[§&]x(?:[§&][0-9a-f]){6}|[§&]#[0-9a-f]{6}|[§&][0-9a-fk-or]", re.I)
COLOR_NAMES = dict(zip("0123456789abcdef", ("black", "dark_blue", "dark_green", "dark_aqua", "dark_red", "dark_purple", "gold", "gray", "dark_gray", "blue", "green", "aqua", "red", "light_purple", "yellow", "white")))
DECORATIONS = {"k": "obfuscated", "l": "bold", "m": "strikethrough", "n": "underlined", "o": "italic"}
LABELS = {"image": "图片", "record": "语音", "video": "视频", "file": "文件"}
MESSAGE_COLORS = {"link": "blue", "image": "blue", "record": "blue", "video": "blue", "file": "blue",
                  "forward": "blue", "reply": "gray", "face": "green", "at": "yellow"}
REPLY_HOVER = "{sender}\n{message}\nDate: {time}"
REPLY_LABEL = "[回复 {sender}]"
EVENTS = {"player_chat": "PlayerChatEvent", "player_join": "PlayerJoinEvent", "player_quit": "PlayerQuitEvent", "player_death": "PlayerDeathEvent", "player_achievement": "PlayerAchievementEvent", "player_advancement": "PlayerAchievementEvent"}


def _safe_url(value: str) -> str:
    value = str(value).strip()
    if any(ord(char) < 32 for char in value):
        return ""
    if value.lower().startswith("www."):
        value = "https://" + value
    elif value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return ""


def _segment_url(data: dict) -> str:
    """Find an already-provided URL without resolving or downloading anything."""
    for key in ("url", "file", "file_path", "path"):
        source = _safe_url(data.get(key, ""))
        if source:
            return source
    return ""


def strip_styles(value: str) -> str:
    # Ampersands in URL query strings are data, not Minecraft style markers.
    output, start = [], 0
    for match in URL.finditer(value):
        output.extend((FORMAT.sub("", value[start:match.start()]), match.group()))
        start = match.end()
    output.append(FORMAT.sub("", value[start:]))
    return "".join(output)


def styled_components(text: str, *, modern: bool = False, color: str = "white", links: bool = True,
                      link_color: str = "blue") -> list[dict]:
    """Translate legacy colour/style codes to actual JSON properties, preserving URLs."""
    return _styled_parts([text], modern=modern, color=color, links=links, link_color=link_color)


def _styled_parts(parts: list[str | dict], *, modern: bool, color: str, links: bool, link_color: str = "blue") -> list[dict]:
    """Keep text formatting state across inserted components without modifying them."""
    state = {"color": color, **{name: False for name in DECORATIONS.values()}}
    result = []

    def emit(value: str):
        if value:
            result.append({**minecraft_component(value, color=state["color"]), **state})

    def plain(value: str):
        cursor = 0
        for match in FORMAT.finditer(value):
            emit(value[cursor:match.start()])
            token = match.group().lower()
            code = token[1]
            if code in COLOR_NAMES or code in ("x", "#", "r"):
                state.update({name: False for name in DECORATIONS.values()})
                state["color"] = ("#" + token[3::2]) if code == "x" else token[1:] if code == "#" else COLOR_NAMES.get(code, color)
            else:
                state[DECORATIONS[code]] = True
            cursor = match.end()
        emit(value[cursor:])

    for text in parts:
        if isinstance(text, dict):
            result.append(deepcopy(text))
            continue
        cursor = 0
        for match in URL.finditer(text):
            plain(text[cursor:match.start()])
            source = match.group().rstrip("。，！；,;)")
            if links:
                result.append(minecraft_component("[链接]", hover=source, url=_safe_url(source), modern=modern, color=link_color))
            else:
                emit(source)
            cursor = match.start() + len(source)
        plain(text[cursor:])
    return result


def _prefix_components(template: str, values: dict, replies: list[dict], *, modern: bool) -> list[dict]:
    # Validate with the same template rules as other text. Only real template
    # fields insert components: escaped braces and sender names remain literal.
    render_template(template, values)
    parts, pending = [], []
    for literal, field, _, _ in string.Formatter().parse(template):
        pending.append(literal)
        if field == "reply" and replies:
            parts.append("".join(pending))
            pending.clear()
            parts.extend(replies)
        elif field is not None and field != "reply":
            value = values.get(field, "")
            pending.append("" if value is None else str(value))
    parts.append("".join(pending))

    # Preserve the existing prefix size bound, including inserted label text.
    bounded, remaining = [], 300
    for part in parts:
        if remaining <= 0:
            break
        text = part if isinstance(part, str) else part.get("text", "")
        bounded.append(text[:remaining] if isinstance(part, str) else {**part, "text": text[:remaining]})
        remaining -= len(text)
    return _styled_parts(bounded, modern=modern, color="aqua", links=False)


def _message_colors(route: dict) -> dict[str, str]:
    colors = dict(MESSAGE_COLORS)
    configured = route.get("message_colors", {})
    if isinstance(configured, dict):
        for kind in colors:
            value = str(configured.get(kind, "")).lower()
            if value in COLOR_NAMES.values() or re.fullmatch(r"#[0-9a-f]{6}", value):
                colors[kind] = value
    return colors


def _quote_time(info: dict, data: dict, route: dict) -> str:
    for source, keys in ((info, ("time", "timestamp")), (data, ("timestamp", "time"))):
        for key in keys:
            raw = source.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            try:
                timestamp = float(raw)
                if timestamp <= 0:
                    continue
                if timestamp >= 100_000_000_000:
                    timestamp /= 1000
                return datetime.fromtimestamp(timestamp, ZoneInfo(str(route.get("timezone") or "Asia/Shanghai"))).strftime("%H:%M:%S")
            except (ValueError, TypeError, OverflowError, OSError, KeyError):
                continue
    return "未知"


def component_text(value, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    return component_text(parsed, depth + 1)
            except ValueError:
                pass
        return strip_styles(value)
    if isinstance(value, list):
        return "".join(component_text(item, depth + 1) for item in value[:100])
    value = _as_dict(value)
    if value:
        text = component_text(value.get("text", ""), depth + 1)
        if not text:
            for key in ("translation", "translate", "message", "raw_message"):
                nested = value.get(key)
                if isinstance(nested, (dict, list)):
                    text = component_text(nested, depth + 1)
                    if text:
                        break
        if not text and value.get("translate"):
            text = str(value["translate"]) + " " + " ".join(component_text(item, depth + 1) for item in value.get("with", [])[:10])
        return strip_styles(text) + component_text(value.get("extra", []), depth + 1)
    return ""


def minecraft_component(text: str, *, hover: str = "", url: str = "", modern: bool = False, color: str = "white") -> dict:
    result = {"text": text, "color": color, **{name: False for name in DECORATIONS.values()}}
    if hover:
        hover = hover[:1800]
        contents = {"text": hover}
        if strip_styles(hover) != hover:
            contents = {"text": "", "extra": styled_components(hover, modern=modern, links=False)}
        if modern:
            result["hover_event"] = {"action": "show_text", "value": contents}
        else:
            result["hoverEvent"] = {"action": "show_text", "contents": contents}
    url = _safe_url(url)
    if url:
        result["underlined"] = True
        result["click_event" if modern else "clickEvent"] = {"action": "open_url", "url" if modern else "value": url}
    return result


def _as_dict(value) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    # AstrBot Reply.toDict() intentionally drops everything except id. Prefer the
    # complete pydantic representation, including pydantic.v1 used by AstrBot 4.29.
    for method in ("model_dump", "dict", "toDict"):
        callback = getattr(value, method, None)
        if not callable(callback):
            continue
        try:
            result = callback()
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    fields = getattr(value, "__dict__", None)
    if isinstance(fields, dict):
        return {key: data for key, data in fields.items() if not key.startswith("_")}
    return {}


def _kind(value) -> str:
    name = str(getattr(value, "value", value)).lower()
    return {"plain": "text", "nodes": "forward", "node": "forward", "json": "link", "share": "link", "attachment": "image", "audio": "record", "emoji": "face", "mface": "face", "dice": "face", "rps": "face"}.get(name, name)


def _segment(value) -> dict:
    raw = _as_dict(value)
    kind = _kind(raw.get("type", "text"))
    data = raw.get("data", {key: value for key, value in raw.items() if key != "type"})
    if not isinstance(data, dict):
        data = {"data": data}
    original_kind = str(getattr(raw.get("type"), "value", raw.get("type"))).lower()
    if original_kind in ("mface", "dice", "rps"):
        data = {**data, "_face_kind": original_kind}
    if original_kind == "node":
        data = {"content": [data]}
    return {"type": kind, "data": data}


def _cq_unescape(text: str) -> str:
    return text.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def _message_segments(value) -> list[dict]:
    if isinstance(value, (list, tuple)):
        return [_segment(item) for item in value[:200]]
    if not isinstance(value, str):
        return []
    result, cursor = [], 0
    for match in re.finditer(r"\[CQ:([a-z_]+)((?:,[^\]]*)?)\]", value[:65536], re.I):
        if match.start() > cursor:
            result.append({"type": "text", "data": {"text": _cq_unescape(value[cursor:match.start()])}})
        data = {}
        for field in match[2].lstrip(",").split(","):
            key, separator, content = field.partition("=")
            if separator:
                data[key] = _cq_unescape(content)
        result.append(_segment({"type": match[1], "data": data}))
        cursor = match.end()
    if cursor < len(value):
        result.append({"type": "text", "data": {"text": _cq_unescape(value[cursor:65536])}})
    return result[:200]


def _raw_event(event) -> dict:
    raw = getattr(getattr(event, "message_obj", None), "raw_message", {})
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    return _as_dict(raw)


def _group_id(event) -> str:
    callback = getattr(event, "get_group_id", None)
    group = callback() if callable(callback) else None
    return str(group or getattr(getattr(event, "message_obj", None), "group_id", "") or _raw_event(event).get("group_id") or "")


class Relay:
    def __init__(self, settings, gateway, media):
        self.settings, self.gateway, self.media = settings, gateway, media
        self._seen: dict[str, float] = {}
        self._rates: dict[str, deque] = {}
        self._echoes: dict[tuple[str, str], float] = {}
        self._semaphore = asyncio.Semaphore(8)

    async def close(self):
        self._seen.clear()
        self._rates.clear()
        self._echoes.clear()

    def _once(self, key: str, ttl: int = 120) -> bool:
        now = time.monotonic()
        if len(self._seen) > 4096:
            self._seen = {k: expiry for k, expiry in self._seen.items() if expiry > now}
            if len(self._seen) > 4096:
                self._seen.clear()
        if self._seen.get(key, 0) > now:
            return False
        self._seen[key] = now + ttl
        return True

    def _rate(self, key: str) -> bool:
        now = time.monotonic()
        queue = self._rates.setdefault(key, deque())
        while queue and queue[0] < now - 10:
            queue.popleft()
        if len(queue) >= 15:
            return False
        queue.append(now)
        return True

    @staticmethod
    def _segments(event) -> list:
        raw = _raw_event(event)
        callback = getattr(event, "get_messages", None)
        adapted = _message_segments(callback() if callable(callback) else [])
        message = raw.get("message")
        if message is None:
            # A raw CQ string is a valid AstrBot raw_message representation too.
            candidate = getattr(getattr(event, "message_obj", None), "raw_message", "")
            message = candidate if isinstance(candidate, str) and not candidate.lstrip().startswith("{") else None
        original = _message_segments(message)
        if not original:
            return adapted
        remaining = list(adapted)
        for segment in original:
            kind, data = segment["type"], segment["data"]
            for index, extra in enumerate(remaining):
                if extra["type"] != kind:
                    continue
                if kind == "reply" and str(data.get("id", "")) != str(extra["data"].get("id", "")):
                    continue
                merged = {**extra["data"], **data}
                if kind in LABELS and not _segment_url(merged):
                    # Raw image.file is often an opaque ID while the framework's
                    # corresponding Image.file/url already contains a usable URL.
                    # Keep that URL when raw empty fields/IDs overwrite adaptation.
                    source = _segment_url(data) or _segment_url(extra["data"])
                    if source:
                        merged["url"] = source
                segment["data"] = merged
                remaining.pop(index)
                break
        return original

    @staticmethod
    async def _api(event, action: str, **params) -> dict:
        bot = getattr(event, "bot", None)
        callback = getattr(bot, "call_action", None)
        if not callback:
            callback = getattr(getattr(bot, "api", None), "call_action", None)
        if not callback:
            raise MediaUnavailable("当前平台未提供 OneBot 媒体查询")
        self_id = _raw_event(event).get("self_id") or (event.get_self_id() if hasattr(event, "get_self_id") else None)
        if self_id:
            params.setdefault("self_id", self_id)
        result = await asyncio.wait_for(callback(action=action, **params), timeout=8)
        if not isinstance(result, dict):
            raise MediaRejected("OneBot 返回格式异常")
        if "retcode" in result and result["retcode"] != 0:
            raise MediaUnavailable("OneBot 无法获取消息内容")
        return result.get("data", result) if isinstance(result.get("data", result), dict) else result

    async def from_qq(self, event, route: dict) -> None:
        if not self.settings.data.get("enabled", True) or not route.get("enabled", True) or not route.get("qq_to_mc", True):
            return
        user = str(event.get_sender_id())
        own = str(event.get_self_id()) if hasattr(event, "get_self_id") else ""
        if user == own:
            return
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        umo = str(event.unified_msg_origin)
        if message_id and not self._once(f"qq:{umo}:{message_id}"):
            return
        if not self._rate(umo):
            return
        server = self.settings.server(route["server_id"])
        modern = server.get("component_version") == "modern"
        sender = str(event.get_sender_name()) if hasattr(event, "get_sender_name") else user
        group = str(route.get("display_name") or route.get("name") or "QQ群")
        prefix = route.get("prefix", "[{group}] {sender}{reply}：")
        move_replies = "reply" in template_fields(prefix)
        prefix_values = {"group": group, "sender": sender, "user_id": user}
        async with self._semaphore:
            segments = self._segments(event)
            replies = []
            components = await self._render_segments(event, segments, route, modern, 0, [0],
                                                     reply_components=replies if move_replies else None)
            if not components and not replies:
                return
            bounded, remaining = [], 12000
            for component in components:
                text = component.get("text", "")
                if len(text) > remaining:
                    bounded.append(minecraft_component(text[:remaining] + "[消息已截断]", color="gray"))
                    break
                bounded.append(component)
                remaining -= len(text)
            components = bounded
            components[:0] = _prefix_components(prefix, prefix_values, replies, modern=modern)
            if not components:
                return
            rendered = "".join(item["text"] for item in components)
            digest = hashlib.sha256(rendered.encode()).hexdigest()
            self._echoes[(route["server_id"], digest)] = time.monotonic() + 30
            self._echoes = {k: expiry for k, expiry in self._echoes.items() if expiry > time.monotonic()}
            await self.gateway.broadcast(route["server_id"], components)

    async def _render_segments(self, event, segments: list, route: dict, modern: bool, depth: int, budget: list[int],
                               *, reply_components: list[dict] | None = None) -> list[dict]:
        if depth > 3:
            return [minecraft_component("[嵌套消息已截断]", color="gray")]
        enabled = set(route.get("types", ["text", "link", "image", "record", "video", "file", "reply", "forward", "at", "face"]))
        prepared, limit = {}, asyncio.Semaphore(4)

        async def resolve(kind, data):
            async with limit:
                if kind == "reply":
                    return await self._quote(event, data, route)
                return await self._media_source(event, kind, data)

        # Only independent metadata is concurrent. Rendering, nested budgets,
        # cache publication and the final message remain in the original order.
        for index, raw in enumerate(segments[:min(100, max(0, 200 - budget[0]))]):
            segment = _segment(raw)
            kind = segment["type"]
            if kind not in enabled:
                continue
            if kind == "forward":
                break
            if kind in LABELS or kind == "reply":
                prepared[index] = asyncio.create_task(resolve(kind, segment["data"]))
        try:
            return await self._render_segments_ordered(event, segments, route, modern, depth, budget,
                                                       reply_components=reply_components, prepared=prepared)
        finally:
            for task in prepared.values():
                if not task.done():
                    task.cancel()
            if prepared:
                await asyncio.gather(*prepared.values(), return_exceptions=True)

    async def _render_segments_ordered(self, event, segments: list, route: dict, modern: bool, depth: int, budget: list[int],
                                       *, reply_components: list[dict] | None = None, prepared: dict | None = None) -> list[dict]:
        if depth > 3:
            return [minecraft_component("[嵌套消息已截断]", color="gray")]
        result = []
        colors = _message_colors(route)
        enabled = set(route.get("types", ["text", "link", "image", "record", "video", "file", "reply", "forward", "at", "face"]))
        for index, raw in enumerate(segments[:100]):
            if budget[0] >= 200:
                result.append(minecraft_component("[消息过长，已截断]", color="gray"))
                break
            budget[0] += 1
            raw = _segment(raw)
            kind = raw["type"]
            if kind not in enabled:
                continue
            data = raw["data"]
            if kind == "text":
                text = str(data.get("text", ""))[:4000]
                result.extend(styled_components(text, modern=modern, links="link" in enabled, link_color=colors["link"]))
            elif kind == "at":
                target = str(data.get("qq", data.get("id", "")))
                result.append(minecraft_component("@全体成员" if target == "all" else "@" + str(data.get("name") or target), color=colors["at"]))
            elif kind == "face":
                face_kind = data.get("_face_kind", "face")
                label, url = "表情", ""
                if face_kind in ("dice", "rps"):
                    label = "骰子" if face_kind == "dice" else "猜拳"
                    value = str(data.get("result") or data.get("value") or "")
                    if value:
                        label += "：" + value[:20]
                elif face_kind == "mface" and _safe_url(data.get("url", "")):
                    direct = getattr(self.media, "public_source_url", None)
                    if callable(direct):
                        try:
                            url = direct(_safe_url(data["url"]))
                        except (MediaRejected, ValueError):
                            pass
                hover = str(data.get("name") or data.get("summary") or "QQ 表情 " + str(data.get("id", "")))
                result.append(minecraft_component("[" + label + "]", hover=hover, url=url, modern=modern, color=colors["face"]))
            elif kind in LABELS:
                label = LABELS[kind]
                name = str(data.get("name") or data.get("file_name") or "")
                if kind == "file" and name:
                    label += "：" + name[:100]
                try:
                    source = await prepared[index] if prepared and index in prepared else await self._media_source(event, kind, data)
                    direct = getattr(self.media, "public_source_url", None)
                    if callable(direct) and _safe_url(source):
                        url = direct(source)
                    else:
                        url = await self.media.publish(source, kind, name)
                    result.append(minecraft_component(f"[{label}]", hover=name or "点击访问，有效期内可用", url=url, modern=modern, color=colors[kind]))
                except Exception as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    result.append(minecraft_component(f"[{label}·暂不可用]", hover="媒体链接未配置、已失效或无法读取", modern=modern, color=colors[kind]))
                    log.debug("[YumaMCQQ] media unavailable: %s", type(exc).__name__)
            elif kind == "reply":
                author, text = await prepared[index] if prepared and index in prepared else await self._quote(event, data, route)
                template = route.get("templates", {}).get("reply_label") or REPLY_LABEL
                if not template.strip():
                    template = REPLY_LABEL
                # Labels are plain text with their configured semantic colour.
                # Interpolate once, then remove legacy formatting even if it was
                # formed across the template/author boundary.
                label = FORMAT.sub("", render_template(template, {"sender": author}))
                target = result if reply_components is None else reply_components
                target.append(minecraft_component(label, hover=text or "引用内容暂不可用", modern=modern, color=colors["reply"]))
            elif kind == "forward":
                records = await self._forward(event, data, route, modern, depth, budget)
                preview = "\n".join(f"{node['author']}：{node['text']}" for node in records[:5])[:1600]
                try:
                    url = await self.media.publish_record(records)
                except Exception:
                    url = ""
                label = f"[合并消息·{len(records)}条]" if records else "[合并消息·暂不可用]"
                result.append(minecraft_component(label, hover=preview or "合并消息暂不可用", url=url, modern=modern, color=colors["forward"]))
            elif kind == "link":
                url, title = self._card(data)
                result.append(minecraft_component("[链接]" if not title else "[" + title[:80] + "]", hover=url or "链接卡片暂无法展开", url=url, modern=modern, color=colors["link"]))
        return result

    async def _media_source(self, event, kind: str, data: dict) -> str:
        provided = _segment_url(data)
        if provided:
            return provided
        source = str(data.get("url") or data.get("path") or "")
        file_id = str(data.get("file") or data.get("file_id") or "")
        if not source and file_id.startswith("base64://"):
            return file_id
        if kind == "record" and file_id:
            try:
                info = await self._api(event, "get_record", file=file_id, out_format="mp3")
                source = self._source_from_api(info, source)
            except Exception:
                if not source:
                    raise
        elif kind == "file" and not source and file_id:
            group = _group_id(event)
            try:
                info = await self._api(event, "get_group_file_url" if group else "get_private_file_url", file_id=file_id, **({"group_id": int(group)} if group else {}))
                source = self._source_from_api(info)
            except Exception:
                pass
            if not source:
                info = await self._api(event, "get_file", file_id=file_id)
                source = self._source_from_api(info)
        elif kind == "image" and not source and file_id:
            info = await self._api(event, "get_image", file=file_id)
            source = self._source_from_api(info)
        elif kind == "video" and not source and file_id:
            info = await self._api(event, "get_file", file_id=file_id)
            source = self._source_from_api(info)
        if not source and file_id.startswith(("http://", "https://", "file://", "base64://")):
            source = file_id
        if not source:
            raise MediaUnavailable("媒体来源不可用")
        return source

    @staticmethod
    def _source_from_api(info: dict, fallback: str = "") -> str:
        values = [str(info.get(key) or "") for key in ("url", "file", "file_path", "path")]
        for value in values:
            if _safe_url(value):
                return _safe_url(value)
        if info.get("base64"):
            return "base64://" + str(info["base64"])
        return next((value for value in values if value), fallback)

    async def _quote(self, event, data: dict, route: dict | None = None) -> tuple[str, str]:
        route = route or {}
        author, text, quoted_time = "某人", "引用内容暂不可用", "未知"
        try:
            info = await self._api(event, "get_msg", message_id=int(data["id"]))
            group = _group_id(event)
            if group:
                if group != str(info.get("group_id", "")):
                    raise MediaRejected("引用消息不属于当前群聊")
            elif info.get("group_id") or str(info.get("user_id", "")) != str(event.get_sender_id()):
                raise MediaRejected("引用消息不属于当前私聊")
            sender = _as_dict(info.get("sender", {}))
            author = str(sender.get("card") or sender.get("nickname") or data.get("sender_nickname") or sender.get("user_id") or "某人")
            author = " ".join(FORMAT.sub("", author).split()) or "某人"
            quoted = info.get("message") or info.get("raw_message") or data.get("chain") or data.get("message_str") or ""
            text = self._summary(quoted, preserve_styles=True)[:1600] or "引用内容暂不可用"
            quoted_time = _quote_time(info, data, route)
        except Exception:
            pass
        template = route.get("templates", {}).get("reply_hover") or REPLY_HOVER
        return author, render_template(template, {"sender": author, "message": text, "time": quoted_time})

    async def _forward(self, event, data: dict, route: dict, modern: bool, depth: int, budget: list[int]) -> list[dict]:
        content = data.get("content") or data.get("nodes")
        if not isinstance(content, list):
            try:
                info = await self._api(event, "get_forward_msg", message_id=str(data.get("id", "")))
                content = info.get("messages") or info.get("content") or []
            except Exception:
                return []
        records = []
        limit = min(100, max(1, int(route.get("max_forward_nodes", 30))))
        for node in content[:limit]:
            if not isinstance(node, dict):
                continue
            node = _as_dict(node.get("data", node))
            sender = _as_dict(node.get("sender", {}))
            author = str(node.get("name") or sender.get("card") or sender.get("nickname") or node.get("uin") or "未知发送者")
            segments = node.get("content") or node.get("message") or []
            if isinstance(segments, str):
                segments = [{"type": "text", "data": {"text": segments}}]
            elif not isinstance(segments, list):
                segments = []
            rendered = await self._render_segments(event, segments, route, modern, depth + 1, budget)
            text = "".join(part.get("text", "") for part in rendered)[:4000]
            links = []
            for part in rendered:
                click = part.get("clickEvent") or part.get("click_event") or {}
                link = click.get("url") or click.get("value")
                if link:
                    links.append({"label": part.get("text", "附件"), "url": link})
            records.append({"author": author, "text": text, "links": links})
        return records

    @staticmethod
    def _summary(segments, *, preserve_styles: bool = False) -> str:
        result = []
        for raw in _message_segments(segments)[:50]:
            kind, data = raw["type"], raw["data"]
            if kind == "text":
                value = str(data.get("text", ""))
                result.append(value if preserve_styles else strip_styles(value))
            else:
                result.append("[" + LABELS.get(kind, {"reply": "引用", "forward": "合并消息", "at": "提及", "face": "表情", "link": "链接"}.get(kind, "消息")) + "]")
        return "".join(result)[:1600]

    @staticmethod
    def _card(data: dict) -> tuple[str, str]:
        raw = data.get("data", data)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return "", ""
        def search(value, depth=0):
            if depth > 5 or not isinstance(value, dict):
                return "", ""
            for key in ("url", "jumpUrl", "jump_url", "qqdocurl", "doc_url"):
                candidate = _safe_url(str(value.get(key, "")))
                if candidate:
                    return candidate, str(value.get("title") or value.get("desc") or "")
            for nested in list(value.values())[:30]:
                candidate = search(nested, depth + 1)
                if candidate[0]:
                    return candidate
            return "", ""
        return search(raw)

    async def from_game(self, server_id: str, payload: dict) -> list[tuple[str, str]]:
        if not self.settings.data.get("enabled", True):
            return []
        event_name = str(payload.get("event_name") or EVENTS.get(str(payload.get("sub_type", "")), ""))
        if event_name == "PlayerCommandEvent":
            return []
        # Notice events may carry null/empty IDs. They are not a shared event ID.
        message_id = str(payload.get("message_id") or "")
        if message_id and not self._once(f"mc:{server_id}:{event_name}:{message_id}"):
            return []
        player = _as_dict(payload.get("player"))
        name = strip_styles(str(player.get("nickname") or player.get("name") or "玩家"))
        text = component_text(payload.get("message") or payload.get("raw_message") or "")
        if text.startswith(("{", "[")):
            try:
                text = component_text(json.loads(text))
            except ValueError:
                pass
        digest = hashlib.sha256(text.encode()).hexdigest()
        if self._echoes.get((server_id, digest), 0) > time.monotonic():
            return []
        if event_name == "PlayerJoinEvent":
            text = name + " 加入了游戏"
        elif event_name == "PlayerQuitEvent":
            text = name + " 离开了游戏"
        elif event_name == "PlayerDeathEvent":
            death = payload.get("death") or {}
            text = component_text(death) or component_text(payload.get("death_message")) or component_text(payload.get("message")) or name + " 死亡了"
        elif event_name == "PlayerAchievementEvent":
            achievement = _as_dict(payload.get("achievement"))
            translated = achievement.get("translation") or achievement.get("translate") or achievement.get("text")
            display = _as_dict(achievement.get("display"))
            text = component_text(translated) or name + " 获得成就：" + component_text(display.get("title") or achievement.get("title") or achievement.get("key") or "新成就")
        elif event_name == "PlayerChatEvent":
            text = name + "：" + text
        else:
            return []
        server = self.settings.server(server_id)
        output = []
        for route in self.settings.data.get("routes", []):
            if route.get("server_id") != server_id or not route.get("enabled", True) or not route.get("mc_to_qq", True):
                continue
            if event_name not in route.get("events", list(EVENTS.values())):
                log.debug("[YumaMCQQ] route event disabled: server=%s event=%s", server_id, event_name)
                continue
            if not self._rate("mc:" + route["umo"] + ":" + event_name):
                continue
            template = route.get("templates", {}).get("mc_to_qq", "[{server}] {message}")
            shown = server.get("display_name") or server.get("name", server_id)
            output.append((route["umo"], strip_styles(render_template(template, {"server": shown, "server_name": server.get("name", server_id), "server_id": server_id, "player": name, "message": text[:4000]}))))
        return output
