"""Real AstrBot dashboard HTTP routes, auth, schemas and command persistence.

Only command storage and the plugin event fixture are isolated. Requests go
through FastAPI/ASGI and the installed framework's own command service.
"""
from __future__ import annotations

import time
import types
import unittest
from urllib.parse import quote

try:
    from . import test_astrbot_integration as fixture
except ImportError:
    import test_astrbot_integration as fixture

if fixture.HAS_ASTRBOT:
    import httpx
    import jwt
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from astrbot.dashboard.api import extensions
    from astrbot.dashboard.responses import ApiError, error
    from astrbot.dashboard.services.command_service import CommandService


@unittest.skipUnless(fixture.HAS_ASTRBOT, "requires AstrBot 4.29 runtime")
class CommandHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.CommandRegistryTests.asyncSetUp(self)
        # Match the deployed plugin module path and management identity shape.
        plugin_metadata = fixture.star_map.pop(self.module)
        self.module = "data.plugins.astrbot_plugin_yuma_mcqq.main"
        self.plugin_class.__module__ = self.module
        plugin_metadata.module_path = self.module
        plugin_metadata.name = "astrbot_plugin_yuma_mcqq"
        fixture.star_map[self.module] = plugin_metadata
        self.plugin.app = types.SimpleNamespace(settings=types.SimpleNamespace(data={"command_prefix": "mc"}))
        self.definitions = [{"id": "durable-id", "name": "list", "aliases": [],
                             "description": "测试查询在线人数", "enabled": True}]
        fixture.integration.install_builtin_commands(self.plugin_class)
        self.bridge = fixture.integration.CommandRegistryBridge(self.plugin)
        await self.bridge.reconcile(self.definitions)
        self.http_app = FastAPI()
        self.http_app.state.jwt_secret = "isolated-command-http-regression-secret"
        self.http_app.state.db = self.db
        self.http_app.state.services = types.SimpleNamespace(commands=CommandService({"wake_prefix": ["/"]}))

        @self.http_app.exception_handler(ApiError)
        async def api_error_handler(request, exc):
            return JSONResponse(error(exc.message, exc.data), status_code=exc.status_code)

        self.http_app.include_router(extensions.router, prefix="/api/v1")
        self.http_app.include_router(extensions.legacy_router)
        token = jwt.encode({"username": "isolated-admin", "exp": time.time() + 600}, self.http_app.state.jwt_secret, algorithm="HS256")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.http_app), base_url="http://astrbot.test",
                                       headers={"Authorization": "Bearer " + token})

    async def asyncTearDown(self):
        await self.client.aclose()
        await fixture.CommandRegistryTests.asyncTearDown(self)

    def path(self, full_name):
        return "/api/v1/commands/" + quote(full_name, safe="")

    async def update(self, full_name, payload):
        response = await self.client.patch(self.path(full_name), json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ok", response.text)
        return response.json()["data"]

    def register_other_plugin(self, name, *, aliases=(), enabled=True):
        """Use the framework decorator to model an independently loaded plugin."""
        framework = fixture.integration._framework()
        module = "data.plugins.other_command_http_test.main"
        raw = fixture.integration._handler("other", module, "cmd_other", "Other plugin")
        framework.register_command(name, alias=set(aliases))(raw)
        handler = framework.registry.get_handler_by_full_name(module + "_cmd_other")
        handler.enabled = enabled
        framework.star_map[module] = fixture.StarMetadata(
            name="other_command_http_test", module_path=module, activated=enabled,
        )
        self.addCleanup(framework.registry.remove, handler)
        self.addCleanup(framework.star_map.pop, module, None)
        return handler

    async def test_user_list_to_online_count_without_alias_or_plugin_prefix(self):
        self.plugin.app.settings.data["command_prefix"] = ""
        full_name = self.bridge.full_name("durable-id")
        response = await self.client.get("/api/v1/commands")
        row = next(item for item in response.json()["data"]["items"] if item["handler_full_name"] == full_name)
        self.assertEqual(row["current_fragment"], "list")
        changed = await self.update(row["handler_full_name"], {"alias": "在线人数", "aliases": []})
        self.assertEqual(changed["current_fragment"], "在线人数")
        self.assertEqual(changed["aliases"], [])
        metadata = fixture.star_handlers_registry.get_handler_by_full_name(full_name)
        command_filter = metadata.event_filters[0]
        # AstrBot waking stage already strips its global "/" before filtering.
        self.assertTrue(command_filter.filter(fixture.FakeEvent("在线人数", ai=True), {"wake_prefix": ["/"]}))
        self.assertFalse(command_filter.filter(fixture.FakeEvent("在线人数", ai=False), {"wake_prefix": ["/"]}))
        self.assertFalse(command_filter.filter(fixture.FakeEvent("list", ai=True), {}))

    async def test_http_list_rename_alias_permission_toggle_and_custom_reload(self):
        response = await self.client.get("/api/v1/commands")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["data"]["items"]
        full_name = self.bridge.full_name("durable-id")
        self.assertIn(full_name, {row["handler_full_name"] for row in items})
        renamed = await self.update(full_name, {"alias": "在线人数", "aliases": ["在线列表"]})
        self.assertEqual(renamed["effective_command"], "在线人数")
        for permission in ("admin", "member", "group_admin", "shared_group_admin"):
            changed = await self.update(full_name, {"permission_group": permission})
            self.assertEqual(changed["permission"], permission)
        changed = await self.update(full_name, {"enabled": False})
        self.assertFalse(changed["enabled"])
        self.bridge.close()
        self.bridge = fixture.integration.CommandRegistryBridge(self.plugin)
        await self.bridge.reconcile(self.definitions)
        response = await self.client.get("/api/v1/commands")
        loaded = next(row for row in response.json()["data"]["items"] if row["handler_full_name"] == full_name)
        self.assertEqual(loaded["effective_command"], "在线人数")
        self.assertEqual(loaded["aliases"], ["在线列表"])
        self.assertEqual(loaded["permission"], "shared_group_admin")
        self.assertFalse(loaded["enabled"])
        await self.update(full_name, {"enabled": True})
        metadata = fixture.star_handlers_registry.get_handler_by_full_name(full_name)
        event = fixture.FakeEvent("mc在线列表 payload", ai=False)
        self.assertTrue(metadata.event_filters[0].filter(event, {}))
        await metadata.handler(event, **event.get_extra("parsed_params"))
        self.plugin._execute_action.assert_awaited_once_with("custom:durable-id", event, "payload")
        self.assertEqual(event.message_str, "mc在线列表 payload")
        self.assertFalse(event.is_at_or_wake_command)

    async def test_http_builtin_rename_alias_and_permission_with_prefix(self):
        full_name = self.module + "_cmd_signin"
        changed = await self.update(full_name, {"alias": "每日签到", "aliases": ["签到测试别名"]})
        self.assertEqual(changed["effective_command"], "每日签到")
        await self.update(full_name, {"permission_group": "admin"})
        metadata = fixture.star_handlers_registry.get_handler_by_full_name(full_name)
        framework = fixture.integration._framework()
        command_filter = next(item for item in metadata.event_filters if isinstance(item, framework.CommandFilter))
        permission_filter = next(item for item in metadata.event_filters if isinstance(item, framework.PermissionTypeFilter))
        event = fixture.FakeEvent("mc 每日签到", ai=False)
        self.assertTrue(command_filter.filter(event, {}))
        self.assertFalse(permission_filter.filter(event, {}))
        event.is_admin = lambda: True
        self.assertTrue(permission_filter.filter(event, {}))
        self.assertTrue(command_filter.filter(fixture.FakeEvent("mc签到测试别名"), {}))
        self.assertFalse(command_filter.filter(fixture.FakeEvent("mc签到"), {}))

    async def test_legacy_http_routes_use_the_same_managed_command(self):
        full_name = self.bridge.full_name("durable-id")
        for endpoint, body in [
            ("rename", {"new_name": "旧API改名", "aliases": ["旧API别名"]}),
            ("permission", {"permission": "admin"}),
            ("toggle", {"enabled": False}),
        ]:
            response = await self.client.post("/api/commands/" + endpoint, json={"handler_full_name": full_name, **body})
            self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.get("/api/commands")
        row = next(item for item in response.json()["data"]["items"] if item["handler_full_name"] == full_name)
        self.assertEqual(row["effective_command"], "旧API改名")
        self.assertEqual(row["permission"], "admin")
        self.assertFalse(row["enabled"])

    async def test_http_400_conflicts_include_reason_and_do_not_mutate_command(self):
        full_name = self.bridge.full_name("durable-id")
        for body in ({"alias": "签到"}, {"alias": "合法新名称", "aliases": ["签到"]}):
            response = await self.client.patch(self.path(full_name), json=body)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("占用", response.json()["message"])
        metadata = fixture.star_handlers_registry.get_handler_by_full_name(full_name)
        self.assertEqual(metadata.event_filters[0].command_name, "list")

    async def test_other_disabled_plugin_still_occupies_name_under_framework_policy(self):
        self.register_other_plugin("在线人数", enabled=False)
        full_name = self.bridge.full_name("durable-id")
        response = await self.client.patch(self.path(full_name), json={"alias": "在线人数", "aliases": []})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["message"], "指令名 '在线人数' 已被其他指令占用。")
        metadata = fixture.star_handlers_registry.get_handler_by_full_name(full_name)
        self.assertEqual(metadata.event_filters[0].command_name, "list")

    async def test_retained_alias_conflict_rejects_otherwise_free_new_name(self):
        self.definitions[0]["aliases"] = ["旧别名"]
        await self.bridge.reconcile(self.definitions)
        self.register_other_plugin("其他查询", aliases=["旧别名"])
        full_name = self.bridge.full_name("durable-id")
        response = await self.client.get("/api/v1/commands")
        row = next(item for item in response.json()["data"]["items"] if item["handler_full_name"] == full_name)
        # The actual dashboard preserves the previous aliases in the rename form.
        response = await self.client.patch(self.path(full_name), json={"alias": "在线人数", "aliases": row["aliases"]})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["message"], "别名 '旧别名' 已被其他指令占用。")
        changed = await self.update(full_name, {"alias": "在线人数", "aliases": []})
        self.assertEqual(changed["effective_command"], "在线人数")

    async def test_admin_rcon_has_an_independent_manageable_prefixed_action(self):
        self.assertIn("rcon", fixture.integration.ADMIN_ACTIONS)
        full_name = self.module + "_cmd_rcon"
        metadata = fixture.star_handlers_registry.get_handler_by_full_name(full_name)
        self.plugin.app.settings.data["command_prefix"] = ""
        event = fixture.FakeEvent("cmd cmi ping rincho", ai=True)
        self.assertTrue(metadata.event_filters[0].filter(event, {}))
        await metadata.handler(self.plugin, event, **event.get_extra("parsed_params"))
        self.plugin._execute_action.assert_awaited_once_with("rcon", event, "cmi ping rincho")
        changed = await self.update(full_name, {"alias": "控制台", "aliases": ["执行命令"]})
        self.assertEqual(changed["original_command"], "cmd")
        metadata = fixture.star_handlers_registry.get_handler_by_full_name(full_name)
        raw_commands = [
            "cmi ping rincho",
            'tellraw @a {"text":"hello  {player}","extra":[{"text":"quote: \\\" hi"}]}',
            "say first\nsay second",  # Application must see and reject the newline.
        ]
        for command in raw_commands:
            for prefix, trigger, ai in [("mc", "mc 执行命令", False), ("mc", "mc控制台", False), ("", "控制台", True)]:
                self.plugin.app.settings.data["command_prefix"] = prefix
                event = fixture.FakeEvent(trigger + " " + command, ai=ai)
                self.assertTrue(metadata.event_filters[0].filter(event, {}))
                self.assertEqual(event.get_extra("parsed_params"), {"args": command})
                await metadata.handler(self.plugin, event, **event.get_extra("parsed_params"))
                self.plugin._execute_action.assert_awaited_with("rcon", event, command)

        # The global wake prefix may already have been removed before this filter.
        self.plugin.app.settings.data["command_prefix"] = "mc"
        command = 'tellraw @a {"text":"hello  {player}"}'
        event = fixture.FakeEvent("控制台 " + command, ai=True)
        event.message_obj.message_str = "mc控制台 " + command
        self.assertTrue(metadata.event_filters[0].filter(event, {"wake_prefix": ["mc"]}))
        self.assertEqual(event.get_extra("parsed_params"), {"args": command})
        self.assertEqual(event.message_str, "控制台 " + command)

    async def test_http_rejects_missing_auth_and_stale_handler_identity(self):
        full_name = self.bridge.full_name("durable-id")
        response = await self.client.patch(self.path(full_name), json={"alias": "新名称"}, headers={"Authorization": ""})
        self.assertEqual(response.status_code, 401)
        response = await self.client.patch(self.path(full_name + "_stale"), json={"alias": "新名称"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("不存在", response.json()["message"])


if __name__ == "__main__":
    unittest.main()
