"""Offline/in-process transport, media, relay and telemetry contracts."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import socket
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "yuma_transport_contract"
if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
gateway_module = importlib.import_module(PACKAGE + ".services.gateway")
media_module = importlib.import_module(PACKAGE + ".services.media")
relay_module = importlib.import_module(PACKAGE + ".services.relay")
monitor_module = importlib.import_module(PACKAGE + ".services.monitor")


def settings(data):
    return types.SimpleNamespace(data=data, server=lambda sid: next(s for s in data.get("servers", []) if s["id"] == sid))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.headers = {}
        self.peer = None
        async def websocket(request):
            self.headers = dict(request.headers)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            self.peer = ws
            async for message in ws:
                request_data = json.loads(message.data)
                self.received.append(request_data)
                command = request_data.get("data", {}).get("command", "")
                if command == "never":
                    continue
                if command == "disconnect":
                    await ws.close()
                    break
                if command.startswith("error-"):
                    await ws.send_json({"post_type": "response", "api": request_data["api"], "echo": request_data["echo"], "code": int(command[6:]), "status": "FAILED", "data": {"error": "RCON failure"}})
                    continue
                if command == "malformed":
                    await ws.send_json({"post_type": "response", "api": request_data["api"], "echo": request_data["echo"], "data": "no status"})
                    continue
                await ws.send_json({"post_type": "response", "api": request_data["api"], "echo": request_data["echo"], "code": 200, "status": "SUCCESS", "data": command})
            return ws
        application = web.Application()
        application.router.add_get("/ws", websocket)
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        port = self.runner.addresses[0][1]
        self.on_event = AsyncMock()
        self.gateway = gateway_module.Gateway(settings({"servers": [{"id": "one", "name": "Paper", "token": "test-token", "url": f"ws://127.0.0.1:{port}/ws", "timeout": 2}]}), self.on_event)
        await self.gateway.start()
        for _ in range(100):
            if self.gateway.status()[0]["connected"]:
                break
            await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.runner.cleanup()

    async def test_handshake_and_correlated_requests(self):
        responses = await asyncio.gather(*(self.gateway.rcon("one", f"cmd{i}") for i in range(5)))
        self.assertEqual(responses, [f"cmd{i}" for i in range(5)])
        self.assertEqual(self.headers["x-self-name"], "Paper")
        self.assertEqual(self.headers["Authorization"], "Bearer test-token")
        self.assertEqual(len({r["echo"] for r in self.received}), 5)

    async def test_timeout_and_disconnect_are_unknown_not_replayed(self):
        with self.assertRaises(gateway_module.OutcomeUnknown):
            await self.gateway.request("one", "send_rcon_command", {"command": "never"}, timeout=0.08)
        self.assertEqual(sum(r["data"].get("command") == "never" for r in self.received), 1)
        with self.assertRaises(gateway_module.OutcomeUnknown):
            await self.gateway.rcon("one", "disconnect")
        with self.assertRaises(gateway_module.GatewayUnavailable):
            await self.gateway.rcon("missing", "say test")

    async def test_event_callback_can_request_without_deadlock(self):
        complete = asyncio.Event()
        async def callback(server_id, event):
            self.assertEqual(await self.gateway.rcon(server_id, "inside-event"), "inside-event")
            complete.set()
        self.gateway.on_event = callback
        await self.peer.send_json({"post_type": "message", "event_name": "PlayerChatEvent", "server_name": "Paper", "message": "hello"})
        await asyncio.wait_for(complete.wait(), 1)

    async def test_rcon_api_error_never_proves_side_effect_failed(self):
        for command in ("error-500", "error-400", "malformed"):
            with self.assertRaises(gateway_module.OutcomeUnknown):
                await self.gateway.rcon("one", command)
        with self.assertRaises(gateway_module.GatewayUnavailable):
            await self.gateway.rcon("one", "error-503")
        with self.assertRaises(ValueError):
            await self.gateway.rcon("one", " ")


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        trusted = self.root / "trusted"
        trusted.mkdir()
        self.config = {"enabled": True, "host": "127.0.0.1", "port": 0, "public_base": "http://127.0.0.1", "trusted_local_roots": [str(trusted)], "max_bytes": 4096, "max_cache_bytes": 32768, "ttl_seconds": 300}
        self.media = media_module.MediaService(self.root, settings({"media": self.config}))
        await self.media.start()
        port = self.media._runner.addresses[0][1]
        self.config["public_base"] = f"http://127.0.0.1:{port}"
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.media.close()
        self.temp.cleanup()

    async def test_local_scope_capability_and_expiry(self):
        source = self.root / "trusted" / "note.txt"
        source.write_text("hello")
        url = await self.media.publish(str(source), "file", "note.txt")
        async with self.client.get(url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), "hello")
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.media._db.execute("UPDATE assets SET expires=?", (time.time() - 1,))
        self.media._db.commit()
        async with self.client.get(url) as response:
            self.assertEqual(response.status, 404)
        other = self.root / "secret.txt"
        other.write_text("not media")
        with self.assertRaises(media_module.MediaRejected):
            await self.media.publish(str(other), "file")

    async def test_network_validation_and_record_escaping(self):
        for url in ("http://127.0.0.1/a", "http://169.254.169.254/", "http://[::1]/", "http://user:pass@example.org/a", "file:///etc/passwd"):
            with self.assertRaises(media_module.MediaRejected):
                self.media._check_url(url)
        resolver = media_module._PublicResolver()
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 80))]
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=answers)):
            with self.assertRaises(media_module.MediaRejected):
                await resolver.resolve("public-looking.example", 80)
        with self.assertRaises(media_module.MediaRejected):
            self.media._local_path("\\\\untrusted-host\\share\\secret")
        url = await self.media.publish_record([{"author": "<script>name</script>", "text": "<img onerror=bad>", "links": [{"url": "javascript:alert(1)", "label": "bad"}]}])
        async with self.client.get(url) as response:
            body = await response.text()
            self.assertIn("&lt;script&gt;", body)
            self.assertNotIn("<script>", body)
            self.assertNotIn("javascript:", body)
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

    async def test_reload_keeps_cache_database_usable(self):
        await self.media.reload()
        self.assertTrue(self.media.status()["running"])

    async def test_redirect_is_revalidated_before_second_request(self):
        class Redirect:
            status = 302
            headers = {"Location": "http://169.254.169.254/metadata"}
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return None
        await self.media._session.close()
        fake = types.SimpleNamespace(get=unittest.mock.Mock(return_value=Redirect()), close=AsyncMock())
        self.media._session = fake
        with self.assertRaises(media_module.MediaRejected):
            await self.media._download("https://public.example/image")
        self.assertEqual(fake.get.call_count, 1)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.route = {"umo": "bot:GroupMessage:1", "server_id": "one", "enabled": True, "types": ["text", "image", "record", "video", "file", "link", "forward", "reply", "at"], "events": ["PlayerChatEvent"]}
        self.config = settings({"servers": [{"id": "one", "name": "Paper", "component_version": "modern"}], "routes": [self.route]})
        self.gateway = types.SimpleNamespace(broadcast=AsyncMock())
        self.media = types.SimpleNamespace(publish=AsyncMock(return_value="https://media.example/m/token"), publish_record=AsyncMock(return_value="https://media.example/v/record"))
        self.relay = relay_module.Relay(self.config, self.gateway, self.media)

    def event(self, segments):
        class Event:
            unified_msg_origin = "bot:GroupMessage:1"
            def get_sender_id(self): return "123"
            def get_sender_name(self): return "Alice"
            def get_self_id(self): return "999"
            def get_messages(self): return []
        event = Event()
        event.message_obj = types.SimpleNamespace(message_id="one", raw_message={"group_id": 1, "message": segments})
        async def api(action, **params):
            if action == "get_msg": return {"group_id": 1, "sender": {"nickname": "Bob"}, "message": [{"type": "text", "data": {"text": "quoted text"}}]}
            if action == "get_record": return {"url": "https://media.example/voice.mp3"}
            if action == "get_forward_msg": return {"messages": [{"sender": {"nickname": "Carol"}, "content": [{"type": "text", "data": {"text": "nested message"}}]}]}
            return {}
        event.bot = types.SimpleNamespace(call_action=AsyncMock(side_effect=api))
        return event

    async def test_all_rich_types_and_quote_hover_preserve_order(self):
        segments = [{"type": "text", "data": {"text": "hello https://example.org/a?x=1&b=2 "}}]
        for kind in ("image", "record", "video", "file"):
            segments.append({"type": kind, "data": {"url": "https://example.org/file", "file": "id", "name": "sample"}})
        segments.extend([{"type": "reply", "data": {"id": "20"}}, {"type": "forward", "data": {"id": "30"}}])
        await self.relay.from_qq(self.event(segments), self.route)
        components = self.gateway.broadcast.await_args.args[1]
        joined = "".join(c["text"] for c in components)
        for label in ("[图片]", "[语音]", "[视频]", "[文件：sample]", "[回复 Bob]", "[合并消息·1条]"):
            self.assertIn(label, joined)
        url = next(c["click_event"]["url"] for c in components if c["text"] == "[链接]")
        self.assertIn("&b=2", url)
        quote = next(c for c in components if "回复 Bob" in c["text"])
        self.assertEqual(quote["hover_event"]["value"]["text"], "quoted text")
        await self.relay.from_qq(self.event(segments), self.route)
        self.assertEqual(self.gateway.broadcast.await_count, 1)

    async def test_media_unavailable_preserves_text_and_game_routes_exact_umo(self):
        self.media.publish.side_effect = media_module.MediaUnavailable("disabled")
        await self.relay.from_qq(self.event([{"type": "image", "data": {"url": "https://example.org/image"}}, {"type": "text", "data": {"text": "body"}}]), self.route)
        output = "".join(c["text"] for c in self.gateway.broadcast.await_args.args[1])
        self.assertIn("图片·暂不可用", output)
        self.assertIn("body", output)
        result = await self.relay.from_game("one", {"event_name": "PlayerChatEvent", "message_id": "mc1", "player": {"nickname": "Steve"}, "message": "hello"})
        self.assertEqual(result, [("bot:GroupMessage:1", "[Paper] Steve：hello")])

    async def test_quote_never_fetches_other_conversation_content(self):
        event = self.event([])
        event.bot.call_action = AsyncMock(return_value={"message_type": "private", "user_id": "42", "message": "private code"})
        author, quote = await self.relay._quote(event, {"id": "31"})
        self.assertNotIn("private code", quote)
        self.assertEqual(author, "某人")

    async def test_current_queqiao_achievement_and_death_models(self):
        self.route["events"] += ["PlayerAchievementEvent", "PlayerDeathEvent"]
        result = await self.relay.from_game("one", {"event_name": "PlayerAchievementEvent", "player": {"nickname": "Steve"}, "achievement": {"translate": {"text": "Steve 获得了进度 石器时代"}}})
        self.assertEqual(result[0][1], "[Paper] Steve 获得了进度 石器时代")
        result = await self.relay.from_game("one", {"event_name": "PlayerDeathEvent", "player": {"nickname": "Steve"}, "death": {"key": "death.attack.fall", "text": "Steve 从高处摔了下来"}})
        self.assertEqual(result[0][1], "[Paper] Steve 从高处摔了下来")


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def test_parsers_ignore_window_numbers_and_missing(self):
        self.assertEqual(monitor_module.parse_tps("TPS from last 1m, 5m, 15m: 20.0, 19.5, 19.7")["value"], 20)
        self.assertEqual(monitor_module.parse_tps("§8[§e⚡§8] TPS from last 5s, 10s, 1m, 5m, 15m:[⚡] *20.0, 19.9, 19.8, 19.7, 19.6[⚡] CPU usage from last 10s, 1m, 15m: 10%, 10%, 20%")["value"], 20)
        self.assertIsNone(monitor_module.parse_tps("Unknown command")["value"])
        self.assertEqual(monitor_module.parse_mspt("Server tick times (avg/min/max) from last 5s, 10s, 1m:\n10.0/2.0/30.0, 12.0/3.0/40.0, 15.0/4.0/50.0")["value"], 10)
        self.assertEqual(monitor_module.parse_ping("Ping min/median/95%ile/max: 5/20/30/50 ms")["value"], 20)

    async def test_history_retains_nulls_and_chart_is_png(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings({"timezone": "Asia/Shanghai", "servers": [{"id": "one", "name": "Paper", "game_host": ""}], "monitor": {"enabled": False, "retention_days": 7}})
            gateway = types.SimpleNamespace(rcon=AsyncMock(side_effect=gateway_module.GatewayUnavailable("offline")))
            monitor = monitor_module.Monitor(None, config, gateway, directory)
            try:
                sample = await monitor.sample("one")
                self.assertEqual(sample["status"], "missing")
                self.assertIsNone(sample["tps"])
                self.assertEqual(len(monitor.history("one")), 1)
                image = Path(await monitor.render("one"))
                self.assertEqual(image.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
                await monitor.reload()
                self.assertEqual(len(monitor.history("one")), 1)
            finally:
                await monitor.close()
