"""Routing must not capture ordinary chat or expose private purchase rewards."""
import sys
import types
import unittest
from pathlib import Path

PACKAGE = "checkin_points_routing_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[PACKAGE] = package
from checkin_points_routing_tests.core import DEFAULT_TEMPLATES, TEMPLATE_FIELDS, validate_settings
from checkin_points_routing_tests.routing import COMMANDS, parse_command, render_reply


class RoutingTests(unittest.TestCase):
    def test_commands_and_arguments_are_preserved(self):
        for command in COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(parse_command(command, "", True), (command, []))
        self.assertEqual(parse_command("商店 购买 42", "", True), ("商店", ["购买", "42"]))
        self.assertEqual(parse_command("修改 @123 100", "", True), ("修改", ["@123", "100"]))
        self.assertEqual(parse_command("签到 多余参数", "", True), ("签到", ["多余参数"]))

    def test_unwoken_commands_are_not_captured(self):
        for command in COMMANDS - {"兑换"}:
            self.assertIsNone(parse_command(command, "", False))
            self.assertIsNone(parse_command("cp " + command, "cp", False))
        self.assertIsNone(parse_command("cp 兑换", "cp", False))

    def test_only_bare_confirmation_bypasses_wake_and_prefix(self):
        for prefix in ("", "cp"):
            for woken in (True, False):
                self.assertEqual(parse_command(" \t兑换\n", prefix, woken), ("兑换", []))
        for message in ("兑换 1", "请兑换", "兑换吧", "兑换码", "我想兑换"):
            self.assertIsNone(parse_command(message, "cp", False))
        self.assertEqual(parse_command("cp 兑换", "cp", True), ("兑换", []))

    def test_prefix_is_a_complete_token(self):
        self.assertEqual(parse_command(" cp \t 签到 \n", "cp", True), ("签到", []))
        for message in ("签到", "cp签到", "cp2 签到", "other 签到", "cp", "cp cp 签到"):
            self.assertIsNone(parse_command(message, "cp", True))
        self.assertIsNone(parse_command("cp 签到", "", True))

    def test_aliases_and_whitespace(self):
        self.assertEqual(parse_command("cp CHECKIN", "cp", True), ("签到", []))
        self.assertEqual(parse_command(" \t points \t", "", True), ("积分", []))
        self.assertEqual(parse_command("cp\u3000商店\t购买\n7", "cp", True), ("商店", ["购买", "7"]))

    def test_host_wake_is_never_removed_twice(self):
        self.assertIsNone(parse_command("#cp 签到", "cp", True))
        self.assertIsNone(parse_command("/签到", "", True))

    def test_ai_and_ordinary_chat_pass_through(self):
        for message in ("chat 你好", "chat 签到", "cp chat 你好", "你好", "日常", "", "   "):
            for prefix in ("", "cp"):
                self.assertIsNone(parse_command(message, prefix, True))


class ReplyTests(unittest.TestCase):
    def test_all_known_fields_are_initialized(self):
        template = "|".join("{" + key + "}" for key in sorted(TEMPLATE_FIELDS))
        settings = {"currency_name": "星币", "templates": {"balance": template}}
        result = render_reply(settings, "balance", total=15)
        self.assertIn("星币", result)
        self.assertIn("15", result)
        self.assertNotIn("None", result)

    def test_reward_never_appears_in_any_public_template(self):
        for key in DEFAULT_TEMPLATES:
            if key == "purchase_private":
                continue
            settings = {"templates": {key: "群回复：{reward} {total}"}}
            with self.subTest(key=key):
                self.assertEqual(render_reply(settings, key, reward="SECRET-CODE", total=42), "群回复： 42")

    def test_private_reward_preserves_literal_content(self):
        secret = "CODE-{player}-{{raw}}\nhttps://example.test/a?x=1&y=2"
        settings = {"templates": {"purchase_private": "{reward}"}}
        self.assertEqual(render_reply(settings, "purchase_private", reward=secret), secret)

    def test_fallback_and_currency(self):
        self.assertEqual(render_reply({"currency_name": "星币"}, "balance", total=3), "你的余额：3 星币。")
        self.assertEqual(render_reply({}, "balance", total=3), "你的余额：3 积分。")

    def test_oversized_reward_fails_instead_of_truncating(self):
        settings = {"templates": {"purchase_private": "{reward}"}}
        reward = "x" * 20_000
        self.assertEqual(render_reply(settings, "purchase_private", reward=reward), reward)
        with self.assertRaises(ValueError):
            render_reply(settings, "purchase_private", reward=reward + "x")

    def test_validation_blocks_attribute_access_before_render(self):
        for template in ("{player.__class__}", "{player[0]}", "{reward!r}", "{total:100}"):
            with self.subTest(template=template), self.assertRaises(ValueError):
                validate_settings({"templates": {"balance": template}})


if __name__ == "__main__":
    unittest.main()
