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
from checkin_points_command_tests.core import TEMPLATE_FIELDS, classify_mc_output, validate_settings
from checkin_points_command_tests.routing import COMMANDS, parse_command, render_reply
from checkin_points_command_tests.store import Store

# Compile actual classes, replacing only framework bases/decorators. All command,
# binding, dispatch, permission and delivery method bodies remain unchanged.
tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
           and node.name in {"PointsCommandFilter", "CheckinPointsPlugin"}]
for cls in classes:
    cls.bases = [ast.Name(id="object", ctx=ast.Load())]
    for method in cls.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method.decorator_list = [decorator for decorator in method.decorator_list
                                     if isinstance(decorator, ast.Name) and decorator.id == "staticmethod"]
module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                         *classes], type_ignores=[])
namespace = dict(asyncio=asyncio, contextlib=contextlib, json=json, re=re, Path=Path,
                 logger=logging.getLogger("command-test"), NAME="checkin-test", MC_NAME="mc-test",
                 COMMANDS=COMMANDS, TEMPLATE_FIELDS=TEMPLATE_FIELDS, Store=Store,
                 parse_command=parse_command, render_reply=render_reply,
                 classify_mc_output=classify_mc_output, validate_settings=validate_settings)
exec(compile(ast.fix_missing_locations(module), "actual-entry-under-test", "exec"), namespace)
Plugin = namespace["CheckinPointsPlugin"]
EntryFilter = namespace["PointsCommandFilter"]


class Event:
    def __init__(self, text, bot, *, session="mybot:GroupMessage:123456789", user="100",
                 admin=False, woken=True, components=None):
        self.text, self.bot = text, bot
        self.unified_msg_origin = session
        self.user, self.admin = user, admin
        self.is_at_or_wake_command = woken
        self.components = components or []
        self.stopped = False

    def get_message_str(self):
        return self.text

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

    async def run_message(self, text, **kwargs):
        event = Event(text, self.bot, **kwargs)
        if not EntryFilter().filter(event, {}):
            return event, []
        return event, [reply async for reply in self.plugin.on_command(event)]

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
        for text, woken in (("cp 签到", False), ("签到", True), ("cp签到", True),
                             ("wrong 签到", True), ("chat 你好", True), ("cp chat 你好", True),
                             ("#cp 签到", True), ("你好", False)):
            with self.subTest(text=text, woken=woken):
                event, replies = await self.run_message(text, woken=woken)
                self.assertEqual(replies, [])
                self.assertFalse(event.stopped)
        event, replies = await self.run_message("cp 签到")
        self.assertTrue(event.stopped)
        self.assertIn("签到成功", replies[0])
        self.assertEqual(self.store.balance(self.session, "100"), 11)

    async def test_uppercase_english_alias_survives_entry_filter(self):
        _, replies = await self.run_message("cp CHECKIN")
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.store.balance(self.session, "100"), 11)

    async def test_mc_failure_keeps_balance_and_returns_actual_response(self):
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"])
        self.mc_results = [("§c玩家 Steve 今天已经签到了，请不要重复签到。", "queqiao")]
        _, replies = await self.run_message("cp 签到")
        self.assertNotIn("签到成功", replies[0])
        self.assertIn("今天已经签到了", replies[0])
        self.assertNotIn("§c", replies[0])
        self.assertEqual(self.store.balance(self.session, "100"), 0)
        self.assertEqual(self.mc_calls, ["litesignin click Steve"])
        await self.run_message("cp 签到")
        self.assertEqual(len(self.mc_calls), 1)

    async def test_mc_empty_response_is_unknown_not_credit(self):
        self.configure(mc_enabled=True, server_name="Server", commands=["litesignin click {player}"])
        self.mc_results = [("", "direct")]
        _, replies = await self.run_message("cp 签到")
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
        for message in ("cp 签到", "cp 积分", "cp 商店列表", "兑换"):
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
        event, replies = await self.run_message("cp 签到")
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
        _, first = await self.run_message("cp 签到")
        _, duplicate = await self.run_message("cp 签到")
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


if __name__ == "__main__":
    unittest.main()
