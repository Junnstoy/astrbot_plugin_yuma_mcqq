"""Authenticated framework requests serialize settings rollback and server rename.

Uses AstrBot's real HTTP router/request context plus the plugin's real settings
and SQLite stores. Network transports are replaced; all records are synthetic.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    from . import test_astrbot_integration as fixture
except ImportError:
    import test_astrbot_integration as fixture

if fixture.HAS_ASTRBOT:
    # The fixture sets an isolated ASTRBOT_ROOT before any framework import.
    import httpx
    import jwt
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from astrbot.dashboard.api import plugins
    from astrbot.dashboard.responses import ApiError, error


class ObservedLock(asyncio.Lock):
    """Expose a deterministic waiter barrier without changing lock behavior."""

    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.waiting.set()
        return await super().acquire()


@unittest.skipUnless(fixture.HAS_ASTRBOT, "requires AstrBot 4.29 runtime")
class AdminSerializationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.package_name = "yuma_admin_serialization_fixture"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(root)]
        sys.modules[cls.package_name] = package
        spec = importlib.util.spec_from_file_location(cls.package_name + ".main", root / "main.py")
        cls.main = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.main
        spec.loader.exec_module(cls.main)

    @classmethod
    def tearDownClass(cls):
        for handler in list(fixture.star_handlers_registry):
            if handler.handler_module_path.startswith(cls.package_name + "."):
                fixture.star_handlers_registry.remove(handler)
        for name in list(fixture.star_map):
            if name.startswith(cls.package_name + "."):
                fixture.star_map.pop(name)
        for name in list(sys.modules):
            if name == cls.package_name or name.startswith(cls.package_name + "."):
                sys.modules.pop(name, None)

    async def asyncSetUp(self):
        self.tasks = []
        self.temp = tempfile.TemporaryDirectory(prefix="admin-serialization-", dir=fixture.TEST_WORK)
        self.app = self.main.Application(self.temp.name, {
            "servers": [{"id": "paper", "name": "fixture", "token": "synthetic-secret"}],
            "routes": [{"umo": "fixture:GroupMessage:1", "server_id": "paper", "scope": "community"}],
            "monitor": {"enabled": False},
        })
        self.app.gateway.start = AsyncMock()
        self.app.gateway.close = AsyncMock()
        self.app.gateway.reload = AsyncMock()
        self.app.media.reload = AsyncMock()
        self.plugin = object.__new__(self.main.YumaMCQQPlugin)
        self.plugin.app = self.app
        self.plugin._ready = True
        self.plugin._closing = False
        self.plugin._settings_lock = ObservedLock()
        self.plugin.commands = types.SimpleNamespace(validate=Mock(), reconcile=AsyncMock())
        self.plugin.context = types.SimpleNamespace(registered_web_apis=[])
        self.plugin.context.register_web_api = lambda *args: self.plugin.context.registered_web_apis.append(args)
        self.plugin._register_web_api()

        self.http_app = FastAPI()
        self.http_app.state.jwt_secret = "isolated-admin-serialization-secret"
        self.http_app.state.core_lifecycle = types.SimpleNamespace(star_context=self.plugin.context)

        @self.http_app.exception_handler(ApiError)
        async def api_error_handler(request, exc):
            return JSONResponse(error(exc.message, exc.data), status_code=exc.status_code)

        self.http_app.include_router(plugins.router, prefix="/api/v1")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.http_app), base_url="http://astrbot.test")
        self.base = "/api/v1/plugins/extensions/" + self.main.PLUGIN_NAME

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        await self.app.close()
        self.temp.cleanup()

    def headers(self, username):
        token = jwt.encode({"username": username, "exp": time.time() + 600},
                           self.http_app.state.jwt_secret, algorithm="HS256")
        return {"Authorization": "Bearer " + token}

    async def barrier(self, event):
        # A missing phase fails promptly instead of leaving a test hanging.
        await asyncio.wait_for(event.wait(), timeout=3)

    async def test_rename_waits_for_failed_reconcile_and_complete_settings_rollback(self):
        original = copy.deepcopy(self.app.settings.data)
        revision = self.app.revision()
        self.app.store.bind_name("10001", "FixturePlayer", "paper", 3600)
        binding = self.app.store.binding("10001")
        self.app.store.adjust_points("community", "10001", "add", 25, "fixture", "seed", "fixture")
        balance = self.app.store.balance("community", "10001")
        phases = []
        registration_started = asyncio.Event()
        fail_registration = asyncio.Event()
        rollback_started = asyncio.Event()
        finish_rollback = asyncio.Event()
        rename_started = asyncio.Event()
        original_rename = self.app._rename_server
        reload_count = 0

        async def fail_reconcile(definitions):
            phases.append("reconcile")
            registration_started.set()
            await fail_registration.wait()
            phases.append("registration_failed")
            raise RuntimeError("synthetic command registry failure")

        async def reload_gateway():
            nonlocal reload_count
            reload_count += 1
            if reload_count == 2:
                phases.append("rollback_started")
                rollback_started.set()
                await finish_rollback.wait()
                phases.append("rollback_finished")

        async def rename(payload, actor):
            phases.append("rename_started")
            rename_started.set()
            return await original_rename(payload, actor)

        self.plugin.commands.reconcile.side_effect = fail_reconcile
        self.app.gateway.reload.side_effect = reload_gateway
        with patch.object(self.app, "_rename_server", side_effect=rename), patch.object(self.main, "logger", Mock()):
            save_task = asyncio.create_task(self.client.post(self.base + "/settings", headers=self.headers("settings-admin"),
                json={"revision": revision, "settings": {"points_name": "临时名称", "commands": original["commands"]}}))
            self.tasks.append(save_task)
            await self.barrier(registration_started)
            self.assertEqual(self.app.settings.data["points_name"], "临时名称")
            self.assertNotEqual(self.app.revision(), revision)

            rename_task = asyncio.create_task(self.client.post(self.base + "/action", headers=self.headers("rename-admin"),
                json={"action": "server_rename", "payload": {
                    "server_id": "paper", "new_server_id": "survival", "revision": revision,
                }}))
            self.tasks.append(rename_task)
            # The second authenticated request has reached the outer lock. It
            # must not start Application.web while registration is in flight.
            await self.barrier(self.plugin._settings_lock.waiting)
            self.assertFalse(rename_started.is_set())
            self.assertFalse(rename_task.done())
            self.assertFalse(self.app._rename_path.exists())

            fail_registration.set()
            await self.barrier(rollback_started)
            self.assertEqual(self.app.settings.data, original)
            self.assertFalse(rename_started.is_set())
            self.assertFalse(rename_task.done())
            self.assertEqual(self.app.store.binding("10001"), binding)
            # Even after old JSON is saved, transport recovery is still part
            # of the save transaction and cannot overlap server migration.
            finish_rollback.set()
            save_response, rename_response = await asyncio.wait_for(asyncio.gather(save_task, rename_task), timeout=5)

        self.assertEqual(save_response.status_code, 500, save_response.text)
        self.assertEqual(rename_response.status_code, 200, rename_response.text)
        self.assertEqual(rename_response.json()["status"], "SUCCESS")
        self.assertEqual(phases, ["reconcile", "registration_failed", "rollback_started", "rollback_finished", "rename_started"])
        self.plugin.commands.reconcile.assert_awaited_once_with(original["commands"])
        expected = copy.deepcopy(original)
        expected["servers"][0]["id"] = "survival"
        expected["routes"][0]["server_id"] = "survival"
        self.assertEqual(self.app.settings.data, expected)
        self.assertEqual(json.loads(self.app.settings.path.read_text(encoding="utf-8")), expected)
        self.assertEqual(self.app.store.binding("10001"), {**binding, "server_id": "survival"})
        self.assertEqual(self.app.store.balance("community", "10001"), balance)
        self.assertEqual(self.app.store.server_references("paper"), 0)
        self.assertFalse(self.app._rename_path.exists())
        self.assertFalse(self.app._maintenance)
        self.assertFalse(self.app._active)
        actors = [tuple(row) for row in self.app._ops.execute("SELECT action,actor FROM audit ORDER BY id")]
        self.assertEqual(actors, [("settings", "dashboard:settings-admin"),
                                  ("settings", "dashboard:settings-admin"),
                                  ("server_rename", "dashboard:rename-admin")])


if __name__ == "__main__":
    unittest.main()
