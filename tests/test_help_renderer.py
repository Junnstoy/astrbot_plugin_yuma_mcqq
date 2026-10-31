"""Help permissions, editable stable IDs and real paginated Chinese PNG output."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import help_renderer as help_module


class HelpCatalogTests(unittest.TestCase):
    def setUp(self):
        self.effective = [
            {"handler_name": "cmd_signin", "effective_command": "今日签到", "original_command": "签到",
             "aliases": ["每日签", "每日签"], "description": "签到 玩家名"},
            {"handler_name": "cmd_binding", "effective_command": "绑定"},
            {"handler_name": "cmd_tps", "effective_command": "服务器性能", "enabled": False},
            {"handler_name": "cmd_rcon", "effective_command": "控制台", "permission": "admin"},
            {"handler_name": "cmd_custom_hash", "effective_command": "查余额", "aliases": ["金币查询"], "description": "查询说明"},
        ]
        self.customs = {"cmd_custom_hash": {"id": "lookup", "admin_only": False, "require_binding": True, "cooldown_seconds": 5}}
        self.preferences = {"entries": {"binding": {"visible": False}, "custom:lookup": {"order": -5},
                                         "signin": {"description": "字面说明：签到 玩家名"}}}

    def catalog(self):
        return help_module.help_catalog(self.effective, self.customs, self.preferences, {}, {"rcon"})

    def test_editor_catalog_keeps_hidden_disabled_admin_and_stable_custom_ids(self):
        catalog = self.catalog()
        by_id = {entry["action_id"]: entry for entry in catalog}
        self.assertEqual(len(catalog), 5)
        self.assertEqual(catalog[0]["action_id"], "custom:lookup")
        self.assertFalse(by_id["binding"]["visible"])
        self.assertFalse(by_id["tps"]["enabled"])
        self.assertTrue(by_id["rcon"]["admin_only"])
        self.assertEqual(by_id["signin"]["default_description"], "今日签到 玩家名")
        self.assertEqual(by_id["signin"]["description"], "字面说明：签到 玩家名")
        self.assertEqual(by_id["signin"]["aliases"], ["每日签"])

    def test_document_uses_effective_aliases_prefix_permissions_and_global_order(self):
        document = help_module.build_help_document(self.catalog(), is_admin=False, command_hint=lambda name: "mc " + name)
        text = document.as_text()
        self.assertIn("mc 查余额", text)
        self.assertIn("mc 金币查询", text)
        self.assertIn("mc 每日签", text)
        self.assertIn("需要绑定 MC 账号", text)
        self.assertIn("冷却：5 秒", text)
        self.assertLess(text.index("mc 查余额"), text.index("mc 今日签到"))
        for hidden in ("mc 绑定", "服务器性能", "控制台"):
            self.assertNotIn(hidden, text)
        admin_text = help_module.build_help_document(self.catalog(), is_admin=True, command_hint=lambda name: "/" + name).as_text()
        self.assertIn("/控制台", admin_text)
        self.assertIn("权限：管理员", admin_text)

    def test_custom_visibility_and_permission_cannot_enable_unavailable_action(self):
        self.preferences["entries"]["custom:lookup"] = {"visible": True, "order": 1}
        self.customs["cmd_custom_hash"]["admin_only"] = True
        text = help_module.build_help_document(self.catalog(), is_admin=False, command_hint=str).as_text()
        self.assertNotIn("查余额", text)
        self.customs["cmd_custom_hash"].update(admin_only=False, enabled=False)
        text = help_module.build_help_document(self.catalog(), is_admin=True, command_hint=str).as_text()
        self.assertNotIn("查余额", text)

    def test_equal_order_uses_default_order_in_catalog_and_document(self):
        self.effective.reverse()
        self.preferences["entries"] = {
            "binding": {"order": 1}, "signin": {"order": 1},
            "custom:lookup": {"order": 1}, "rcon": {"order": 1}, "tps": {"order": 1},
        }
        catalog = self.catalog()
        self.assertEqual([entry["action_id"] for entry in catalog],
                         ["signin", "binding", "tps", "rcon", "custom:lookup"])
        document = help_module.build_help_document(list(reversed(catalog)), is_admin=True, command_hint=str)
        self.assertEqual([command.name for section in document.sections for command in section.commands],
                         ["今日签到", "绑定", "控制台", "查余额"])


class HelpImageTests(unittest.TestCase):
    def test_long_chinese_help_is_paginated_without_truncating_content(self):
        try:
            font = help_module._font_path()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        from PIL import Image
        description = "完整说明与原始参数必须全部显示ABCDEFGHIJ" * 100 + "唯一结尾-END"
        document = help_module.HelpDocument("MCQQ 指令帮助", ["当前权限：普通成员"], [
            help_module.HelpSection("自定义命令", [help_module.HelpCommand("mc 很长的命令", ["mc 别名"], description, "权限：成员可用")]),
        ])
        with tempfile.TemporaryDirectory() as directory:
            paths = help_module.render_help_pages(document, directory, width=600, page_height=650, font_path=font)
            self.assertGreater(len(paths), 2)
            visible_text = []
            for index, path in enumerate(paths):
                with Image.open(path) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.width, 600)
                    self.assertLessEqual(image.height, 650)
                    visible_text.append(image.info["page_text"])
                    if index == 0:
                        self.assertEqual(image.info["help_text"], document.as_text())
            self.assertIn(description, "".join(visible_text).replace("\n", ""))
            self.assertIn("唯一结尾-END", visible_text[-1])

    def test_missing_chinese_font_is_reported_for_text_fallback(self):
        document = help_module.HelpDocument("帮助", [], [])
        with tempfile.TemporaryDirectory() as directory, patch.object(help_module, "_font_path", side_effect=RuntimeError("缺少中文字体")):
            with self.assertRaisesRegex(RuntimeError, "中文字体"):
                help_module.render_help_pages(document, directory)
            self.assertFalse(list(Path(directory).glob("*.png")))
