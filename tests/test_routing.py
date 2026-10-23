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
from checkin_points_routing_tests.routing import render_reply


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
        self.assertEqual(render_reply({}, "balance", total=3), "你的余额：3 点数。")

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
