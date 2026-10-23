"""Real entry methods and SQLite store with simulated chat/MC transports only."""
from __future__ import annotations

import ast
import asyncio
import copy
import contextlib
import json
import logging
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "checkin_points_command_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
from checkin_points_command_tests.core import CHECKIN_COMMANDS, TEMPLATE_FIELDS, classify_mc_output, validate_settings
from checkin_points_command_tests.routing import render_reply
from checkin_points_command_tests.store import Store


class NativeCommandFilter:
    """Small host boundary stub; session routing is compiled from the real file.

    Actual AstrBot registration, management persistence and parameter parsing
    are additionally exercised by the external native integration check.
    """

    def __init__(self, command_name, alias, handler_md):
        self.command_name, self.alias, self.handler_md = command_name, alias, handler_md
        self.custom_filter_list = []
        self._cmpl_cmd_names = None

    def get_complete_command_names(self):
        if self._cmpl_cmd_names is None:
            self._cmpl_cmd_names = [self.command_name, *self.alias]
        return self._cmpl_cmd_names

    def filter(self, event, cfg):
        if not event.is_at_or_wake_command:
            return False
        if not all(item.filter(event, cfg) for item in self.custom_filter_list):
            return False
        text = " ".join(event.get_message_str().split())
        matched = False
        # Keep the host's current alias-loop behavior so the wrapper's guard
        # against consuming an argument as a second alias is exercised.
        for name in self.get_complete_command_names():
            if text == name or text.startswith(name + " "):
                matched = True
                text = text[len(name):].strip()
        if matched:
            event.set_extra("parsed_params", {"args": text})
        return matched


registry = SimpleNamespace(handlers=[])
registry.get_handlers_by_module_name = lambda module: [
    handler for handler in registry.handlers if handler.handler_module_path == module]
filter_tree = ast.parse((ROOT / "command_filters.py").read_text(encoding="utf-8"))
filter_module = ast.Module(body=[node for node in filter_tree.body
                                if isinstance(node, (ast.ClassDef, ast.FunctionDef))], type_ignores=[])
filter_namespace = dict(CommandFilter=NativeCommandFilter, star_handlers_registry=registry,
                        CHECKIN_COMMANDS=CHECKIN_COMMANDS)
exec(compile(ast.fix_missing_locations(filter_module), "actual-command-filters-under-test", "exec"), filter_namespace)
install_command_filters = filter_namespace["install_command_filters"]

# Compile actual entry methods, replacing only the framework base/decorators.
# Registration specifications are read from those same native decorators.
tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
           and node.name == "CheckinPointsPlugin"]
command_specs = {}
for cls in classes:
    cls.bases = [ast.Name(id="object", ctx=ast.Load())]
    for method in cls.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in method.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == "command"):
                    options = {item.arg: ast.literal_eval(item.value) for item in decorator.keywords}
                    command_specs[method.name] = dict(name=ast.literal_eval(decorator.args[0]),
                        alias=options.get("alias", set()), priority=options.get("priority", 0))
            method.decorator_list = [decorator for decorator in method.decorator_list
                                     if isinstance(decorator, ast.Name) and decorator.id == "staticmethod"]
module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                         *classes], type_ignores=[])
namespace = dict(asyncio=asyncio, contextlib=contextlib, json=json, re=re, Path=Path,
                 logger=logging.getLogger("command-test"), NAME="checkin-test", MC_NAME="mc-test",
                 __name__=PACKAGE + ".entry", TEMPLATE_FIELDS=TEMPLATE_FIELDS, Store=Store,
                 CHECKIN_COMMANDS=CHECKIN_COMMANDS,
                 render_reply=render_reply, install_command_filters=install_command_filters,
                 classify_mc_output=classify_mc_output, validate_settings=validate_settings)
exec(compile(ast.fix_missing_locations(module), "actual-entry-under-test", "exec"), namespace)
Plugin = namespace["CheckinPointsPlugin"]


class Event:
    def __init__(self, text, bot, *, session="mybot:GroupMessage:123456789", user="100",
                 admin=False, woken=True, components=None):
        self.message_str, self.bot = text, bot
        self.unified_msg_origin = session
        self.user, self.admin = user, admin
        self.is_at_or_wake_command = woken
        self.components = components or []
        self.stopped = False
        self.extras = {}

    def get_message_str(self):
        return self.message_str

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_sender_id(self):
        return self.user

    def get_platform_id(self):
        return self.unified_msg_origin.split(":", 1)[0]

    def get_platform_name(self):
        return "aiocqhttp"

    def get_self_id(self):
        return "800"

    def get_messages(self):
        return self.components

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


class CommandTests(unittest.IsolatedAsyncioTestCase):
    session = "mybot:GroupMessage:123456789"
    other = "mybot:GroupMessage:987654321"

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "points.sqlite3")
        self.settings = validate_settings(dict(enabled=True, command_prefix="cp", admin_ids=["901"],
            min_points=11, max_points=11, currency_name="星币", products=[dict(
                id="card", name="兑换卡", price=30, reward_type="codes", codes=["SECRET-ONE", "SECRET-TWO"])]))
        self.store.save_settings(self.session, self.settings)
        self.store.save_settings(self.other, self.settings)
        self.bindings = {"mybot:User:100": "Steve", "mybot:User:200": "Alex"}
        self.mc_calls, self.private_calls = [], []
        self.mc_results, self.private_results = [], []

        async def mc_execute(command):
            self.mc_calls.append(command)
            result = self.mc_results.pop(0) if self.mc_results else ("成功给玩家 Steve 签到", "queqiao")
            if isinstance(result, BaseException):
                raise result
            return result

        async def private_send(**kwargs):
            self.private_calls.append(kwargs)
            result = self.private_results.pop(0) if self.private_results else {"message_id": 123}
            if isinstance(result, BaseException):
                raise result
            return result

        server = SimpleNamespace(execute_command_with_channel=mc_execute)
        mc = SimpleNamespace(binding_service=SimpleNamespace(all_bindings=lambda: self.bindings.copy()),
                             server_manager=SimpleNamespace(get=lambda name: server))
        self.bot = SimpleNamespace(send_private_msg=private_send)
        self.plugin = Plugin.__new__(Plugin)
        self.plugin.store, self.plugin.config = self.store, {"enabled": True}
        self.plugin._sync_lock = asyncio.Lock()
        self.plugin.context = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(
            star_cls=mc, activated=True))
        registry.handlers = []
        for name, spec in command_specs.items():
            handler = SimpleNamespace(handler_name=name, handler_module_path=Plugin.__module__,
                                      priority=spec["priority"], enabled=True)
            handler.event_filters = [NativeCommandFilter(spec["name"], spec["alias"].copy(), handler)]
            registry.handlers.append(handler)
        self.plugin._command_filters = install_command_filters(self.plugin, self.plugin._operations)
        await self.plugin.refresh_command_settings()

    async def run_message(self, text, **kwargs):
        # Direct store edits in tests simulate UI actions, which refresh this
        # cache in production before accepting the next message.
        await self.plugin.refresh_command_settings()
        event = Event(text, self.bot, **kwargs)
        replies = []
        for handler in sorted(registry.handlers, key=lambda item: item.priority, reverse=True):
            if not handler.enabled:
                continue
            original = event.message_str, event.is_at_or_wake_command
            matched = all(item.filter(event, {}) for item in handler.event_filters)
            self.assertEqual((event.message_str, event.is_at_or_wake_command), original)
            if matched:
                arguments = event.extras["parsed_params"]["args"]
                replies.extend([reply async for reply in getattr(self.plugin, handler.handler_name)(event, arguments)])
                if event.stopped:
                    break
        return event, replies

    def rename(self, canonical, name, aliases=()):
        entry = self.plugin._command_filters[canonical]
        entry.command_name, entry.alias, entry._cmpl_cmd_names = name, set(aliases), None

    def disable(self, canonical):
        self.plugin._command_filters[canonical].handler_md.enabled = False

    def configure(self, **changes):
        self.settings.update(changes)
        self.settings = validate_settings(self.settings)
        self.store.save_settings(self.session, self.settings)

    def fund(self, user="100", amount=100, session=None):
        self.store.adjust_balance(session or self.session, user, "set", amount, "900")

    async def test_gui_admin_and_global_admin_permissions_are_or(self):
        _, replies = await self.run_message("cp 修改 @200 100", user="901")
        self.assertIn("100", replies[0])
        self.assertEqual(self.store.balance(self.session, "200"), 100)
        await self.run_message("cp 增加 @200 10", user="900", admin=True)
        self.assertEqual(self.store.balance(self.session, "200"), 110)
        await self.run_message("cp 减少 @200 20", user="901")
        self.assertEqual(self.store.balance(self.session, "200"), 90)
        _, replies = await self.run_message("cp 查询 @200", user="901")
        self.assertIn("90", replies[0])

    async def test_member_cannot_modify_or_query_another_balance(self):
        self.fund("200")
        for command in ("修改 @200 999", "增加 @200 10", "减少 @200 10", "查询 @200"):
            event, replies = await self.run_message("cp " + command)
            self.assertTrue(event.stopped)
            self.assertIn("权限", replies[0])
            self.assertEqual(self.store.balance(self.session, "200"), 100)

    async def test_prefix_wake_and_ai_messages_not_swallowed(self):
        for text, woken in (("cp 签到", False), ("cp 签到", True), ("签到", False), ("cp签到", True),
                             ("wrong 签到", True), ("chat 你好", True), ("cp chat 你好", True),
                             ("#cp 签到", True), ("你好", False)):
            with self.subTest(text=text, woken=woken):
                event, replies = await self.run_message(text, woken=woken)
                self.assertEqual(replies, [])
                self.assertFalse(event.stopped)
        event, replies = await self.run_message("签到")
        self.assertTrue(event.stopped)
        self.assertIn("签到成功", replies[0])
        self.assertEqual(self.store.balance(self.session, "100"), 11)
        _, replies = await self.run_message("点数")
        self.assertIn("11 星币", replies[0])
        _, replies = await self.run_message("cp 点数")
        self.assertEqual(replies, [])

    async def test_uppercase_english_alias_survives_entry_filter(self):
        _, replies = await self.run_message("CHECKIN")
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.store.balance(self.session, "100"), 11)

    async def test_mc_failure_keeps_balance_and_returns_actual_response(self):
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"])
        self.mc_results = [("§c玩家 Steve 今天已经签到了，请不要重复签到。", "queqiao")]
        _, replies = await self.run_message("签到")
        self.assertNotIn("签到成功", replies[0])
        self.assertIn("今天已经签到了", replies[0])
        self.assertNotIn("§c", replies[0])
        self.assertEqual(self.store.balance(self.session, "100"), 0)
        self.assertEqual(self.mc_calls, ["litesignin click Steve"])
        await self.run_message("签到")
        self.assertEqual(len(self.mc_calls), 1)

    async def test_mc_empty_response_is_unknown_not_credit(self):
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"])
        self.mc_results = [("", "direct")]
        _, replies = await self.run_message("签到")
        self.assertIn("未确认", replies[0])
        self.assertEqual(self.store.balance(self.session, "100"), 0)

    async def test_shop_is_two_steps_and_private_destination_is_exact(self):
        self.fund()
        _, pending = await self.run_message("cp 商店 购买 card")
        self.assertEqual(self.store.balance(self.session, "100"), 100)
        self.assertEqual(self.private_calls, [])
        self.assertEqual(self.store.active_purchase(self.session, "100")["status"], "pending")
        event, confirmed = await self.run_message("兑换", woken=False)
        self.assertTrue(event.stopped)
        self.assertEqual(self.store.balance(self.session, "100"), 70)
        call = self.private_calls[0]
        self.assertEqual(set(call), {"user_id", "self_id", "message"})
        self.assertEqual((call["user_id"], call["self_id"]), (100, "800"))
        self.assertIn("SECRET-ONE", call["message"][0]["data"]["text"])
        for reply in pending + confirmed:
            self.assertNotIn("SECRET-", reply)
        event, replies = await self.run_message("兑换", woken=False)
        self.assertEqual(replies, [])
        self.assertFalse(event.stopped)
        self.assertEqual(len(self.private_calls), 1)

    async def test_another_user_or_session_cannot_confirm_purchase(self):
        self.fund()
        await self.run_message("cp 商店 购买 card")
        for kwargs in ({"user": "200"}, {"session": self.other}):
            event, replies = await self.run_message("兑换", woken=False, **kwargs)
            self.assertEqual(replies, [])
            self.assertFalse(event.stopped)
            _, replies = await self.run_message("cp 兑换", **kwargs)
            self.assertIn("没有待确认", replies[0])
        self.assertEqual(self.private_calls, [])
        self.assertEqual(self.store.balance(self.session, "100"), 100)

    async def test_private_failure_recovery_reuses_code_and_charge(self):
        self.fund()
        self.private_results = [RuntimeError("private disabled"), {"message_id": 555}]
        await self.run_message("cp 商店 购买 card")
        _, failed = await self.run_message("兑换", woken=False)
        self.assertIn("失败", failed[0])
        self.assertNotIn("SECRET-", failed[0])
        self.assertEqual(self.store.balance(self.session, "100"), 70)
        _, recovered = await self.run_message("cp 补领")
        self.assertNotIn("SECRET-", recovered[0])
        self.assertEqual(len(self.private_calls), 2)
        self.assertEqual(self.private_calls[0], self.private_calls[1])
        self.assertEqual(self.store.balance(self.session, "100"), 70)
        self.assertEqual(self.store.product_catalog(self.session)[0]["stock"], 1)
        self.assertEqual(self.store.orders(self.session)[0]["status"], "delivered")

    async def test_missing_message_id_does_not_claim_delivery(self):
        self.fund()
        self.private_results = [None]
        await self.run_message("cp 商店 购买 card")
        _, replies = await self.run_message("兑换", woken=False)
        self.assertNotIn("奖励已私聊发送", replies[0])
        self.assertEqual(self.store.orders(self.session)[0]["status"], "delivery_failed")

    async def test_deleted_session_does_not_reply_or_issue_reward(self):
        self.fund()
        await self.run_message("cp 商店 购买 card")
        self.store.delete_session(self.session)
        for message in ("签到", "点数", "cp 商店列表", "兑换"):
            event, replies = await self.run_message(message)
            self.assertEqual(replies, [])
            self.assertFalse(event.stopped)
        self.assertEqual(self.mc_calls, [])
        self.assertEqual(self.private_calls, [])

    async def test_deleted_session_can_recover_paid_reward_without_new_charge(self):
        self.fund()
        self.private_results = [RuntimeError("private disabled"), {"message_id": 777}]
        await self.run_message("cp 商店 购买 card")
        await self.run_message("兑换", woken=False)
        self.assertEqual(self.store.balance(self.session, "100"), 70)
        self.store.delete_session(self.session)
        event, replies = await self.run_message("签到")
        self.assertEqual(replies, [])
        self.assertFalse(event.stopped)
        event, replies = await self.run_message("兑换", woken=False)
        self.assertTrue(event.stopped)
        self.assertNotIn("SECRET-", replies[0])
        self.assertEqual(len(self.private_calls), 2)
        self.assertEqual(self.private_calls[0], self.private_calls[1])
        self.assertEqual(self.store.balance(self.session, "100"), 70)
        self.assertEqual(self.store.orders(self.session)[0]["status"], "delivered")

    async def test_stopped_session_cancels_pending_and_never_charges(self):
        self.fund()
        await self.run_message("cp 商店 购买 card")
        self.store.toggle_session(self.session, False)
        event, replies = await self.run_message("兑换", woken=False)
        self.assertFalse(event.stopped)
        self.assertEqual(replies, [])
        self.assertEqual(self.private_calls, [])
        self.assertEqual(self.store.balance(self.session, "100"), 100)

    async def test_successful_mc_credits_only_once_and_includes_response(self):
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"])
        _, first = await self.run_message("签到")
        _, duplicate = await self.run_message("签到")
        self.assertIn("签到成功", first[0])
        self.assertIn("成功给玩家 Steve", first[0])
        self.assertIn("不会重复", duplicate[0])
        self.assertEqual(self.store.balance(self.session, "100"), 11)
        self.assertEqual(self.mc_calls, ["litesignin click Steve"])

    async def test_public_templates_cannot_expose_reward(self):
        templates = copy.deepcopy(self.settings["templates"])
        templates["purchase_pending"] = "确认 {product_name} {reward}"
        templates["purchase_delivered"] = "已发货 {reward}"
        self.configure(templates=templates)
        self.fund()
        _, first = await self.run_message("cp 商店 购买 card")
        _, second = await self.run_message("兑换", woken=False)
        self.assertNotIn("SECRET-", "\n".join(first + second))
        self.assertIn("SECRET-ONE", self.private_calls[0]["message"][0]["data"]["text"])

    async def test_session_prefixes_are_independent(self):
        self.configure(checkin_prefix="qd")
        self.store.save_settings(self.other, {**self.settings, "command_prefix": "gp", "checkin_prefix": "mc"})
        for message, session in (("gp 商店", self.session), ("cp 商店", self.other),
                                 ("qd 签到", self.other), ("mc 签到", self.session)):
            event, replies = await self.run_message(message, session=session)
            self.assertEqual(replies, [])
            self.assertFalse(event.stopped)
        _, replies = await self.run_message("gp 商店 列表", session=self.other)
        self.assertIn("兑换卡", replies[0])
        _, replies = await self.run_message("mc 签到", session=self.other)
        self.assertIn("签到成功", replies[0])
        self.assertEqual(self.store.balance(self.session, "100"), 0)

    async def test_every_native_command_uses_only_its_group_prefix(self):
        self.configure(checkin_prefix="qd")
        await self.plugin.refresh_command_settings()
        expected_checkin = {"签到", "点数", "签到排行榜", "重置签到", "补签"}
        self.assertEqual(CHECKIN_COMMANDS, expected_checkin)
        for canonical, entry in self.plugin._command_filters.items():
            expected = "qd" if canonical in expected_checkin else "cp"
            other = "cp" if expected == "qd" else "qd"
            for text, accepted in ((expected + " " + entry.command_name, True),
                                   (other + " " + entry.command_name, False),
                                   (entry.command_name, False),
                                   (expected + entry.command_name, False)):
                with self.subTest(canonical=canonical, text=text):
                    self.assertEqual(entry.filter(Event(text, self.bot), {}), accepted)
            self.assertFalse(entry.filter(Event(expected + " " + entry.command_name, self.bot, woken=False), {}))
        self.assertEqual(self.mc_calls, [])
        self.assertEqual(self.private_calls, [])

    async def test_empty_prefixes_require_only_host_wake_for_both_groups(self):
        self.configure(command_prefix="", checkin_prefix="")
        self.fund()
        self.store.create_purchase(self.session, "100", "card")
        await self.plugin.refresh_command_settings()
        for canonical, entry in self.plugin._command_filters.items():
            with self.subTest(canonical=canonical):
                self.assertTrue(entry.filter(Event(entry.command_name, self.bot), {}))
                self.assertEqual(entry.filter(Event(entry.command_name, self.bot, woken=False), {}), canonical == "兑换")
                self.assertFalse(entry.filter(Event("cp " + entry.command_name, self.bot), {}))

    async def test_group_prefixes_survive_native_rename_alias_disable_and_help(self):
        self.configure(checkin_prefix="qd")
        self.rename("签到", "每日 签到", {"daily"})
        self.rename("点数", "余额", {"balance"})
        self.rename("购买", "购买商品", {"buy"})
        for text in ("签到", "qd 签到", "daily", "cp DAILY", "balance", "cp balance"):
            _, replies = await self.run_message(text)
            self.assertEqual(replies, [])
        _, first = await self.run_message("qd DAILY")
        self.assertIn("签到成功", first[0])
        _, points = await self.run_message("qd BALANCE")
        self.assertIn("11 星币", points[0])
        _, help_reply = await self.run_message("cp 帮助")
        self.assertIn("签到指令：", help_reply[0])
        self.assertIn("qd 每日 签到", help_reply[0])
        self.assertIn("qd 重置签到", help_reply[0])
        self.assertIn("qd 补签", help_reply[0])
        self.assertIn("点数管理与商店：", help_reply[0])
        self.assertIn("cp 购买商品", help_reply[0])
        self.assertNotIn("cp 每日 签到", help_reply[0])
        self.disable("签到")
        _, disabled = await self.run_message("qd DAILY")
        self.assertEqual(disabled, [])
        _, help_reply = await self.run_message("cp 帮助")
        self.assertNotIn("qd 每日 签到", help_reply[0])

    async def test_admin_checkin_commands_use_checkin_prefix_and_keep_permissions(self):
        self.configure(checkin_prefix="qd")
        await self.run_message("qd 签到")
        for text in ("cp 重置签到 Steve", "重置签到 Steve", "cp 补签 Alex 2000-01-02", "补签 Alex 2000-01-02"):
            _, replies = await self.run_message(text, user="901")
            self.assertEqual(replies, [])
        _, denied = await self.run_message("qd 重置签到 Steve")
        self.assertIn("权限", denied[0])
        _, reset = await self.run_message("qd 重置签到 Steve", user="901")
        self.assertIn("已重置 100", reset[0])
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"])
        self.mc_results = [("成功给玩家 Alex 签到", "queqiao")]
        _, makeup = await self.run_message("qd 补签 Alex 2000-01-02", user="900", admin=True)
        self.assertIn("已为 200 补签", makeup[0])
        self.assertEqual(self.store.balance(self.session, "200"), 11)

    async def test_command_name_equal_to_prefix_still_requires_prefix(self):
        self.configure(checkin_prefix="签到", mc_enabled=True, server_name="Server",
                       commands=["litesignin click {player}"])
        self.mc_results = [("1. Steve 10 次", "queqiao")]
        for message in ("签到 排行榜", "签到", "cp 签到 排行榜"):
            _, replies = await self.run_message(message)
            self.assertEqual(replies, [])
        _, ranking = await self.run_message("签到 签到 排行榜")
        self.assertEqual(ranking, ["1. Steve 10 次"])
        self.assertEqual(self.mc_calls, ["signin leaderboard"])
        _, replies = await self.run_message("签到 签到")
        self.assertIn("签到成功", replies[0])
        self.assertEqual(self.store.balance(self.session, "100"), 11)

    async def test_bare_confirmation_name_and_alias_can_start_with_session_prefix(self):
        self.fund()
        self.rename("兑换", "cp 确认", {"cp confirm"})
        for confirmation in ("cp 确认", "cp CONFIRM"):
            _, pending = await self.run_message("cp 商店 购买 card")
            self.assertIn("cp 确认", pending[0])
            event, confirmed = await self.run_message(confirmation, woken=False)
            self.assertTrue(event.stopped)
            self.assertIn("已完成", confirmed[0])
        self.assertEqual(self.store.balance(self.session, "100"), 40)
        self.assertEqual(len(self.private_calls), 2)
        self.assertNotEqual(self.private_calls[0], self.private_calls[1])

    async def test_renamed_checkin_name_can_start_with_required_prefix(self):
        self.configure(checkin_prefix="cp")
        self.rename("签到", "cp 日常")
        _, first = await self.run_message("cp 日常")
        self.assertEqual(first, [])
        _, second = await self.run_message("cp cp 日常")
        self.assertIn("签到成功", second[0])
        self.assertEqual(self.store.balance(self.session, "100"), 11)

    async def test_renames_and_disabled_handlers_have_no_legacy_route(self):
        self.rename("签到", "日常 签到", {"daily"})
        for message in ("签到", "cp 签到"):
            event, replies = await self.run_message(message)
            self.assertEqual(replies, [])
            self.assertFalse(event.stopped)
        _, replies = await self.run_message("DAILY")
        self.assertIn("签到成功", replies[0])
        self.disable("签到")
        for message in ("日常 签到", "cp daily"):
            _, replies = await self.run_message(message)
            self.assertEqual(replies, [])
        _, replies = await self.run_message("cp 帮助")
        self.assertNotIn("日常 签到", replies[0])

    async def test_bare_confirmation_respects_effective_name_and_disabled_flag(self):
        self.fund()
        self.rename("兑换", "确认 订单", {"confirm"})
        _, pending = await self.run_message("cp 商店 购买 card")
        self.assertIn("确认 订单", pending[0])
        _, replies = await self.run_message("兑换", woken=False)
        self.assertEqual(replies, [])
        self.assertEqual(self.private_calls, [])
        self.disable("兑换")
        for message in ("确认 订单", "confirm", "cp 确认 订单"):
            _, replies = await self.run_message(message, woken=message.startswith("cp "))
            self.assertEqual(replies, [])
        self.assertEqual(self.store.balance(self.session, "100"), 100)
        self.plugin._command_filters["兑换"].handler_md.enabled = True
        _, confirmed = await self.run_message("CONFIRM", woken=False)
        self.assertIn("已完成", confirmed[0])
        self.assertEqual(self.store.balance(self.session, "100"), 70)

    async def test_disabled_subcommands_do_not_fall_back_to_parent_commands(self):
        self.fund()
        self.disable("购买")
        self.disable("签到排行榜")
        for message in ("cp 商店 购买 card", "签到 排行榜", "cp 签到 排行榜"):
            event, replies = await self.run_message(message)
            self.assertEqual(replies, [])
            self.assertFalse(event.stopped)
        self.assertEqual(self.mc_calls, [])
        self.assertEqual(self.private_calls, [])
        self.assertEqual(self.store.balance(self.session, "100"), 100)
        self.assertIsNone(self.store.active_purchase(self.session, "100"))

    async def test_product_id_equal_to_alias_stays_an_argument(self):
        self.fund()
        self.rename("购买", "商店 购买", {"card"})
        _, pending = await self.run_message("cp 商店 购买 card")
        self.assertIn("兑换卡", pending[0])
        self.assertEqual(self.store.active_purchase(self.session, "100")["status"], "pending")
        self.assertEqual(set(self.plugin._command_filters["购买"].command_names()), {"商店 购买", "card"})

    async def test_filter_exception_restores_shared_event_and_current_aliases(self):
        entry = self.plugin._command_filters["购买"]
        original_names = entry.command_names()

        def fail(event, cfg):
            raise RuntimeError("simulated custom-filter error")

        entry.custom_filter_list.append(SimpleNamespace(filter=fail))
        event = Event("cp 商店 购买 card", self.bot)
        with self.assertRaisesRegex(RuntimeError, "custom-filter error"):
            entry.filter(event, {})
        self.assertEqual(event.message_str, "cp 商店 购买 card")
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(entry.command_names(), original_names)

    async def test_at_component_display_name_is_normalized_for_admin_target(self):
        At = type("At", (), {})
        mention = At()
        mention.qq, mention.name = "200", "测试昵称"
        _, replies = await self.run_message("cp 修改 @测试昵称(200) 50", user="901", components=[mention])
        self.assertIn("200", replies[0])
        self.assertEqual(self.store.balance(self.session, "200"), 50)
        # Some adapters omit the mention from message_str; the actual component
        # still identifies the intended account rather than the administrator.
        await self.run_message("cp 增加 5", user="901", components=[mention])
        self.assertEqual(self.store.balance(self.session, "200"), 55)
        self.assertEqual(self.store.balance(self.session, "901"), 0)

    async def test_reset_requires_admin_and_preserves_balance_and_history(self):
        await self.run_message("签到")
        original_id = self.store.history(self.session)[0]["id"]
        _, denied = await self.run_message("重置签到 Steve")
        self.assertIn("权限", denied[0])
        self.assertIsNotNone(self.store.get_checkin(original_id))
        _, reset = await self.run_message("重置签到 Steve", user="901")
        self.assertIn("已重置 100", reset[0])
        self.assertEqual(self.store.balance(self.session, "100"), 11)
        self.assertIsNone(self.store.get_checkin(original_id))
        self.assertIn("reset_at", self.store.history(self.session)[0])
        self.assertEqual(self.mc_calls, [])
        await self.run_message("签到")
        _, reset_again = await self.run_message("重置签到 @100", user="900", admin=True)
        self.assertIn("已重置 100", reset_again[0])
        self.assertEqual(self.store.balance(self.session, "100"), 22)

    async def test_makeup_requires_admin_and_valid_past_date(self):
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"])
        _, denied = await self.run_message("补签 Alex 2000-01-02")
        self.assertIn("权限", denied[0])
        for day in ("2000-02-30", "9999-12-31", "not-a-date"):
            _, invalid = await self.run_message("补签 Alex " + day, user="901")
            self.assertTrue(invalid)
            self.assertNotIn("已为 200 补签", invalid[0])
        self.assertEqual(self.mc_calls, [])
        self.assertEqual(self.store.history(self.session), [])
        self.assertEqual(self.store.balance(self.session, "200"), 0)

    async def test_makeup_mc_failure_returns_actual_response_and_never_credits(self):
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"])
        self.mc_results = [("§c玩家 Alex 已经签到，请不要重复签到。", "queqiao")]
        _, failed = await self.run_message("补签 Alex 2000-01-02", user="901")
        self.assertIn("请不要重复签到", failed[0])
        self.assertNotIn("§c", failed[0])
        self.assertNotIn("已为 200 补签", failed[0])
        self.assertEqual(self.store.balance(self.session, "200"), 0)
        self.assertEqual(self.mc_calls, ["signin click 2000-01-02 Alex"])
        await self.run_message("补签 Alex 2000-01-02", user="901")
        self.assertEqual(len(self.mc_calls), 1)
        self.assertEqual(self.store.balance(self.session, "200"), 0)

    async def test_makeup_success_uses_target_date_multiplier_and_credits_once(self):
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"],
                       date_rules=[dict(date="2000-01-03", multiplier=2)])
        self.mc_results = [("成功给玩家 Alex 签到", "queqiao")]
        _, success = await self.run_message("补签 Alex 2000-01-03", user="900", admin=True)
        self.assertIn("已为 200 补签 2000-01-03", success[0])
        self.assertIn("22 星币", success[0])
        _, duplicate = await self.run_message("补签 Alex 2000-01-03", user="901")
        self.assertIn("不会重复", duplicate[0])
        self.assertEqual(self.mc_calls, ["signin click 2000-01-03 Alex"])
        self.assertEqual(self.store.balance(self.session, "200"), 22)
        self.assertEqual(self.store.balance(self.session, "900"), 0)

    async def test_mc_ranking_executes_game_command_without_mutating_points(self):
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"])
        self.mc_results = [("§6签到排行榜\n§a1. Steve 10 次", "queqiao")]
        _, ranking = await self.run_message("签到 排行榜")
        self.assertEqual(ranking, ["签到排行榜\n1. Steve 10 次"])
        self.assertEqual(self.mc_calls, ["signin leaderboard"])
        self.assertEqual(self.store.balance(self.session, "100"), 0)
        self.assertEqual(self.store.history(self.session), [])
        _, not_woken = await self.run_message("签到 排行榜", woken=False)
        self.assertEqual(not_woken, [])
        self.assertEqual(len(self.mc_calls), 1)
        _, unprefixed_points_rank = await self.run_message("排行榜")
        self.assertEqual(unprefixed_points_rank, [])


if __name__ == "__main__":
    unittest.main()
