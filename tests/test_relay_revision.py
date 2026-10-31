"""Regression coverage for real AstrBot/OneBot shapes and vanilla MC rich text."""
from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "yuma_relay_revision"
if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
relay_module = importlib.import_module(PACKAGE + ".services.relay")
media_module = importlib.import_module(PACKAGE + ".services.media")


class Kind(str, Enum):
    Reply = "Reply"
    Plain = "Plain"
    Face = "Face"


@dataclass
class RichReply:
    id: str
    chain: list = field(default_factory=list)
    sender_nickname: str = ""
    type: Kind = Kind.Reply
    timestamp: float = 0

    def dict(self):
        return asdict(self)

    def toDict(self):
        # The real AstrBot 4.29 Reply deliberately serializes only the OneBot ID.
        return {"type": "reply", "data": {"id": self.id}}


class Event:
    unified_msg_origin = "napcat:GroupMessage:77"

    def __init__(self, raw, adapted=(), group="77", api=None):
        self.message_obj = types.SimpleNamespace(raw_message=raw, message_id="incoming", group_id=group)
        self.adapted = list(adapted)
        self.bot = types.SimpleNamespace(call_action=AsyncMock(side_effect=api))

    def get_messages(self): return self.adapted
    def get_group_id(self): return self.message_obj.group_id
    def get_sender_id(self): return "123"
    def get_sender_name(self): return "Alice"
    def get_self_id(self): return "999"


class RelayRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.route = {"umo": "napcat:GroupMessage:77", "server_id": "paper", "display_name": "幻域群"}
        self.server = {"id": "paper", "name": "ProtocolServer", "display_name": "幻域生存服", "component_version": "legacy"}
        data = {"servers": [self.server], "routes": [self.route], "media": {"enabled": False}}
        self.settings = types.SimpleNamespace(data=data, server=lambda _: self.server)
        self.gateway = types.SimpleNamespace(broadcast=AsyncMock())
        self.media = media_module.MediaService(self.temp.name, self.settings)
        self.relay = relay_module.Relay(self.settings, self.gateway, self.media)

    async def asyncTearDown(self):
        await self.media.close()
        self.temp.cleanup()

    async def test_cq_raw_and_rich_pydantic_reply_keep_context_and_hover(self):
        reply = RichReply(id="20", chain=[{"type": Kind.Plain, "text": "原文"}, {"type": Kind.Face, "id": 14}, {"type": "Image", "url": "https://image.example/a"}], sender_nickname="Bob")
        async def api(action, **params):
            self.assertEqual(action, "get_msg")
            self.assertEqual(str(params["self_id"]), "999")
            return {"group_id": 77, "sender": {"nickname": "Bob"}, "message": []}
        event = Event("[CQ:reply,id=20]回答", [reply, {"type": "text", "text": "回答"}], api=api)
        parsed = self.relay._segments(event)
        self.assertEqual(parsed[0]["data"]["sender_nickname"], "Bob")
        self.assertEqual(len(parsed[0]["data"]["chain"]), 3)
        await self.relay.from_qq(event, self.route)
        output = self.gateway.broadcast.await_args.args[1]
        quote = next(c for c in output if "回复 Bob" in c["text"])
        self.assertEqual(quote["hoverEvent"]["contents"]["text"], "Bob\n原文[表情][图片]\nDate: 未知")
        self.assertIn("幻域群", "".join(c["text"] for c in output))

    async def test_object_raw_message_and_cq_escaped_quote(self):
        raw = types.SimpleNamespace(group_id=77, message=[{"type": "reply", "data": {"id": "21"}}, {"type": "text", "data": {"text": "hello"}}])
        async def api(action, **params):
            return {"group_id": 77, "sender": {"card": "CardName"}, "message": "a&amp;b[CQ:image,url=https://x.example/a&#44;b]"}
        await self.relay.from_qq(Event(raw, api=api), self.route)
        quote = next(c for c in self.gateway.broadcast.await_args.args[1] if "回复 CardName" in c["text"])
        # &b is a valid style token outside a URL, but CQ entities are decoded first.
        self.assertEqual(relay_module.component_text(quote["hoverEvent"]["contents"]), "CardName\na[图片]\nDate: 未知")

    async def test_quote_hover_preserves_color_style_hex_and_url_query(self):
        async def api(action, **params):
            return {"group_id": 77, "sender": {"nickname": "Bob"}, "message": [{"type": "text", "data": {"text": "&a绿色 &l粗体 §r复位 &#12abef彩色 https://example.com?a=1&b=2"}}]}
        await self.relay.from_qq(Event({"message": [{"type": "reply", "data": {"id": "20"}}]}, api=api), self.route)
        quote = next(c for c in self.gateway.broadcast.await_args.args[1] if "回复 Bob" in c["text"])
        extra = quote["hoverEvent"]["contents"]["extra"]
        by_text = {part["text"].strip(): part for part in extra}
        self.assertEqual(by_text["绿色"]["color"], "green")
        self.assertTrue(by_text["粗体"]["bold"])
        self.assertFalse(by_text["复位"]["bold"])
        self.assertEqual(by_text["彩色"]["color"], "#12abef")
        self.assertIn("https://example.com?a=1&b=2", by_text)
        self.assertFalse(any("clickEvent" in item for item in extra))

    async def test_reply_cannot_use_enriched_chain_to_bypass_conversation_check(self):
        async def api(action, **params):
            return {"message_type": "private", "user_id": "123", "sender": {"nickname": "Bob"}, "message": "private reward"}
        event = Event({}, api=api)
        author, text = await self.relay._quote(event, {"id": "22", "chain": [{"type": "text", "data": {"text": "private reward"}}]})
        self.assertEqual(author, "某人")
        self.assertNotIn("private reward", text)
        event.bot.call_action = AsyncMock(return_value={"group_id": 88, "message": "another group secret"})
        self.assertNotIn("secret", (await self.relay._quote(event, {"id": "23"}))[1])

    async def test_reply_prefix_slot_moves_hover_component_for_both_mc_formats(self):
        self.route["prefix"] = "&e[{group}]&e{sender}{reply}&8:"
        async def api(action, **params):
            return {"group_id": 77, "sender": {"nickname": "Bob"}, "message": "原文"}
        for modern in (False, True):
            with self.subTest(modern=modern):
                self.server["component_version"] = "modern" if modern else "legacy"
                event = Event({"message": [{"type": "text", "data": {"text": "前"}},
                                            {"type": "reply", "data": {"id": "20"}},
                                            {"type": "text", "data": {"text": "后"}}]}, api=api)
                event.message_obj.message_id = str(modern)
                await self.relay.from_qq(event, self.route)
                output = self.gateway.broadcast.await_args.args[1]
                self.assertEqual("".join(c["text"] for c in output), "[幻域群]Alice[回复 Bob]:前后")
                quote = next(c for c in output if "回复 Bob" in c["text"])
                hover_key, value_key = ("hover_event", "value") if modern else ("hoverEvent", "contents")
                self.assertEqual(quote[hover_key][value_key]["text"], "Bob\n原文\nDate: 未知")
                self.assertEqual(quote["color"], "gray")
                self.assertEqual(next(c for c in output if c["text"] == ":")["color"], "dark_gray")
                event.bot.call_action.assert_awaited_once()

    async def test_repeated_reply_slots_keep_formatting_state_and_fetch_once(self):
        self.route["prefix"] = "&e&l前{reply}后&r复位§8冒号§a{reply}尾"
        async def api(action, **params):
            return {"group_id": 77, "sender": {"user_id": 456}, "message": "§a绿色原文"}
        event = Event({"message": [{"type": "reply", "data": {"id": "20"}},
                                    {"type": "text", "data": {"text": "正文"}}]}, api=api)
        await self.relay.from_qq(event, self.route)
        output = self.gateway.broadcast.await_args.args[1]
        by_text = {c["text"]: c for c in output}
        self.assertEqual("".join(c["text"] for c in output), "前[回复 456]后复位冒号[回复 456]尾正文")
        for text in ("前", "后"):
            self.assertEqual(by_text[text]["color"], "yellow")
            self.assertTrue(by_text[text]["bold"])
        self.assertEqual(by_text["复位"]["color"], "aqua")
        self.assertFalse(by_text["复位"]["bold"])
        self.assertEqual(by_text["冒号"]["color"], "dark_gray")
        self.assertEqual(by_text["尾"]["color"], "green")
        self.assertEqual(by_text["正文"]["color"], "white")
        quotes = [c for c in output if "回复 456" in c["text"]]
        self.assertEqual(len(quotes), 2)
        self.assertEqual(quotes[0], quotes[1])
        self.assertIsNot(quotes[0]["hoverEvent"], quotes[1]["hoverEvent"])
        self.assertEqual(next(c for c in quotes[0]["hoverEvent"]["contents"]["extra"] if "绿色原文" in c["text"])["color"], "green")
        self.assertFalse(quotes[0]["bold"])
        event.bot.call_action.assert_awaited_once()

    async def test_empty_or_disabled_reply_slot_is_empty_without_lookup(self):
        self.route["prefix"] = "&e{sender}{reply}&8:"
        for with_reply, types_enabled in ((False, ["text", "reply"]), (True, ["text"])):
            with self.subTest(with_reply=with_reply):
                self.route["types"] = types_enabled
                segments = [{"type": "text", "data": {"text": "正文"}}]
                if with_reply:
                    segments.insert(0, {"type": "reply", "data": {"id": "20"}})
                event = Event({"message": segments})
                event.message_obj.message_id = str(with_reply)
                await self.relay.from_qq(event, self.route)
                output = self.gateway.broadcast.await_args.args[1]
                self.assertEqual("".join(c["text"] for c in output), "Alice:正文")
                self.assertFalse(any("hoverEvent" in c for c in output))
                event.bot.call_action.assert_not_awaited()
        self.gateway.broadcast.reset_mock()
        event = Event({"message": [{"type": "reply", "data": {"id": "20"}}]})
        await self.relay.from_qq(event, self.route)
        self.gateway.broadcast.assert_not_awaited()
        event.bot.call_action.assert_not_awaited()

    async def test_legacy_and_escaped_reply_prefix_keep_original_body_position(self):
        async def api(action, **params):
            return {"group_id": 77, "sender": {"nickname": "Bob"}, "message": "原文"}
        for prefix, expected in (("{sender}:", "Alice:"), ("{sender}{{reply}}:", "Alice{reply}:")):
            with self.subTest(prefix=prefix):
                self.route["prefix"] = prefix
                event = Event({"message": [{"type": "text", "data": {"text": "前"}},
                                            {"type": "reply", "data": {"id": "20"}},
                                            {"type": "text", "data": {"text": "后"}}]}, api=api)
                event.message_obj.message_id = prefix
                await self.relay.from_qq(event, self.route)
                output = self.gateway.broadcast.await_args.args[1]
                self.assertEqual("".join(c["text"] for c in output), expected + "前[回复 Bob]后")

    async def test_placeholder_in_sender_or_group_does_not_insert_extra_replies(self):
        self.route.update(prefix="[{group}]{sender}{reply}:", display_name="{reply}")
        async def api(action, **params):
            return {"group_id": 77, "sender": {"nickname": "Bob"}, "message": "原文"}
        event = Event({"message": [{"type": "reply", "data": {"id": "20"}}]}, api=api)
        event.get_sender_name = lambda: "{reply}"
        await self.relay.from_qq(event, self.route)
        output = self.gateway.broadcast.await_args.args[1]
        self.assertEqual("".join(c["text"] for c in output), "[{reply}]{reply}[回复 Bob]:")
        self.assertEqual(sum("hoverEvent" in c for c in output), 1)

    async def test_reply_slot_keeps_cross_group_privacy_check(self):
        self.route["prefix"] = "{sender}{reply}:"
        async def api(action, **params):
            return {"group_id": 88, "sender": {"nickname": "SecretAuthor"}, "message": "secret"}
        reply = RichReply(id="20", sender_nickname="SecretAuthor", chain=[{"type": "Plain", "text": "secret"}])
        event = Event("[CQ:reply,id=20]正文", [reply, {"type": "text", "text": "正文"}], api=api)
        await self.relay.from_qq(event, self.route)
        output = self.gateway.broadcast.await_args.args[1]
        self.assertEqual("".join(c["text"] for c in output), "Alice[回复 某人]:正文")
        self.assertNotIn("secret", json.dumps(output).lower())
        self.assertIn("引用内容暂不可用", str(output))

    async def test_multiple_references_preserve_order_in_each_slot_not_in_body(self):
        self.route["prefix"] = "先{reply}中{reply}后:"
        async def api(action, **params):
            return {"group_id": 77, "sender": {"nickname": "Bob" + str(params["message_id"])}, "message": "原文"}
        event = Event({"message": [{"type": "text", "data": {"text": "A"}},
                                    {"type": "reply", "data": {"id": "20"}},
                                    {"type": "text", "data": {"text": "B"}},
                                    {"type": "reply", "data": {"id": "21"}},
                                    {"type": "text", "data": {"text": "C"}}]}, api=api)
        await self.relay.from_qq(event, self.route)
        output = self.gateway.broadcast.await_args.args[1]
        self.assertEqual("".join(c["text"] for c in output), "先[回复 Bob20][回复 Bob21]中[回复 Bob20][回复 Bob21]后:ABC")
        self.assertEqual(event.bot.call_action.await_count, 2)

    async def test_configured_semantic_colors_and_three_line_quote_hover_in_both_formats(self):
        self.route.update(prefix="{sender}{reply}:", timezone="Asia/Shanghai", message_colors={
            "link": "gold", "image": "#12ABEF", "record": "red", "video": "dark_purple", "file": "aqua",
            "forward": "dark_green", "reply": "light_purple", "face": "white", "at": "dark_red",
        })
        timestamp = datetime(2026, 10, 31, 3, 11, 11, tzinfo=timezone.utc).timestamp()
        segments = [{"type": "reply", "data": {"id": "20"}},
                    {"type": "text", "data": {"text": "https://example.com www.example.com "}},
                    {"type": "share", "data": {"title": "卡片", "url": "https://example.com/card"}},
                    {"type": "at", "data": {"qq": "456"}}, {"type": "face", "data": {"id": 14}},
                    {"type": "dice", "data": {"result": "6"}}, {"type": "rps", "data": {"result": "2"}},
                    {"type": "mface", "data": {"url": "https://cdn.example.com/mface.gif"}},
                    {"type": "forward", "data": {"content": [{"sender": {"nickname": "N"}, "content": [{"type": "text", "data": {"text": "嵌套"}}]}]}}]
        segments.extend({"type": kind, "data": {"url": "https://cdn.example.com/" + kind}} for kind in ("image", "record", "video", "file"))
        expected_colors = {"[链接]": "gold", "[卡片]": "gold", "[图片]": "#12abef", "[语音]": "red",
                           "[视频]": "dark_purple", "[文件]": "aqua", "[合并消息·1条]": "dark_green",
                           "[回复 占星术士]": "light_purple", "[表情]": "white", "[骰子：6]": "white",
                           "[猜拳：2]": "white", "@456": "dark_red"}
        for modern in (False, True):
            with self.subTest(modern=modern):
                self.server["component_version"] = "modern" if modern else "legacy"
                async def api(action, **params):
                    self.assertEqual(action, "get_msg")
                    return {"group_id": 77, "sender": {"nickname": "占星术士"}, "message": "www.google.com",
                            "time": timestamp * (1000 if modern else 1)}
                event = Event({"message": segments}, api=api)
                event.message_obj.message_id = str(modern)
                await self.relay.from_qq(event, self.route)
                output = self.gateway.broadcast.await_args.args[1]
                found = set()
                for component in output:
                    if component["text"] in expected_colors:
                        found.add(component["text"])
                        self.assertEqual(component["color"], expected_colors[component["text"]])
                self.assertEqual(found, set(expected_colors))
                quote = next(c for c in output if c["text"] == "[回复 占星术士]")
                hover = quote["hover_event"]["value"] if modern else quote["hoverEvent"]["contents"]
                self.assertEqual(hover["text"], "占星术士\nwww.google.com\nDate: 11:11:11")
                link = next(c for c in output if c["text"] == "[链接]")
                self.assertIn("click_event" if modern else "clickEvent", link)
                event.bot.call_action.assert_awaited_once()

    async def test_reply_hover_template_uses_verified_chain_time_and_keeps_message_styles(self):
        self.route.update(prefix="{reply}:", timezone="UTC", templates={"reply_hover": "{sender}\n{message}\n原消息时间: {time}"})
        timestamp = datetime(2026, 10, 31, 3, 11, 11, tzinfo=timezone.utc).timestamp()
        reply = RichReply(id="20", sender_nickname="Bob", timestamp=timestamp * 1000,
                          chain=[{"type": "text", "data": {"text": "&a原文 www.google.com?a=1&b=2"}},
                                 {"type": "image", "data": {"url": "https://cdn.example.com/image"}}])
        async def api(action, **params):
            return {"group_id": 77, "message": [], "time": 0}
        event = Event("[CQ:reply,id=20]正文", [reply, {"type": "text", "text": "正文"}], api=api)
        await self.relay.from_qq(event, self.route)
        quote = next(c for c in self.gateway.broadcast.await_args.args[1] if "回复 Bob" in c["text"])
        contents = quote["hoverEvent"]["contents"]
        self.assertEqual(relay_module.component_text(contents), "Bob\n原文 www.google.com?a=1&b=2[图片]\n原消息时间: 03:11:11")
        self.assertEqual(next(c for c in contents["extra"] if "原文" in c["text"])["color"], "green")
        self.assertFalse(any("clickEvent" in c for c in contents["extra"]))
        event.bot.call_action.assert_awaited_once()

    async def test_invalid_reply_time_stays_unknown_and_cross_group_time_is_not_used(self):
        self.route.update(prefix="{reply}", timezone="Asia/Shanghai")
        for group, timestamp in ((77, "not a timestamp"), (88, 1793416271)):
            with self.subTest(group=group):
                async def api(action, **params):
                    return {"group_id": group, "time": timestamp, "sender": {"nickname": "Bob"}, "message": "内容"}
                event = Event({"message": [{"type": "reply", "data": {"id": "20"}}]}, api=api)
                event.message_obj.message_id = str(group)
                await self.relay.from_qq(event, self.route)
                quote = next(c for c in self.gateway.broadcast.await_args.args[1] if "回复" in c["text"])
                text = quote["hoverEvent"]["contents"]["text"]
                self.assertTrue(text.endswith("Date: 未知"))
                if group == 88:
                    self.assertNotIn("Bob", text)
                    self.assertNotIn("内容\n", text)

    async def test_custom_reply_label_keeps_hover_color_and_original_or_slot_position(self):
        self.route.update(templates={"reply_label": "(回复 @{sender})"}, message_colors={"reply": "gold"})
        async def api(action, **params):
            return {"group_id": 77, "sender": {"nickname": "Bob"}, "message": "原文"}
        for modern, prefix, expected in (
            (False, "{sender}:", "Alice:前(回复 @Bob)后"),
            (True, "{sender}{reply}:", "Alice(回复 @Bob):前后"),
        ):
            with self.subTest(modern=modern):
                self.server["component_version"] = "modern" if modern else "legacy"
                self.route["prefix"] = prefix
                event = Event({"message": [{"type": "text", "data": {"text": "前"}},
                                            {"type": "reply", "data": {"id": "20"}},
                                            {"type": "text", "data": {"text": "后"}}]}, api=api)
                event.message_obj.message_id = str(modern)
                await self.relay.from_qq(event, self.route)
                output = self.gateway.broadcast.await_args.args[1]
                self.assertEqual("".join(c["text"] for c in output), expected)
                quote = next(c for c in output if c["text"] == "(回复 @Bob)")
                self.assertEqual(quote["color"], "gold")
                hover = quote["hover_event"]["value"] if modern else quote["hoverEvent"]["contents"]
                self.assertEqual(hover["text"], "Bob\n原文\nDate: 未知")
                event.bot.call_action.assert_awaited_once()

    async def test_reply_label_does_not_interpret_author_as_template_or_color(self):
        self.route.update(prefix="{reply}", templates={"reply_label": "{{literal}} (回复 @{sender})"},
                          message_colors={"reply": "#123abc"})
        async def api(action, **params):
            return {"group_id": 77, "sender": {"nickname": "§c&l作者{message}\n&#12abef"}, "message": "真正原文"}
        event = Event({"message": [{"type": "reply", "data": {"id": "20"}}]}, api=api)
        await self.relay.from_qq(event, self.route)
        quote = self.gateway.broadcast.await_args.args[1][0]
        self.assertEqual(quote["text"], "{literal} (回复 @作者{message})")
        self.assertEqual(quote["color"], "#123abc")
        self.assertFalse(quote["bold"])
        self.assertEqual(quote["hoverEvent"]["contents"]["text"], "作者{message}\n真正原文\nDate: 未知")

    async def test_blank_reply_label_uses_default_and_explicit_spacing_is_preserved(self):
        self.route["prefix"] = "{reply}:"
        async def api(action, **params):
            return {"group_id": 77, "sender": {"user_id": 456}, "message": "原文"}
        for template, expected in (("", "[回复 456]:"), ("  ", "[回复 456]:"),
                                   ("(回复 @{sender}) ", "(回复 @456) :")):
            with self.subTest(template=template):
                self.route["templates"] = {"reply_label": template}
                event = Event({"message": [{"type": "reply", "data": {"id": "20"}}]}, api=api)
                event.message_obj.message_id = repr(template)
                await self.relay.from_qq(event, self.route)
                self.assertEqual("".join(c["text"] for c in self.gateway.broadcast.await_args.args[1]), expected)

    async def test_http_media_links_work_without_media_service_or_chatimage(self):
        segments = [{"type": "text", "data": {"text": "www.example.com?a=1&b=2 "}}, {"type": "face", "data": {"id": 14}}]
        for kind in ("image", "video", "record", "file"):
            segments.append({"type": kind, "data": {"url": f"https://cdn.example.com/{kind}", "file": "napcat-id", "name": "附件"}})
        event = Event({"group_id": 77, "message": segments})
        await self.relay.from_qq(event, self.route)
        output = self.gateway.broadcast.await_args.args[1]
        shown = "".join(c["text"] for c in output)
        for label in ("[链接]", "[表情]", "[图片]", "[视频]", "[语音]", "[文件：附件]"):
            self.assertIn(label, shown)
        self.assertNotIn("CICode", json.dumps(output))
        clicks = [c["clickEvent"]["value"] for c in output if "clickEvent" in c]
        self.assertIn("https://www.example.com?a=1&b=2", clicks)
        self.assertEqual(len(clicks), 5)
        event.bot.call_action.assert_not_awaited()

    async def test_private_media_url_is_never_published_or_fetched(self):
        self.route["message_colors"] = {"image": "gold"}
        self.media.publish = AsyncMock()
        event = Event({"message": [{"type": "image", "data": {"url": "http://127.0.0.1/secret"}}, {"type": "text", "data": {"text": "正文"}}]})
        await self.relay.from_qq(event, self.route)
        output = self.gateway.broadcast.await_args.args[1]
        self.assertTrue(any("暂不可用" in c["text"] for c in output))
        self.assertEqual(next(c for c in output if "图片" in c["text"])["color"], "gold")
        self.assertIn("正文", "".join(c["text"] for c in output))
        self.media.publish.assert_not_awaited()
        self.assertFalse(any("clickEvent" in c for c in output))

    def test_standard_hex_and_decorations_become_json_properties(self):
        values = relay_module.styled_components("&a绿色&l粗体&r白色 &#12abef六位 §x§1§2§3§4§5§6长格式 §r复位 https://example.com?a=1&b=2 &e黄色")
        by_text = {part["text"].strip(): part for part in values}
        self.assertEqual(by_text["绿色"]["color"], "green")
        self.assertTrue(by_text["粗体"]["bold"])
        self.assertFalse(by_text["白色"]["bold"])
        self.assertEqual(by_text["六位"]["color"], "#12abef")
        self.assertEqual(by_text["长格式"]["color"], "#123456")
        self.assertEqual(by_text["复位"]["color"], "white")
        self.assertEqual(by_text["黄色"]["color"], "yellow")
        self.assertEqual(by_text["[链接]"]["clickEvent"]["value"], "https://example.com?a=1&b=2")
        self.assertEqual(relay_module.strip_styles("§a绿 &b蓝 &#abcdef彩 https://example.com?a=1&b=2"), "绿 蓝 彩 https://example.com?a=1&b=2")

    async def test_notices_with_null_or_no_ids_are_not_accidentally_deduplicated(self):
        payload = {"post_type": "notice", "event_name": "PlayerDeathEvent", "message_id": None, "player": {"nickname": "Steve"}, "death": {"key": "death.attack.fall", "args": [{"text": "Steve"}], "text": "§cSteve 从高处摔下"}}
        first = await self.relay.from_game("paper", payload)
        second = await self.relay.from_game("paper", payload)
        self.assertEqual(first, [(self.route["umo"], "[幻域生存服] Steve 从高处摔下")])
        self.assertEqual(second, first)
        payload.pop("event_name")
        payload["sub_type"] = "player_death"
        self.assertEqual(await self.relay.from_game("paper", payload), first)

    async def test_chat_id_does_not_hide_notice_and_game_colors_are_removed(self):
        await self.relay.from_game("paper", {"event_name": "PlayerChatEvent", "message_id": "1", "player": {"nickname": "Steve"}, "message": "hello"})
        result = await self.relay.from_game("paper", {"event_name": "PlayerDeathEvent", "message_id": "1", "player": {"nickname": "Steve"}, "death": "{\"text\":\"&cSteve 死亡\"}"})
        self.assertEqual(result[0][1], "[幻域生存服] Steve 死亡")
        self.route["events"] = ["PlayerChatEvent"]
        self.assertEqual(await self.relay.from_game("paper", {"event_name": "PlayerDeathEvent", "death": {"text": "hidden"}}), [])

    async def test_face_type_switch_is_respected(self):
        self.route["types"] = ["text"]
        await self.relay.from_qq(Event({"message": [{"type": "face", "data": {"id": 14}}, {"type": "text", "data": {"text": "hello"}}]}), self.route)
        self.assertNotIn("表情", str(self.gateway.broadcast.await_args))

    async def test_mface_uses_public_link_without_chatimage(self):
        event = Event({"message": [{"type": "mface", "data": {"summary": "大表情", "url": "https://cdn.example.com/mface.gif"}}]})
        await self.relay.from_qq(event, self.route)
        component = next(c for c in self.gateway.broadcast.await_args.args[1] if c["text"] == "[表情]")
        self.assertEqual(component["clickEvent"]["value"], "https://cdn.example.com/mface.gif")
        self.assertEqual(component["hoverEvent"]["contents"]["text"], "大表情")
        self.assertNotIn("CICode", str(component))

    async def test_dice_and_rps_use_face_switch_and_text_results(self):
        segments = [{"type": "dice", "data": {"result": "6"}}, {"type": "rps", "data": {"result": "2"}}]
        await self.relay.from_qq(Event({"message": segments}), self.route)
        labels = [c["text"] for c in self.gateway.broadcast.await_args.args[1]]
        self.assertIn("[骰子：6]", labels)
        self.assertIn("[猜拳：2]", labels)
        self.route["types"] = ["text"]
        self.gateway.broadcast.reset_mock()
        event = Event({"message": segments})
        event.message_obj.message_id = "different"
        await self.relay.from_qq(event, self.route)
        self.gateway.broadcast.assert_not_awaited()

    async def test_napcat_record_base64_can_use_cache_without_reading_local_path(self):
        encoded = base64.b64encode(b"ID3" + b"\x00" * 20).decode()
        async def api(action, **params):
            self.assertEqual(action, "get_record")
            return {"url": r"C:\NapCat\private-cache\voice.mp3", "file": r"C:\NapCat\private-cache\voice.mp3", "base64": encoded}
        source = await self.relay._media_source(Event({}, api=api), "record", {"file": "napcat-record-id"})
        self.assertEqual(source, "base64://" + encoded)
        self.settings.data["media"].update(enabled=True, public_base="https://media.example.com")
        self.media._runner = Mock()
        try:
            url = await self.media.publish(source, "record", "voice.mp3")
            self.assertIn("/v/", url)
            row = self.media._db.execute("SELECT mime FROM assets").fetchone()
            self.assertEqual(row["mime"], "audio/mpeg")
        finally:
            self.media._runner = None

    async def test_base64_size_and_encoding_are_validated_before_cache_write(self):
        self.settings.data["media"].update(enabled=True, public_base="https://media.example.com", max_bytes=8)
        self.media._runner = Mock()
        try:
            for source in ("base64://not valid!", "base64://" + base64.b64encode(b"A" * 9).decode()):
                with self.assertRaises(media_module.MediaRejected):
                    await self.media.publish(source, "record")
            self.assertEqual(self.media.status()["assets"], 0)
        finally:
            self.media._runner = None

    async def test_group_file_and_video_lookup_use_received_file_identity(self):
        async def api(action, **params):
            if action == "get_group_file_url":
                self.assertEqual(params["group_id"], 77)
                self.assertEqual(params["file_id"], "group-file-id")
                return {"url": "https://cdn.example.com/file"}
            self.assertEqual(action, "get_file")
            self.assertEqual(params["file_id"], "video-id")
            return {"url": "https://cdn.example.com/video"}
        event = Event({}, api=api)
        self.assertEqual(await self.relay._media_source(event, "file", {"file_id": "group-file-id"}), "https://cdn.example.com/file")
        self.assertEqual(await self.relay._media_source(event, "video", {"file": "video-id"}), "https://cdn.example.com/video")


if __name__ == "__main__":
    unittest.main()
