"""Plugin-only prefixes retain AstrBot's real command management contract."""
from __future__ import annotations

import types
import unittest
from unittest.mock import Mock, patch

try:
    from . import test_astrbot_integration as fixture
except ImportError:
    import test_astrbot_integration as fixture


@unittest.skipUnless(fixture.HAS_ASTRBOT, "requires AstrBot 4.29 runtime")
class PrefixRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.CommandRegistryTests.asyncSetUp(self)
        self.plugin.app = types.SimpleNamespace(settings=types.SimpleNamespace(data={"command_prefix": "mc"}))
        await self.bridge.reconcile(self.definitions)
        self.metadata = fixture.star_handlers_registry.get_handler_by_full_name(self.bridge.full_name("durable-id"))
        self.filter = self.metadata.event_filters[0]

    async def asyncTearDown(self):
        await fixture.CommandRegistryTests.asyncTearDown(self)

    async def test_spaced_and_compact_prefix_match_without_global_wake(self):
        for message in ("mc测试命令甲 first second", "mc 测试命令甲 first second", "mc测试别名甲 first second"):
            event = fixture.FakeEvent(message, ai=False)
            self.assertTrue(self.filter.filter(event, {"wake_prefix": ["/"]}))
            self.assertEqual(event.get_extra("parsed_params"), {"args": "first second"})
            self.assertEqual(event.message_str, message)
            self.assertFalse(event.is_at_or_wake_command)
        await self.metadata.handler(event, **event.get_extra("parsed_params"))
        self.plugin._execute_action.assert_awaited_once_with("custom:durable-id", event, "first second")

    async def test_plugin_prefix_is_required_even_when_global_bot_was_awakened(self):
        for message in ("测试命令甲", "xmc测试命令甲", "mc测试命令甲suffix"):
            event = fixture.FakeEvent(message, ai=True)
            self.assertFalse(self.filter.filter(event, {}))
            self.assertEqual(event.message_str, message)
            self.assertTrue(event.is_at_or_wake_command)

    async def test_empty_prefix_restores_framework_wake_requirement_dynamically(self):
        self.plugin.app.settings.data["command_prefix"] = ""
        self.assertFalse(self.filter.filter(fixture.FakeEvent("测试命令甲", ai=False), {}))
        self.assertTrue(self.filter.filter(fixture.FakeEvent("测试命令甲", ai=True), {}))
        self.plugin.app.settings.data["command_prefix"] = "服"
        self.assertTrue(self.filter.filter(fixture.FakeEvent("服测试命令甲", ai=False), {}))

    async def test_prefix_already_stripped_by_global_waking_is_still_recognized(self):
        event = fixture.FakeEvent("测试命令甲 argument", ai=True)
        event.message_obj.message_str = "mc测试命令甲 argument"
        self.assertTrue(self.filter.filter(event, {"wake_prefix": ["mc"]}))
        self.assertEqual(event.message_str, "测试命令甲 argument")
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.get_extra("parsed_params"), {"args": "argument"})

    async def test_management_renames_aliases_and_disabling_still_apply(self):
        full_name = self.bridge.full_name("durable-id")
        await fixture.command_management.rename_command(full_name, "新触发名", ["新别名"])
        self.assertFalse(self.filter.filter(fixture.FakeEvent("mc测试命令甲"), {}))
        self.assertTrue(self.filter.filter(fixture.FakeEvent("mc新别名"), {}))
        records = await self.bridge.effective_commands()
        self.assertEqual(records[0]["effective_command"], "新触发名")
        await fixture.command_management.toggle_command(full_name, False)
        self.assertFalse(self.metadata.enabled)

    async def test_parse_error_cannot_leak_temporary_text_or_ai_wake_flag(self):
        event = fixture.FakeEvent("mc测试命令甲 invalid", ai=False)
        with patch.object(self.filter, "validate_and_convert_params", Mock(side_effect=ValueError("invalid argument"))):
            with self.assertRaises(ValueError):
                self.filter.filter(event, {})
        self.assertEqual(event.message_str, "mc测试命令甲 invalid")
        self.assertFalse(event.is_at_or_wake_command)

    async def test_all_builtins_keep_independent_management_filters(self):
        fixture.integration.install_builtin_commands(self.plugin_class)
        self.bridge = fixture.integration.CommandRegistryBridge(self.plugin)
        handlers = fixture.star_handlers_registry.get_handlers_by_module_name(self.module)
        builtins = [handler for handler in handlers if not handler.handler_name.startswith("cmd_custom_")]
        self.assertEqual(len(builtins), len(fixture.integration.BUILTINS))
        for handler in builtins:
            command = handler.event_filters[0]
            self.assertIsInstance(command, fixture.integration._framework().CommandFilter)
            self.assertTrue(command.filter(fixture.FakeEvent("mc " + command.command_name, ai=False), {}))
        self.assertIn("performance", {row[0] for row in fixture.integration.BUILTINS})


if __name__ == "__main__":
    unittest.main()
