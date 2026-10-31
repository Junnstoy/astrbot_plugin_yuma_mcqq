"""Structured OneBot ↔ vanilla Minecraft rendering without framework imports."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import deque
from urllib.parse import urlsplit

from ..core.text import render_template
from .media import MediaRejected, MediaUnavailable

log = logging.getLogger(__name__)
URL = re.compile(r"https?://[^\s<>\[\]]+", re.I)
COLORS = re.compile(r"§[0-9a-fk-orx]", re.I)
LABELS = {"image": "图片", "record": "语音", "video": "视频", "file": "文件"}


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return ""


def component_text(value, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, str):
        return COLORS.sub("", value)
    if isinstance(value, list):
        return "".join(component_text(item, depth + 1) for item in value[:100])
    if isinstance(value, dict):
        text = str(value.get("text", ""))
        if not text and value.get("translate"):
            text = str(value["translate"]) + " " + " ".join(component_text(item, depth + 1) for item in value.get("with", [])[:10])
        return COLORS.sub("", text) + component_text(value.get("extra", []), depth + 1)
    return ""


def minecraft_component(text: str, *, hover: str = "", url: str = "", modern: bool = False, color: str = "white") -> dict:
    result = {"text": text, "color": color}
    if hover:
        if modern:
            result["hover_event"] = {"action": "show_text", "value": {"text": hover[:1800]}}
        else:
            result["hoverEvent"] = {"action": "show_text", "contents": {"text": hover[:1800]}}
    if _safe_url(url):
        result["underlined"] = True
        result["click_event" if modern else "clickEvent"] = {"action": "open_url", "url" if modern else "value": url}
    return result


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "toDict"):
        try:
            result = value.toDict()
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {}


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
        raw = getattr(getattr(event, "message_obj", None), "raw_message", {})
        raw = _as_dict(raw)
        message = raw.get("message")
        if isinstance(message, list):
            return message[:200]
        return [_as_dict(item) for item in event.get_messages()][:200]

    @staticmethod
    async def _api(event, action: str, **params) -> dict:
        bot = getattr(event, "bot", None)
        callback = getattr(bot, "call_action", None)
        if not callback:
            callback = getattr(getattr(bot, "api", None), "call_action", None)
        if not callback:
            raise MediaUnavailable("当前平台未提供 OneBot 媒体查询")
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
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", ""))
        umo = str(event.unified_msg_origin)
        if message_id and not self._once(f"qq:{umo}:{message_id}"):
            return
        if not self._rate(umo):
            return
        server = self.settings.server(route["server_id"])
        modern = server.get("component_version") == "modern"
        sender = str(event.get_sender_name()) if hasattr(event, "get_sender_name") else user
        group = str(route.get("display_name") or route.get("name") or "QQ群")
        prefix = render_template(route.get("prefix", "[{group}] {sender}："), {"group": group, "sender": sender, "user_id": user})
        async with self._semaphore:
            segments = self._segments(event)
            components = await self._render_segments(event, segments, route, modern, 0, [0])
            if not components:
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
            components.insert(0, minecraft_component(prefix[:300], color="aqua"))
            rendered = "".join(item["text"] for item in components)
            digest = hashlib.sha256(rendered.encode()).hexdigest()
            self._echoes[(route["server_id"], digest)] = time.monotonic() + 30
            self._echoes = {k: expiry for k, expiry in self._echoes.items() if expiry > time.monotonic()}
            await self.gateway.broadcast(route["server_id"], components)

    async def _render_segments(self, event, segments: list, route: dict, modern: bool, depth: int, budget: list[int]) -> list[dict]:
        if depth > 3:
            return [minecraft_component("[嵌套消息已截断]", color="gray")]
        result = []
        enabled = set(route.get("types", ["text", "link", "image", "record", "video", "file", "reply", "forward", "at"]))
        for raw in segments[:100]:
            if budget[0] >= 200:
                result.append(minecraft_component("[消息过长，已截断]", color="gray"))
                break
            budget[0] += 1
            raw = _as_dict(raw)
            original_kind = str(raw.get("type", "text")).lower()
            kind = original_kind
            kind = {"plain": "text", "nodes": "forward", "node": "forward", "json": "link", "share": "link"}.get(kind, kind)
            if kind not in enabled:
                continue
            data = raw.get("data", raw)
            if not isinstance(data, dict):
                data = {"data": data}
            if original_kind == "node":
                data = {"content": [data]}
            if kind == "text":
                text = str(data.get("text", ""))[:4000]
                cursor = 0
                for match in URL.finditer(text):
                    if match.start() > cursor:
                        result.append(minecraft_component(text[cursor:match.start()]))
                    url = match.group().rstrip("。，！；,;)")
                    result.append(minecraft_component("[链接]", hover=url, url=url, modern=modern, color="blue"))
                    cursor = match.start() + len(url)
                if cursor < len(text):
                    result.append(minecraft_component(text[cursor:]))
            elif kind == "at":
                target = str(data.get("qq", data.get("id", "")))
                result.append(minecraft_component("@全体成员" if target == "all" else "@" + str(data.get("name") or target), color="yellow"))
            elif kind in LABELS:
                label = LABELS[kind]
                name = str(data.get("name") or data.get("file_name") or "")
                if kind == "file" and name:
                    label += "：" + name[:100]
                try:
                    source = await self._media_source(event, kind, data)
                    url = await self.media.publish(source, kind, name)
                    result.append(minecraft_component(f"[{label}]", hover=name or "点击访问，有效期内可用", url=url, modern=modern, color="blue"))
                except Exception as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    result.append(minecraft_component(f"[{label}·暂不可用]", hover="媒体链接未配置、已失效或无法读取", modern=modern, color="gray"))
                    log.debug("[YumaMCQQ] media unavailable: %s", type(exc).__name__)
            elif kind == "reply":
                author, text = await self._quote(event, data)
                result.append(minecraft_component(f"[回复 {author}] ", hover=text or "引用内容暂不可用", modern=modern, color="gray"))
            elif kind == "forward":
                records = await self._forward(event, data, route, modern, depth, budget)
                preview = "\n".join(f"{node['author']}：{node['text']}" for node in records[:5])[:1600]
                try:
                    url = await self.media.publish_record(records)
                except Exception:
                    url = ""
                label = f"[合并消息·{len(records)}条]" if records else "[合并消息·暂不可用]"
                result.append(minecraft_component(label, hover=preview or "合并消息暂不可用", url=url, modern=modern, color="blue"))
            elif kind == "link":
                url, title = self._card(data)
                result.append(minecraft_component("[链接]" if not title else "[" + title[:80] + "]", hover=url or "链接卡片暂无法展开", url=url, modern=modern, color="blue"))
        return result

    async def _media_source(self, event, kind: str, data: dict) -> str:
        source = str(data.get("url") or data.get("path") or "")
        file_id = str(data.get("file") or data.get("file_id") or "")
        if kind == "record" and file_id:
            try:
                info = await self._api(event, "get_record", file=file_id, out_format="mp3")
                source = str(info.get("url") or info.get("file") or info.get("path") or source)
            except Exception:
                if not source:
                    raise
        elif kind == "file" and not source and file_id:
            info = await self._api(event, "get_file", file_id=file_id)
            source = str(info.get("url") or info.get("file") or info.get("file_path") or "")
        elif kind == "image" and not source and file_id:
            info = await self._api(event, "get_image", file=file_id)
            source = str(info.get("url") or info.get("file") or "")
        if not source and file_id.startswith(("http://", "https://", "file://")):
            source = file_id
        if not source:
            raise MediaUnavailable("媒体来源不可用")
        return source

    async def _quote(self, event, data: dict) -> tuple[str, str]:
        if data.get("chain") or data.get("message_str"):
            return str(data.get("sender_nickname") or data.get("sender_id") or "某人"), str(data.get("message_str") or self._summary(data["chain"]))[:1600]
        try:
            info = await self._api(event, "get_msg", message_id=int(data["id"]))
            raw = _as_dict(getattr(getattr(event, "message_obj", None), "raw_message", {}))
            if raw.get("group_id"):
                if str(raw["group_id"]) != str(info.get("group_id", "")):
                    raise MediaRejected("引用消息不属于当前群聊")
            elif info.get("group_id") or str(info.get("user_id", "")) != str(event.get_sender_id()):
                raise MediaRejected("引用消息不属于当前私聊")
            sender = _as_dict(info.get("sender", {}))
            author = str(sender.get("card") or sender.get("nickname") or sender.get("user_id") or "某人")
            return author, self._summary(info.get("message", []))[:1600]
        except Exception:
            return "某人", "引用内容暂不可用"

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
    def _summary(segments) -> str:
        if isinstance(segments, str):
            return segments[:1600]
        result = []
        for raw in list(segments or [])[:50]:
            raw = _as_dict(raw)
            kind = str(raw.get("type", "text")).lower()
            data = _as_dict(raw.get("data", raw))
            if kind in ("text", "plain"):
                result.append(str(data.get("text", "")))
            else:
                result.append("[" + LABELS.get(kind, {"reply": "引用", "forward": "合并消息", "at": "提及"}.get(kind, "消息")) + "]")
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
        event_name = str(payload.get("event_name", ""))
        if event_name == "PlayerCommandEvent":
            return []
        message_id = str(payload.get("message_id", ""))
        if message_id and not self._once(f"mc:{server_id}:{message_id}"):
            return []
        player = _as_dict(payload.get("player"))
        name = str(player.get("nickname") or player.get("name") or "玩家")
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
            text = component_text(death) or component_text(payload.get("message")) or name + " 死亡了"
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
            if event_name not in route.get("events", ["PlayerChatEvent"]):
                continue
            if not self._rate("mc:" + route["umo"]):
                continue
            template = route.get("templates", {}).get("mc_to_qq", "[{server}] {message}")
            output.append((route["umo"], render_template(template, {"server": server.get("name", server_id), "player": name, "message": text[:4000]})))
        return output
