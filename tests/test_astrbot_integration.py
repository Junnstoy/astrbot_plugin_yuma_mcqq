"""Real AstrBot 4.29 contract checks; skipped in core-only environments.

No running AstrBot instance, production database, QQ account or server is used.
Framework imports and command configuration use isolated temporary storage.
"""

from __future__ import annotations

import importlib.util
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


HAS_ASTRBOT = importlib.util.find_spec("astrbot") is not None
TEST_WORK = Path(__file__).resolve().parents[1] / "work" / "astrbot-integration"
if HAS_ASTRBOT:
    TEST_WORK.mkdir(parents=True, exist_ok=True)
ISOLATED_ROOT = tempfile.TemporaryDirectory(prefix="yuma-astrbot-tests-", dir=TEST_WORK) if HAS_ASTRBOT else None
if HAS_ASTRBOT:
    # Set before importing astrbot.core: its own config path is initialized at
    # import time. Never point the tests at the installed production root.
    os.environ["ASTRBOT_ROOT"] = ISOLATED_ROOT.name
    from adapters import astrbot_commands as integration
    from astrbot.core.db.sqlite import SQLiteDatabase
    from astrbot.core.star import command_management
    from astrbot.core.star.star import StarMetadata, star_map
    from astrbot.core.star.star_handler import star_handlers_registry


class Preferences:
    def __init__(self):
        self.data = {}

    async def global_get(self, key, default=None):
        return self.data.get(key, default)

    async def global_put(self, key, value):
        self.data[key] = value


class FakeEvent:
    def __init__(self, text="", *, ai=False, user="10001", umo="bot:GroupMessage:12345"):
        self.message_str = text
        self.unified_msg_origin = umo
        self.message_obj = types.SimpleNamespace(message_id="message-1")
        self.is_at_or_wake_command = ai
        self.is_wake = True
        self.call_llm = False
        self._has_send_oper = False
        self._stopped = False
        self._extras = {}
        self.user = user
        self.bot = types.SimpleNamespace(call_action=AsyncMock(return_value={"message_id": 89}))

    def get_message_str(self):
        return self.message_str

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        return self._extras if key is None else self._extras.get(key, default)

    def get_sender_id(self):
        return self.user

    def get_group_id(self):
        return "12345"

    def get_self_id(self):
        return "99999"

    def get_platform_id(self):
        return "bot"

    def get_platform_name(self):
        return "aiocqhttp"

    def is_admin(self):
        return False

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped


@unittest.skipUnless(HAS_ASTRBOT, "requires AstrBot 4.29 runtime")
class CommandRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yuma-command-registry-", dir=TEST_WORK)
        self.db = SQLiteDatabase(str(Path(self.temp.name) / "commands.db"))
        await self.db.initialize()
        self.preferences = Preferences()
        self.database_patch = patch.object(command_management, "db_helper", self.db)
        self.preferences_patch = patch.object(command_management, "sp", self.preferences)
        self.database_patch.start()
        self.preferences_patch.start()
        real_framework = integration._framework

        def isolated_framework():
            api = real_framework()
            api.sp = self.preferences
            return api

        self.framework_patch = patch.object(integration, "_framework", isolated_framework)
        self.framework_patch.start()
        self.module = "data.plugins.yuma_command_test.main"
        self.plugin_class = type("TestPlugin", (), {"__module__": self.module})
        self.plugin = self.plugin_class()
        self.plugin._execute_action = AsyncMock()
        star_map[self.module] = StarMetadata(
            name="yuma_command_test", module_path=self.module,
            star_cls=self.plugin, activated=True,
        )
        self.bridge = integration.CommandRegistryBridge(self.plugin)
        self.definitions = [{"id": "durable-id", "name": "测试命令甲", "aliases": ["测试别名甲"],
                             "description": "test", "enabled": True}]

    async def asyncTearDown(self):
        self.bridge.close()
        for metadata in list(star_handlers_registry):
            if metadata.handler_module_path == self.module:
                star_handlers_registry.remove(metadata)
        star_map.pop(self.module, None)
        self.framework_patch.stop()
        self.preferences_patch.stop()
        self.database_patch.stop()
        await self.db.engine.dispose()
        self.temp.cleanup()

    async def test_registered_handler_uses_framework_parser_and_bound_instance(self):
        await self.bridge.reconcile(self.definitions)
        full_name = self.bridge.full_name("durable-id")
        metadata = star_handlers_registry.get_handler_by_full_name(full_name)
        self.assertIn(full_name, star_map[self.module].star_handler_full_names)
        event = FakeEvent("测试命令甲 first second", ai=True)
        command_filter = metadata.event_filters[0]
        self.assertTrue(command_filter.filter(event, {}))
        self.assertEqual(event.get_extra("parsed_params"), {"args": "first second"})
        await metadata.handler(event, **event.get_extra("parsed_params"))
        self.plugin._execute_action.assert_awaited_once_with("custom:durable-id", event, "first second")
        empty = FakeEvent("测试命令甲", ai=True)
        self.assertTrue(command_filter.filter(empty, {}))
        self.assertEqual(empty.get_extra("parsed_params"), {"args": ""})

    async def test_management_rename_disable_alias_and_permission_survive_reload(self):
        await self.bridge.reconcile(self.definitions)
        full_name = self.bridge.full_name("durable-id")
        await command_management.rename_command(full_name, "修改后命令", ["修改后别名"])
        await command_management.toggle_command(full_name, False)
        await command_management.update_command_permission(full_name, "admin")
        self.bridge.close()
        self.assertIsNotNone(await self.db.get_command_config(full_name))
        self.bridge = integration.CommandRegistryBridge(self.plugin)
        await self.bridge.reconcile([{**self.definitions[0], "name": "新的默认名称"}])
        metadata = star_handlers_registry.get_handler_by_full_name(full_name)
        self.assertFalse(metadata.enabled)
        command_filter = metadata.event_filters[0]
        self.assertEqual(command_filter.command_name, "修改后命令")
        self.assertEqual(command_filter.alias, {"修改后别名"})
        self.assertEqual(metadata.event_filters[1].permission_type.name, "ADMIN")
        records = await self.bridge.effective_commands()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["effective_command"], "修改后命令")

    async def test_duplicate_name_rejected_without_disturbing_live_handlers(self):
        await self.bridge.reconcile(self.definitions)
        full_name = self.bridge.full_name("durable-id")
        original = star_handlers_registry.get_handler_by_full_name(full_name)
        with self.assertRaisesRegex(ValueError, "冲突"):
            await self.bridge.reconcile(self.definitions + [{"id": "second", "name": "测试命令甲"}])
        self.assertIs(star_handlers_registry.get_handler_by_full_name(full_name), original)

    async def test_delete_cleans_handler_and_saved_management_override(self):
        await self.bridge.reconcile(self.definitions)
        full_name = self.bridge.full_name("durable-id")
        await command_management.rename_command(full_name, "修改后命令")
        await self.bridge.reconcile([])
        self.assertIsNone(star_handlers_registry.get_handler_by_full_name(full_name))
        self.assertIsNone(await self.db.get_command_config(full_name))

    async def test_new_definition_cannot_shadow_a_management_renamed_custom_command(self):
        await self.bridge.reconcile(self.definitions)
        await command_management.rename_command(self.bridge.full_name("durable-id"), "管理行为改名", ["管理行为别名"])
        for occupied in ("管理行为改名", "管理行为别名"):
            with self.assertRaisesRegex(ValueError, "冲突"):
                self.bridge.validate(self.definitions + [{"id": "new-action", "name": occupied}])

    async def test_reconcile_is_idempotent(self):
        await self.bridge.reconcile(self.definitions)
        await self.bridge.reconcile(self.definitions)
        names = [m.handler_full_name for m in star_handlers_registry]
        self.assertEqual(names.count(self.bridge.full_name("durable-id")), 1)

    async def test_cancelled_reconcile_restores_original_handlers(self):
        await self.bridge.reconcile(self.definitions)
        full_name = self.bridge.full_name("durable-id")
        original = star_handlers_registry.get_handler_by_full_name(full_name)
        names = list(star_map[self.module].star_handler_full_names)
        with patch.object(command_management, "sync_command_configs", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.bridge.reconcile([{**self.definitions[0], "name": "待回退名称"}])
        self.assertIs(star_handlers_registry.get_handler_by_full_name(full_name), original)
        self.assertEqual(star_map[self.module].star_handler_full_names, names)


@unittest.skipUnless(HAS_ASTRBOT, "requires AstrBot 4.29 runtime")
class MainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.package_name = "yuma_framework_fixture"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(root)]
        sys.modules[cls.package_name] = package
        application_stub = types.ModuleType(cls.package_name + ".services.application")
        application_stub.Application = object
        sys.modules[application_stub.__name__] = application_stub
        spec = importlib.util.spec_from_file_location(cls.package_name + ".main", root / "main.py")
        cls.main = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.main
        spec.loader.exec_module(cls.main)

    @classmethod
    def tearDownClass(cls):
        for handler in list(star_handlers_registry):
            if handler.handler_module_path.startswith(cls.package_name + "."):
                star_handlers_registry.remove(handler)
        for name in list(star_map):
            if name.startswith(cls.package_name + "."):
                star_map.pop(name)
        for name in list(sys.modules):
            if name == cls.package_name or name.startswith(cls.package_name + "."):
                sys.modules.pop(name, None)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yuma-main-test-", dir=TEST_WORK)
        self.plugin = object.__new__(self.main.YumaMCQQPlugin)
        self.plugin.data_dir = Path(self.temp.name)
        self.plugin._ready = True
        self.plugin._closing = False
        self.plugin._shutdown_task = None
        self.plugin._settings_lock = __import__("asyncio").Lock()
        self.route = {"scope": "community", "server_id": "paper", "admins": ["10001"],
                      "enabled": True, "qq_to_mc": True}
        self.plugin.app = types.SimpleNamespace(
            settings=types.SimpleNamespace(data={"enabled": True}, route=lambda umo: self.route),
            execute=AsyncMock(return_value={"text": "完成"}),
            relay_from_qq=AsyncMock(),
            web=AsyncMock(return_value={"saved": True}),
            snapshot=lambda: {"settings": {"servers": []}},
        )
        self.plugin.context = types.SimpleNamespace(
            send_message=AsyncMock(return_value=True), get_config=lambda umo: {"wake_prefix": ["/"]},
            registered_web_apis=[],
        )
        self.plugin.commands = types.SimpleNamespace(
            effective_commands=AsyncMock(return_value=[]), reconcile=AsyncMock(), validate=lambda value: None,
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_every_builtin_is_a_distinct_real_framework_command(self):
        handlers = star_handlers_registry.get_handlers_by_module_name(self.main.__name__)
        registered = [h for h in handlers if h.handler_name.startswith("cmd_")]
        self.assertEqual(len(registered), len(integration.BUILTINS))
        self.assertEqual(len({h.handler_full_name for h in registered}), len(registered))
        names = {h.event_filters[0].command_name for h in registered}
        self.assertIn("签到", names)
        self.assertIn("点数商店", names)
        self.assertIn("确认兑换", names)
        self.assertIn("cmd", names)
        self.assertEqual(len(registered), 23)
        self.assertNotIn("打卡", names)

    async def test_relay_runs_only_after_commands_and_ai_signals_are_excluded(self):
        ordinary = FakeEvent("普通聊天")
        await self.plugin.ordinary_message(ordinary)
        self.plugin.app.relay_from_qq.assert_awaited_once()
        for event in [FakeEvent("AI聊天", ai=True), FakeEvent("已处理")]:
            if event.message_str == "已处理":
                event.set_extra(self.main.CONSUMED_KEY, True)
            await self.plugin.ordinary_message(event)
        self.assertEqual(self.plugin.app.relay_from_qq.await_count, 1)

    async def test_async_reply_uses_original_umo_and_command_is_consumed(self):
        event = FakeEvent("签到", ai=True)

        async def execute(action, ctx, args):
            event.unified_msg_origin = "different:GroupMessage:999"
            self.assertEqual(ctx["scope"], "community")
            return {"text": "签到完成"}

        self.plugin.app.execute.side_effect = execute
        await self.plugin._execute_action("signin", event, "")
        self.assertTrue(event.is_stopped())
        self.assertTrue(event.get_extra(self.main.CONSUMED_KEY))
        self.assertEqual(self.plugin.context.send_message.await_args.args[0], "bot:GroupMessage:12345")

    async def test_private_delivery_requires_real_onebot_message_id(self):
        event = FakeEvent()
        callback = self.plugin._context_for_event(event)["reply_private"]
        self.assertTrue(await callback("123", "reward"))
        event.bot.call_action.return_value = {"status": "ok", "data": {}}
        self.assertFalse(await callback("123", "reward"))
        event.bot.call_action.return_value = {"status": "failed", "data": {"message_id": 2}}
        self.assertFalse(await callback("123", "reward"))
        self.assertFalse(await callback("not-a-qq-id", "reward"))

    async def test_confirmation_uses_current_command_name_and_session_wake_prefix(self):
        self.plugin.commands.effective_commands.return_value = [
            {"handler_name": "cmd_confirm", "effective_command": "领取确认", "enabled": True},
        ]
        await self.plugin._execute_action("purchase", FakeEvent(), "gift 1")
        ctx = self.plugin.app.execute.await_args.args[1]
        self.assertEqual(ctx["confirmation_command"], "/领取确认")

    async def test_help_uses_current_names_and_hides_unavailable_actions(self):
        self.plugin.commands.effective_commands.return_value = [
            {"handler_name": "cmd_confirm", "effective_command": "领取确认", "enabled": True},
            {"handler_name": "cmd_points_add", "effective_command": "加点", "enabled": True},
            {"handler_name": "cmd_tps", "effective_command": "服务器性能", "enabled": False},
        ]
        await self.plugin._execute_action("help", FakeEvent(user="20002"), "")
        self.plugin.app.execute.assert_not_awaited()
        from PIL import Image as PillowImage
        component = self.plugin.context.send_message.await_args.args[1].chain[0]
        self.assertIsInstance(component, self.main.Image)
        with PillowImage.open(next((self.plugin.data_dir / "help").glob("*.png"))) as image:
            message = image.info["help_text"]
        self.assertIn("/领取确认", message)
        self.assertNotIn("加点", message)
        self.assertNotIn("服务器性能", message)

    async def test_help_image_switch_and_render_failure_keep_complete_text(self):
        self.plugin.commands.effective_commands.return_value = [
            {"handler_name": "cmd_confirm", "effective_command": "领取确认", "aliases": ["领奖"], "description": "完整命令说明"},
        ]
        for image_enabled in (False, True):
            with self.subTest(image_enabled=image_enabled):
                self.plugin.app.settings.data["help"] = {"image_enabled": image_enabled}
                with patch.object(self.main, "render_help_image", side_effect=RuntimeError("没有中文字体")) as render:
                    await self.plugin._execute_action("help", FakeEvent(user="20002"), "")
                    self.assertEqual(render.call_count, int(image_enabled))
                message = self.plugin.context.send_message.await_args.args[1].chain[0].text
                self.assertIn("/领取确认", message)
                self.assertIn("/领奖", message)
                self.assertIn("完整命令说明", message)

    async def test_help_sends_one_complete_image_to_captured_umo(self):
        from PIL import Image as PillowImage
        self.plugin.commands.effective_commands.return_value = [
            {"handler_name": "cmd_" + action, "effective_command": name, "description": description}
            for action, name, description in integration.BUILTINS
        ]
        event = FakeEvent(user="10001")
        original_renderer = self.main.render_help_image
        def render(*args, **kwargs):
            event.unified_msg_origin = "different:GroupMessage:999"
            return original_renderer(*args, **kwargs)
        with patch.object(self.main, "render_help_image", side_effect=render):
            await self.plugin._execute_action("help", event, "")
        self.plugin.context.send_message.assert_awaited_once()
        call = self.plugin.context.send_message.await_args
        self.assertEqual(call.args[0], "bot:GroupMessage:12345")
        self.assertEqual(len(call.args[1].chain), 1)
        self.assertIsInstance(call.args[1].chain[0], self.main.Image)
        paths = list((self.plugin.data_dir / "help").glob("*.png"))
        self.assertEqual(len(paths), 1)
        with PillowImage.open(paths[0]) as image:
            for _, name, _ in integration.BUILTINS:
                self.assertIn("/" + name, image.info["help_text"])
            self.assertGreater(image.height, 500)

    async def test_help_image_delivery_failure_falls_back_without_resending_image(self):
        from PIL import Image as PillowImage
        path = self.plugin.data_dir / "single-help.png"
        with PillowImage.new("RGB", (8, 8), "white") as image:
            image.save(path)
        self.plugin.context.send_message.side_effect = [False, True]
        with patch.object(self.main, "render_help_image", return_value=str(path)):
            await self.plugin._execute_action("help", FakeEvent(user="20002"), "")
        calls = self.plugin.context.send_message.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertIsInstance(calls[0].args[1].chain[0], self.main.Image)
        self.assertIsInstance(calls[1].args[1].chain[0], self.main.Plain)
        self.assertTrue(all(call.args[0] == "bot:GroupMessage:12345" for call in calls))

    async def test_admin_actions_are_rechecked_in_business_entry(self):
        event = FakeEvent(user="20002")
        await self.plugin._execute_action("points_add", event, "10001 10")
        self.plugin.app.execute.assert_not_awaited()
        self.assertTrue(event.is_stopped())
        allowed = FakeEvent(user="10001")
        await self.plugin._execute_action("points_add", allowed, "10001 10")
        self.plugin.app.execute.assert_awaited_once()

    async def test_raw_rcon_is_admin_only_in_main_and_retains_literal_arguments(self):
        command = 'tellraw @a {"text":"hello  {player}"}'
        denied = FakeEvent(user="20002")
        await self.plugin._execute_action("rcon", denied, command)
        self.plugin.app.execute.assert_not_awaited()
        self.assertTrue(denied.is_stopped())
        allowed = FakeEvent(user="10001")
        await self.plugin._execute_action("rcon", allowed, command)
        self.plugin.app.execute.assert_awaited_once()
        action, ctx, args = self.plugin.app.execute.await_args.args
        self.assertEqual(action, "rcon")
        self.assertTrue(ctx["is_admin"])
        self.assertEqual(args, command)

    async def test_gui_refuses_missing_dashboard_identity(self):
        with patch.object(self.main, "request", types.SimpleNamespace(username=None)):
            response = await self.plugin.api_state()
        self.assertEqual(response.status_code, 403)

    async def test_gui_returns_raw_state_and_effective_commands(self):
        with patch.object(self.main, "request", types.SimpleNamespace(username="test-admin")):
            response = await self.plugin.api_state()
        result = json.loads(response.body)
        self.assertIn("settings", result)
        self.assertIn("effective_commands", result)

    async def test_nested_settings_conflict_is_rejected_before_saving(self):
        self.plugin.commands.validate = Mock(side_effect=ValueError("冲突"))
        web_request = types.SimpleNamespace(
            username="test-admin", json=AsyncMock(return_value={"revision": "v1", "settings": {"commands": []}}),
        )
        with patch.object(self.main, "request", web_request):
            response = await self.plugin.api_settings()
        self.assertEqual(response.status_code, 400)
        self.plugin.commands.validate.assert_called_once_with([])
        self.plugin.app.web.assert_not_awaited()

    async def test_registration_failure_restores_saved_configuration(self):
        previous = {"enabled": True, "commands": []}
        self.plugin.app.settings.data = previous.copy()
        self.plugin.commands.reconcile = AsyncMock(side_effect=RuntimeError("registry failure"))

        async def save(action, payload, actor):
            self.plugin.app.settings.data = payload["settings"].copy()
            return {"settings": self.plugin.app.settings.data}

        self.plugin.app.web.side_effect = save
        web_request = types.SimpleNamespace(
            username="test-admin", json=AsyncMock(return_value={"settings": {"enabled": True, "commands": [{"id": "new"}]}}),
        )
        with patch.object(self.main, "request", web_request), patch.object(self.main, "logger", Mock()):
            response = await self.plugin.api_settings()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.plugin.app.settings.data, previous)
        self.assertEqual(self.plugin.app.web.await_count, 2)

    async def test_cancelled_initialization_cleans_components_and_web_routes(self):
        self.plugin.app.start = AsyncMock(side_effect=asyncio.CancelledError)
        self.plugin.app.close = AsyncMock()
        self.plugin.commands.close = Mock()
        self.plugin.context.registered_web_apis = [("/owned", self.plugin.api_state, ["GET"], "")]
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.initialize()
        self.plugin.commands.close.assert_called_once()
        self.plugin.app.close.assert_awaited_once()
        self.assertEqual(self.plugin.context.registered_web_apis, [])

    async def test_cancelled_terminate_caller_does_not_abort_shared_cleanup(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def close_app():
            entered.set()
            await finish.wait()

        self.plugin.app.close = AsyncMock(side_effect=close_app)
        self.plugin.commands.shutdown = AsyncMock()
        first = asyncio.create_task(self.plugin.terminate())
        await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(self.plugin._shutdown_task.cancelled())
        finish.set()
        await self.plugin.terminate()
        self.plugin.app.close.assert_awaited_once()
        self.plugin.commands.shutdown.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
