"""Local barriers prove fast paths and bounded metadata concurrency without QQ."""
from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock

from tests import test_relay_revision as fixtures

relay_module = fixtures.relay_module
media_module = fixtures.media_module
Event = fixtures.Event


class MediaLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.route = {"umo": "napcat:GroupMessage:77", "server_id": "paper"}
        self.server = {"id": "paper", "name": "Paper", "component_version": "legacy"}
        self.settings = types.SimpleNamespace(data={"media": {"enabled": False}}, server=lambda _: self.server)
        self.gateway = types.SimpleNamespace(broadcast=AsyncMock())
        self.media = media_module.MediaService(self.temp.name, self.settings)
        self.relay = relay_module.Relay(self.settings, self.gateway, self.media)

    async def asyncTearDown(self):
        await self.relay.close()
        await self.media.close()
        self.temp.cleanup()

    async def test_public_image_bypasses_lookup_download_and_enabled_cache(self):
        self.settings.data["media"].update(enabled=True, public_base="https://cache.example.com")
        self.media.publish = AsyncMock(side_effect=AssertionError("no cache call for an existing URL"))
        self.media._download = AsyncMock(side_effect=AssertionError("no download for a click link"))
        url = "https://cdn.example.com/photo.png?token=private-signature"
        event = Event({"message": [{"type": "image", "data": {"url": url, "file": "opaque-id"}}]})
        await self.relay.from_qq(event, self.route)
        event.bot.call_action.assert_not_awaited()
        self.media.publish.assert_not_awaited()
        self.media._download.assert_not_awaited()
        component = next(item for item in self.gateway.broadcast.await_args.args[1] if item["text"] == "[图片]")
        self.assertEqual(component["clickEvent"]["value"], url)

    async def test_raw_id_and_empty_url_do_not_discard_framework_public_source(self):
        url = "https://cdn.example.com/already-resolved.png"
        event = Event({"message": [{"type": "image", "data": {"file": "opaque-id", "url": ""}}]},
                      adapted=[{"type": "image", "file": url, "url": "", "path": ""}])
        self.media.publish = AsyncMock()
        await self.relay.from_qq(event, self.route)
        event.bot.call_action.assert_not_awaited()
        self.media.publish.assert_not_awaited()
        self.assertEqual(next(item["clickEvent"]["value"] for item in self.gateway.broadcast.await_args.args[1]
                              if item["text"] == "[图片]"), url)

    async def test_public_file_path_field_is_not_mistaken_for_an_unresolved_image_id(self):
        url = "https://cdn.example.com/file-path.png"
        event = Event({"message": [{"type": "image", "data": {"file_path": url, "file": "opaque-id"}}]})
        await self.relay.from_qq(event, self.route)
        event.bot.call_action.assert_not_awaited()
        self.assertIn(url, str(self.gateway.broadcast.await_args))

    async def test_raw_private_url_still_rejected_even_with_adapted_public_url(self):
        event = Event({"message": [{"type": "image", "data": {"url": "http://127.0.0.1/private", "file": "opaque-id"}}]},
                      adapted=[{"type": "image", "file": "https://cdn.example.com/other"}])
        self.media.publish = AsyncMock()
        await self.relay.from_qq(event, self.route)
        self.assertIn("[图片·暂不可用]", str(self.gateway.broadcast.await_args))
        self.assertNotIn("clickEvent", str(self.gateway.broadcast.await_args))
        self.media.publish.assert_not_awaited()
        event.bot.call_action.assert_not_awaited()

    async def test_images_and_verified_quote_queries_overlap_without_reordering_components(self):
        gate, all_started = asyncio.Event(), asyncio.Event()
        actions = []

        async def api(action, **params):
            actions.append(action)
            if len(actions) == 3:
                all_started.set()
            await gate.wait()
            if action == "get_msg":
                return {"group_id": 77, "sender": {"nickname": "引用作者"}, "message": [{"type": "text", "data": {"text": "引用正文"}}]}
            return {"url": "https://cdn.example.com/" + params["file"]}

        event = Event({"message": [{"type": "reply", "data": {"id": "7"}},
                                   {"type": "image", "data": {"file": "first"}},
                                   {"type": "text", "data": {"text": "中间文字"}},
                                   {"type": "image", "data": {"file": "second"}}]}, api=api)
        task = asyncio.create_task(self.relay.from_qq(event, self.route))
        try:
            # Serial code cannot reach this barrier before the gate is released.
            await asyncio.wait_for(all_started.wait(), 1)
            self.gateway.broadcast.assert_not_awaited()
            gate.set()
            await asyncio.wait_for(task, 1)
        finally:
            gate.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        output = self.gateway.broadcast.await_args.args[1]
        self.assertEqual([part["clickEvent"]["value"] for part in output if "clickEvent" in part],
                         ["https://cdn.example.com/first", "https://cdn.example.com/second"])
        self.assertEqual("".join(part["text"] for part in output), "[QQ群] Alice[回复 引用作者]：[图片]中间文字[图片]")
        self.assertCountEqual(actions, ["get_msg", "get_image", "get_image"])

    async def test_seven_lookups_have_at_most_four_in_flight_and_preserve_image_order(self):
        gate, first_wave = asyncio.Event(), asyncio.Event()
        active, peak, started = 0, 0, []

        async def api(action, **params):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.append(params["file"])
            if len(started) == 4:
                first_wave.set()
            try:
                await gate.wait()
                return {"url": "https://cdn.example.com/" + params["file"]}
            finally:
                active -= 1

        event = Event({"message": [{"type": "image", "data": {"file": str(index)}} for index in range(7)]}, api=api)
        task = asyncio.create_task(self.relay.from_qq(event, self.route))
        try:
            await asyncio.wait_for(first_wave.wait(), 1)
            self.assertEqual(len(started), 4)
            gate.set()
            await asyncio.wait_for(task, 1)
        finally:
            gate.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(peak, 4)
        self.assertEqual(active, 0)
        self.assertEqual([part["clickEvent"]["value"] for part in self.gateway.broadcast.await_args.args[1] if "clickEvent" in part],
                         [f"https://cdn.example.com/{index}" for index in range(7)])

    async def test_cancelled_relay_cancels_all_pending_metadata_and_never_broadcasts(self):
        first_wave, gate = asyncio.Event(), asyncio.Event()
        active = 0

        async def api(action, **params):
            nonlocal active
            active += 1
            if active == 4:
                first_wave.set()
            try:
                await gate.wait()
            finally:
                active -= 1

        event = Event({"message": [{"type": "image", "data": {"file": str(index)}} for index in range(7)]}, api=api)
        task = asyncio.create_task(self.relay.from_qq(event, self.route))
        try:
            await asyncio.wait_for(first_wave.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(active, 0)
            self.gateway.broadcast.assert_not_awaited()
        finally:
            gate.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_explicit_publish_still_downloads_and_caches_public_sources(self):
        self.settings.data["media"].update(enabled=True, public_base="https://cache.example.com")
        self.media._runner = Mock()
        self.media._download = AsyncMock(return_value=(b"\x89PNG\r\n\x1a\nbody", "image/png"))
        try:
            source = "https://cdn.example.com/explicit.png"
            url = await self.media.publish(source, "image")
            self.media._download.assert_awaited_once_with(source)
            self.assertTrue(url.startswith("https://cache.example.com/m/"))
            self.assertEqual(self.media.status()["assets"], 1)
        finally:
            self.media._runner = None

if __name__ == "__main__":
    unittest.main()
