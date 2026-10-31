"""Help permissions, editable stable IDs and complete single-image card output."""
from __future__ import annotations

import json
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

    def test_help_is_first_by_default_but_explicit_order_still_wins(self):
        effective = self.effective[:2] + [{"handler_name": "cmd_help", "effective_command": "mc帮助"}]
        default = help_module.help_catalog(effective, {}, {}, {}, set())
        self.assertEqual(default[0]["action_id"], "help")
        overridden = help_module.help_catalog(effective, {}, {"entries": {"help": {"order": 9999}}}, {}, set())
        self.assertEqual(overridden[-1]["action_id"], "help")


class HelpImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.font = help_module._font_path()
        except RuntimeError as exc:
            raise unittest.SkipTest(str(exc))

    def test_long_chinese_help_stays_one_image_without_truncating_any_field(self):
        from PIL import Image
        description = "完整说明与原始参数必须全部显示ABCDEFGHIJ" * 100 + "唯一结尾-END"
        name = "mc 超长指令名称λ测试Ж" * 20
        alias = "@机器人 很长别名α中文名" * 30
        notes = "权限：成员可用 · 需要绑定 MC 账号 · 冷却：123 秒"
        document = help_module.HelpDocument("MCQQ 指令速查", ["当前权限：普通成员"], [
            help_module.HelpSection("自定义命令", [help_module.HelpCommand(name, [alias], description, notes)]),
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = help_module.render_help_image(document, directory, font_path=self.font)
            self.assertIsInstance(path, str)
            self.assertEqual(list(Path(directory).glob("*.png")), [Path(path)])
            with Image.open(path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, 780)
                self.assertGreater(image.height, 2000)
                self.assertEqual(image.info["help_text"], document.as_text())
                visible = image.info["rendered_text"].replace("\n", "")
                for value in (name, alias, description, *notes.split(" · ")):
                    self.assertIn(value, visible)
                self.assertEqual(json.loads(image.info["help_counts"]), {"commands": 1, "categories": 1})
                self.assertNotIn(" 页", image.info["rendered_text"])

    def test_interleaved_categories_stay_separate_in_original_global_order(self):
        from PIL import Image
        sections = [
            help_module.HelpSection("分类甲", [help_module.HelpCommand("/长命令甲", [], "长说明" * 70, "权限：成员可用")]),
            help_module.HelpSection("分类乙", [help_module.HelpCommand("/短命令乙", [], "短说明", "权限：管理员")]),
            help_module.HelpSection("分类甲", [help_module.HelpCommand("/命令丙", ["/别名丙"], "排序丙", "权限：成员可用")]),
        ]
        document = help_module.HelpDocument("非 ASCII 帮助测试：λ与中文", ["完整保留原始前缀"], sections)
        with tempfile.TemporaryDirectory() as directory:
            path = help_module.render_help_image(document, directory, width=1000, font_path=self.font)
            with Image.open(path) as image:
                cards = json.loads(image.info["card_layout"])
                regions = json.loads(image.info["region_layout"])
                self.assertEqual([region["category"] for region in regions], ["分类甲", "分类乙", "分类甲"])
                self.assertEqual([card["title"] for card in cards], ["分类甲", "分类乙", "分类甲 · 续"])
                painted = sorted((entry for card in cards for entry in card["command_text"]), key=lambda entry: entry[0])
                self.assertEqual([(number, lines[0]) for number, lines in painted],
                                 [(1, "01  /长命令甲"), (2, "02  /短命令乙"), (3, "03  /命令丙")])
                self.assertEqual(sorted(number for card in cards if card["category"] == "分类甲"
                                        for number in card["sequences"]), [1, 3])
                self.assertEqual(json.loads(image.info["help_counts"]), {"commands": 3, "categories": 2})
                visible = image.info["rendered_text"]
                self.assertLess(visible.index("/长命令甲"), visible.index("/短命令乙"))
                self.assertLess(visible.index("/短命令乙"), visible.index("/命令丙"))
                self.assertIn(document.title, visible.replace("\n", ""))
                self.assertGreater(image.height, max(card["y"] + card["height"] for card in cards))
                self.assertIn("完整保留原始前缀", visible)
                self.assert_connected_disjoint_regions(image)

    def assert_connected_disjoint_regions(self, image):
        from PIL import Image, ImageChops, ImageDraw
        regions = json.loads(image.info["region_layout"])
        combined = Image.new("1", image.size)
        for region in regions:
            rectangles = region["rectangles"]
            polygon = [tuple(point) for point in region["polygon"]]
            for first, second in zip(polygon, polygon[1:] + polygon[:1]):
                self.assertTrue(first[0] == second[0] or first[1] == second[1], "outline must be orthogonal")
            pending, visited = [0], set()
            while pending:
                index = pending.pop()
                if index in visited:
                    continue
                visited.add(index)
                first = rectangles[index]
                for other_index, second in enumerate(rectangles):
                    dx = min(first[2], second[2]) - max(first[0], second[0])
                    dy = min(first[3], second[3]) - max(first[1], second[1])
                    if other_index not in visited and dx >= 0 and dy >= 0 and dx + dy > 0:
                        pending.append(other_index)
            self.assertEqual(len(visited), len(rectangles), "one category must have no disconnected islands")
            outline_mask, union_mask = Image.new("1", image.size), Image.new("1", image.size)
            ImageDraw.Draw(outline_mask).polygon(polygon, fill=1)
            union_draw = ImageDraw.Draw(union_mask)
            for rectangle in rectangles:
                self.assertLess(rectangle[0], rectangle[2])
                self.assertLess(rectangle[1], rectangle[3])
                union_draw.rectangle(rectangle, fill=1)
            self.assertIsNone(ImageChops.difference(outline_mask, union_mask).getbbox(),
                              "outer polygon must equal its parts, without bridging through unrelated content")
            self.assertIsNone(ImageChops.logical_and(combined, outline_mask).getbbox(), "category regions must not overlap")
            combined = ImageChops.logical_or(combined, outline_mask)

    def test_default_23_and_custom_5_15_30_50_read_down_columns_of_at_most_15(self):
        from PIL import Image
        from adapters.astrbot_commands import ADMIN_ACTIONS, BUILTINS
        for count in (0, 5, 15, 30, 50):
            with self.subTest(custom_commands=count), tempfile.TemporaryDirectory() as directory:
                effective = [{"handler_name": "cmd_" + action, "effective_command": name, "description": description}
                             for action, name, description in BUILTINS]
                customs = {}
                for index in range(count):
                    handler = f"cmd_custom_{index:02d}"
                    customs[handler] = {"id": f"custom_{index:02d}", "admin_only": index % 3 == 0,
                                        "require_binding": index % 2 == 0, "cooldown_seconds": index + 1}
                    effective.append({"handler_name": handler, "effective_command": f"自定义{index:02d}",
                                      "aliases": [f"别名{index:02d}"],
                                      "description": f"完整说明{index:02d}：在当前服务器查询或兑换奖励，保留参数 [玩家名]。"})
                settings = {"entries": {"custom:custom_00": {"order": 15}}} if count else {}
                catalog = help_module.help_catalog(effective, customs, settings, {}, ADMIN_ACTIONS)
                self.assertEqual(catalog[0]["action_id"], "help")
                if count:
                    self.assertEqual(catalog[2]["action_id"], "custom:custom_00")
                document = help_module.build_help_document(catalog, is_admin=True, command_hint=lambda value: "mc " + value)
                path = help_module.render_help_image(document, directory, font_path=self.font)
                self.assertEqual(len(list(Path(directory).glob("*.png"))), 1)
                with Image.open(path) as image:
                    self.assertEqual(json.loads(image.info["help_counts"]),
                                     {"commands": 23 + count, "categories": 6 if count else 5})
                    columns = (23 + count + 14) // 15
                    self.assertEqual(int(image.info["columns"]), columns)
                    self.assertEqual(image.width, 60 + 720 * columns)
                    self.assertEqual(image.info["help_text"], document.as_text())
                    cards = json.loads(image.info["card_layout"])
                    visual_cards = sorted(cards, key=lambda card: (card["column"], card["y"]))
                    painted = [entry for card in visual_cards for entry in card["command_text"]]
                    self.assertEqual([number for number, _ in painted], list(range(1, 24 + count)))
                    self.assertEqual([lines[0] for _, lines in painted],
                                     [f"{number:02d}  {command.name}" for number, command in enumerate(
                                         [command for section in document.sections for command in section.commands], 1)])
                    self.assertEqual(painted[0][1][0], "01  mc mc帮助")
                    if count:
                        self.assertEqual(painted[2][1][0], "03  mc 自定义00")
                    for column in range(columns):
                        sequences = [number for card in visual_cards if card["column"] == column for number in card["sequences"]]
                        self.assertEqual(sequences, list(range(column * 15 + 1, min((column + 1) * 15, 23 + count) + 1)))
                        self.assertLessEqual(len(sequences), 15)
                    self.assertEqual(next(card["column"] for card in cards if 15 in card["sequences"]), 0)
                    self.assertEqual(next(card["column"] for card in cards if 16 in card["sequences"]), 1)
                    for card in cards:
                        self.assertEqual(card["sequences"], list(range(card["sequences"][0], card["sequences"][-1] + 1)))
                        self.assertEqual(card["column"], (card["sequences"][0] - 1) // 15)
                    for region in json.loads(image.info["region_layout"]):
                        self.assertEqual(len(region["polygon"]), 4)
                        self.assertEqual(region["layout"], "single_column")
                    visible = image.info["rendered_text"].replace("\n", "")
                    self.assertIn("mc 订单核对", visible)
                    for index in range(count):
                        for value in (f"mc 自定义{index:02d}", f"mc 别名{index:02d}", f"完整说明{index:02d}"):
                            self.assertIn(value, visible)
                    self.assert_connected_disjoint_regions(image)
                    self.assertLessEqual(image.width * image.height, help_module.MAX_IMAGE_PIXELS)

    def test_help_first_long_custom_third_does_not_truncate_or_merge_intervening_category(self):
        from PIL import Image
        from adapters.astrbot_commands import ADMIN_ACTIONS, BUILTINS
        name = "跨世界资源与在线状态联合查询" * 8
        alias = "非常长的自定义别名" * 15
        description = "这段长说明必须完整呈现并且不能覆盖后面的分类和命令。" * 40
        effective = [{"handler_name": "cmd_" + action, "effective_command": value, "description": text}
                     for action, value, text in BUILTINS]
        effective.append({"handler_name": "custom_handler", "effective_command": name,
                          "aliases": [alias], "description": description})
        customs = {"custom_handler": {"id": "third", "admin_only": False}}
        catalog = help_module.help_catalog(effective, customs, {"entries": {"custom:third": {"order": 15}}}, {}, ADMIN_ACTIONS)
        document = help_module.build_help_document(catalog, is_admin=True, command_hint=str)
        with tempfile.TemporaryDirectory() as directory:
            path = help_module.render_help_image(document, directory, font_path=self.font)
            with Image.open(path) as image:
                cards = sorted(json.loads(image.info["card_layout"]), key=lambda card: (card["column"], card["y"]))
                painted = [entry for card in cards for entry in card["command_text"]]
                self.assertEqual(painted[0][1][0], "01  mc帮助")
                self.assertEqual(painted[2][0], 3)
                third_text = "".join(painted[2][1])
                for value in (name, alias, description):
                    self.assertIn(value, third_text)
                self.assertEqual([number for number, _ in painted], list(range(1, 25)))
                self.assertEqual(next(card["column"] for card in cards if 15 in card["sequences"]), 0)
                self.assertEqual(next(card["column"] for card in cards if 16 in card["sequences"]), 1)
                self.assertEqual(cards[2]["category"], "自定义命令")
                self.assertEqual(cards[3]["title"], "签到与绑定 · 续")
                self.assert_connected_disjoint_regions(image)

    def test_explicit_width_cannot_reduce_required_columns_or_shrink_text(self):
        document = help_module.HelpDocument("帮助", [], [
            help_module.HelpSection("自定义命令", [help_module.HelpCommand(f"命令{index:02d}", [], "说明", "权限：成员可用")
                                                    for index in range(56)]),
        ])
        with tempfile.TemporaryDirectory() as directory, patch("PIL.Image.new") as allocate:
            with self.assertRaisesRegex(RuntimeError, "宽度不足"):
                help_module.render_help_image(document, directory, width=1500, font_path=self.font)
            allocate.assert_not_called()
            self.assertFalse(list(Path(directory).glob("*.png")))

    def test_empty_help_has_zero_counts_and_readable_message(self):
        from PIL import Image
        document = help_module.HelpDocument("MCQQ 指令速查", ["没有权限的命令已隐藏。"], [])
        with tempfile.TemporaryDirectory() as directory:
            path = help_module.render_help_image(document, directory, font_path=self.font)
            with Image.open(path) as image:
                self.assertEqual(json.loads(image.info["help_counts"]), {"commands": 0, "categories": 0})
                self.assertEqual(json.loads(image.info["card_layout"]), [])
                self.assertIn("当前没有可用的插件指令。", image.info["rendered_text"])
                self.assertIn("没有权限的命令已隐藏。", image.info["rendered_text"])

    def test_size_budget_raises_before_image_allocation_instead_of_paginating(self):
        document = help_module.HelpDocument("帮助", [], [
            help_module.HelpSection("分类", [help_module.HelpCommand("命令", [], "完整说明" * 10, "权限：成员可用")]),
        ])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(help_module, "MAX_IMAGE_PIXELS", 100), patch("PIL.Image.new") as allocate:
            with self.assertRaisesRegex(RuntimeError, "单张图片尺寸上限"):
                help_module.render_help_image(document, directory, font_path=self.font)
            allocate.assert_not_called()
            self.assertFalse(list(Path(directory).glob("*.png")))

    def test_card_layout_keeps_all_text_inside_each_card(self):
        from PIL import ImageFont
        fonts = {kind: ImageFont.truetype(str(self.font), size) for kind, size in
                 (("section", 26), ("command", 24), ("body", 20), ("muted", 18), ("badge", 16))}
        section = help_module.HelpSection("超长类别名称" * 5, [help_module.HelpCommand(
            "超长名称" * 60, ["超长别名" * 60], "说明中文English" * 100,
            "权限：成员可用 · 需要绑定 MC 账号 · 冷却：10000 秒 · 自定义超长备注" + "完整" * 90,
        )])
        card = help_module._card_layout(section, 440, fonts)
        for kind, value, x, y in card.text:
            self.assertLessEqual(x + fonts[kind].getlength(value), 440 - 25)
            self.assertLess(y + fonts[kind].size + 6, card.height)
        for left, upper, right, lower, _ in card.badges:
            self.assertGreaterEqual(left, 26)
            self.assertLessEqual(right, 440 - 25)
            self.assertLess(lower, card.height)


class HelpFallbackTests(unittest.TestCase):
    def test_missing_chinese_font_is_reported_for_text_fallback(self):
        document = help_module.HelpDocument("帮助", [], [])
        with tempfile.TemporaryDirectory() as directory, patch.object(help_module, "_font_path", side_effect=RuntimeError("缺少中文字体")):
            with self.assertRaisesRegex(RuntimeError, "中文字体"):
                help_module.render_help_image(document, directory)
            self.assertFalse(list(Path(directory).glob("*.png")))
