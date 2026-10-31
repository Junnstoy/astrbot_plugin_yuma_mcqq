"""Authenticated reverse WebSocket regression checks using loopback peers."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from urllib.parse import quote_plus, unquote_plus
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "yuma_reverse_contract"
if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
gateway_module = importlib.import_module(PACKAGE + ".services.gateway")


class ReverseGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = [
            {"id": sid, "name": name, "connection_mode": "reverse", "reverse_host": "127.0.0.1",
             "reverse_port": 0, "reverse_path": "/queqiao", "token": "secret-" + sid, "timeout": 1}
            for sid, name in [("one", "Paper"), ("two", "生存服 +")]
        ]
        self.gateway = gateway_module.Gateway(types.SimpleNamespace(data={"servers": self.servers}), AsyncMock())
        await self.gateway.start()
        runner, _ = next(iter(self.gateway._listeners.values()))
        self.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        self.client = aiohttp.ClientSession()
        self.peers = []

    async def asyncTearDown(self):
        for peer in self.peers:
            await peer.close()
        await self.gateway.close()
        await self.client.close()

    async def connect(self, index=0):
        config = self.servers[index]
        peer = await self.client.ws_connect(self.url + config["reverse_path"], headers={
            "x-self-name": quote_plus(config["name"]), "Authorization": "Bearer " + config["token"],
        })
        self.peers.append(peer)
        return peer

    async def reply(self, peer, packet, *, data="ok", code=200, status="SUCCESS", **extra):
        await peer.send_json({"post_type": "response", "api": packet["api"], "echo": packet["echo"],
                              "code": code, "status": status, "data": data, **extra})

    async def test_authentication_path_and_duplicate_peer_rejected_before_upgrade(self):
        cases = [
            ("/queqiao", {}, 401),
            ("/wrong", {"x-self-name": "Paper", "Authorization": "Bearer secret-one"}, 404),
            ("/queqiao", {"x-self-name": "Paper", "Authorization": "Bearer wrong"}, 401),
            ("/queqiao", {"x-self-name": "Unknown", "Authorization": "Bearer secret-one"}, 401),
            ("/queqiao", {"x-self-name": "Paper", "Authorization": "Bearer secret-one", "x-client-origin": "astrbot_plugin_yuma_mcqq"}, 403),
        ]
        for path, headers, code in cases:
            with self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
                await self.client.ws_connect(self.url + path, headers=headers)
            self.assertEqual(failure.exception.status, code)
        peer = await self.connect()
        with self.assertRaises(aiohttp.WSServerHandshakeError) as duplicate:
            await self.connect()
        self.assertEqual(duplicate.exception.status, 409)
        await peer.close()

    async def test_shared_listener_routes_names_and_correlates_out_of_order_echoes(self):
        self.assertEqual(len(self.gateway._listeners), 1)
        first, second = await self.connect(), await self.connect(1)
        tasks = [asyncio.create_task(self.gateway.rcon("one", "first")),
                 asyncio.create_task(self.gateway.rcon("one", "second")),
                 asyncio.create_task(self.gateway.rcon("two", "third"))]
        a = await first.receive_json(timeout=1)
        b = await first.receive_json(timeout=1)
        c = await second.receive_json(timeout=1)
        await self.reply(first, c, data="wrong connection")
        await self.reply(first, a, data="wrong identity", server_name="Wrong")
        await self.reply(first, b, data="second")
        await self.reply(second, c, data="third")
        await self.reply(first, a, data="first")
        self.assertEqual(await asyncio.gather(*tasks), ["first", "second", "third"])
        self.assertTrue(all(row["connected"] for row in self.gateway.status()))
        self.assertTrue(all(row["connection_mode"] == "reverse" for row in self.gateway.status()))

    async def test_event_callback_can_request_rcon_without_blocking_reader(self):
        peer = await self.connect()
        done = asyncio.Event()

        async def callback(server_id, payload):
            self.assertEqual(server_id, "one")
            self.assertEqual(await self.gateway.rcon(server_id, "inside-event"), "received")
            done.set()

        self.gateway.on_event = callback
        await peer.send_json({"post_type": "message", "server_name": "Paper", "event_name": "PlayerChatEvent"})
        packet = await peer.receive_json(timeout=1)
        await self.reply(peer, packet, data="received")
        await asyncio.wait_for(done.wait(), 1)

    async def test_disconnect_and_reconnect_never_replay_mutations(self):
        peer = await self.connect()
        pending = asyncio.create_task(self.gateway.rcon("one", "give player diamond"))
        previous = await peer.receive_json(timeout=1)
        await peer.close()
        with self.assertRaises(gateway_module.OutcomeUnknown):
            await pending
        peer = await self.connect()
        with self.assertRaises(asyncio.TimeoutError):
            await peer.receive_json(timeout=0.05)
        pending = asyncio.create_task(self.gateway.rcon("one", "list"))
        current = await peer.receive_json(timeout=1)
        self.assertNotEqual(previous["echo"].split(":")[0], current["echo"].split(":")[0])
        await self.reply(peer, previous, data="late old result")
        await self.reply(peer, current, data="players")
        self.assertEqual(await pending, "players")
        self.assertEqual(self.gateway.status()[0]["reconnects"], 1)

    async def test_timeout_and_rcon_rejections_preserve_outcome_semantics(self):
        peer = await self.connect()
        pending = asyncio.create_task(self.gateway.request("one", "send_rcon_command", {"command": "never"}, timeout=0.04))
        await peer.receive_json(timeout=1)
        with self.assertRaises(gateway_module.OutcomeUnknown):
            await pending
        for code, expected in [(503, gateway_module.GatewayUnavailable), (500, gateway_module.OutcomeUnknown)]:
            pending = asyncio.create_task(self.gateway.rcon("one", "give item"))
            packet = await peer.receive_json(timeout=1)
            await self.reply(peer, packet, code=code, status="FAILED")
            with self.assertRaises(expected):
                await pending

    async def test_close_finishes_pending_requests_and_releases_listener(self):
        peer = await self.connect()
        pending = asyncio.create_task(self.gateway.rcon("one", "give item"))
        await peer.receive_json(timeout=1)
        await asyncio.wait_for(self.gateway.close(), 2)
        with self.assertRaises(gateway_module.OutcomeUnknown):
            await pending
        self.assertEqual(self.gateway._listeners, {})
        self.assertEqual(self.gateway.status(), [])
        self.assertIsNone(self.gateway._session)
        with self.assertRaises(gateway_module.GatewayUnavailable):
            await self.gateway.rcon("one", "list")

    async def test_reverse_never_listens_without_token(self):
        other = gateway_module.Gateway(types.SimpleNamespace(data={"servers": [{**self.servers[0], "token": ""}]}), AsyncMock())
        with self.assertRaisesRegex(ValueError, "token"):
            await other.start()
        self.assertEqual(other._listeners, {})
        self.assertIsNone(other._session)

    async def test_listener_bind_failure_cleans_partial_startup(self):
        occupied = web.AppRunner(web.Application())
        await occupied.setup()
        await web.TCPSite(occupied, "127.0.0.1", 0).start()
        other = gateway_module.Gateway(types.SimpleNamespace(data={"servers": [
            self.servers[0], {**self.servers[1], "reverse_port": occupied.addresses[0][1]},
        ]}), AsyncMock())
        try:
            with self.assertRaises(OSError):
                await other.start()
            self.assertEqual(other._listeners, {})
            self.assertEqual(other._connections, {})
            self.assertIsNone(other._session)
        finally:
            await other.close()
            await occupied.cleanup()


class ForwardIdentityRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_encodes_chinese_plus_and_percent_name_but_preserves_token(self):
        name = "生存服 + 100% /"
        token = "literal+% token"
        observed = {}

        async def accept(request):
            observed.update(request.headers)
            if unquote_plus(request.headers.get("x-self-name", "")) != name:
                raise web.HTTPUnauthorized()
            peer = web.WebSocketResponse()
            await peer.prepare(request)
            async for message in peer:
                packet = json.loads(message.data)
                await peer.send_json({"post_type": "response", "api": packet["api"], "echo": packet["echo"],
                                      "code": 200, "status": "SUCCESS", "data": "identity-ok"})
            return peer

        application = web.Application()
        application.router.add_get("/queqiao", accept)
        runner = web.AppRunner(application)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        gateway = gateway_module.Gateway(types.SimpleNamespace(data={"servers": [{
            "id": "encoded", "name": name, "token": token,
            "url": f"ws://127.0.0.1:{runner.addresses[0][1]}/queqiao",
        }]}), AsyncMock())
        try:
            await gateway.start()
            async with asyncio.timeout(2):
                while not gateway.status()[0]["connected"]:
                    await asyncio.sleep(0.01)
            self.assertEqual(await gateway.rcon("encoded", "list"), "identity-ok")
            self.assertEqual(observed["x-self-name"], quote_plus(name))
            self.assertEqual(observed["Authorization"], "Bearer " + token)
            self.assertEqual(gateway.status()[0]["name"], name)
        finally:
            await gateway.close()
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
