"""Independent group/game result combinations and side-effect-free UI previews."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.store import Store
from services.domain import Domain
from tests.test_domain_revision import ALREADY, Gateway, Settings


WORK = Path(__file__).resolve().parents[1] / "work"
WORK.mkdir(exist_ok=True)
SIGNED_17 = "Rcon:§f[§3手记人偶§f] §a成功给玩家 §6rincho §a签到， 当前该玩家已连续签到 §e17 §a天。"
GROUP_DUPLICATE = "群内签到失败❌️ 今日已签到 请不要重复签到"
GAME_DUPLICATE = "游戏签到失败❌️ 今日已签到 请不要重复签到"


class SigninCombinationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="signin-combinations-")
        self.store = Store(Path(self.temp.name) / "data.sqlite3")
        self.settings, self.gateway = Settings(), Gateway()
        self.domain = Domain(self.store, self.settings, self.gateway)
        self.ctx = {"umo": "bot:GroupMessage:1", "scope": "group1", "server_id": "paper", "user_id": "1", "message_id": "current"}
        self.store.bind_name("1", "rincho", "paper", 0)

    async def asyncTearDown(self):
        await self.domain.close()
        self.store.close()
        self.temp.cleanup()

    def seed_group(self):
        day = self.domain._day(self.settings.route_data)
        self.store.begin_signin("earlier", "group1", "1", day, 5, self.store.binding("1"), self.ctx["umo"])
        self.store.set_signin_state("earlier", "game_confirmed", "SIGNED", "earlier reply",
                                    {"game_continuous_days": "2", "points_name": "樱花币"})
        self.store.credit_signin("earlier")

    async def assert_combination(self, group_signed, game_signed):
        if group_signed:
            self.seed_group()
        else:
            self.store.adjust_points("group1", "1", "add", 5, "fixture", "opening")
        self.gateway.replies = [SIGNED_17 if game_signed else ALREADY]
        result = await self.domain.execute("signin", self.ctx)
        group_line = GROUP_DUPLICATE if group_signed else "群内签到成功✅️ 本次获得: 10 樱花币 当前樱花币:15"
        game_line = "游戏签到成功✅️ 已连续签到 17 天。" if game_signed else GAME_DUPLICATE
        self.assertEqual(result["text"], f"玩家: rincho\n{group_line}\n{game_line}")
        self.assertEqual(result["data"]["group_status"], "ALREADY_SIGNED" if group_signed else "SIGNED")
        self.assertEqual(result["data"]["game_status"], "SIGNED" if game_signed else "ALREADY_SIGNED")
        self.assertEqual(result["data"]["days"], "17" if game_signed else "")
        self.assertEqual(self.store.balance("group1", "1")["total"], 5 if group_signed else 15)
        self.assertEqual(len(self.gateway.calls), 1)
        replay = await self.domain.execute("signin", self.ctx)
        self.assertEqual(replay, result)
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_group_success_game_success(self):
        await self.assert_combination(False, True)

    async def test_group_success_game_already_signed(self):
        await self.assert_combination(False, False)

    async def test_group_already_signed_game_success_uses_current_days(self):
        await self.assert_combination(True, True)

    async def test_group_already_signed_game_already_signed(self):
        await self.assert_combination(True, False)

    async def test_recovery_keeps_current_game_success_instead_of_prior_daily_result(self):
        self.seed_group()
        self.gateway.replies = [SIGNED_17]
        with patch.object(self.store, "credit_signin", side_effect=sqlite3.OperationalError("synthetic interruption")):
            interrupted = await self.domain.execute("signin", self.ctx)
        self.assertEqual(interrupted["status"], "unknown")
        await self.domain.recover()
        recovered = await self.domain.execute("signin", self.ctx)
        self.assertEqual(recovered["text"], f"玩家: rincho\n{GROUP_DUPLICATE}\n游戏签到成功✅️ 已连续签到 17 天。")
        self.assertEqual(self.store.balance("group1", "1")["total"], 5)
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_unknown_game_does_not_infer_group_status_or_credit(self):
        self.seed_group()
        self.gateway.replies = ["§c[手记人偶] 无法连接签到数据库"]
        result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("游戏签到结果无法确认", result["text"])
        self.assertNotIn("今日已签到", result["text"])
        self.assertEqual(self.store.balance("group1", "1")["total"], 5)
        operation = self.store.signin_operation(self.domain._key("signin", self.ctx))
        self.assertEqual(operation["state"], "unknown")

    async def test_old_completed_results_without_days_can_replay_without_fabrication(self):
        operation_id = self.domain._key("signin", self.ctx)
        old = {"group_status": "SIGNED", "game_status": "SIGNED", "points_delta": 3, "balance": 13, "day": "2026-10-31"}
        self.store.begin_signin(operation_id, "group1", "1", "2026-10-31", 3, self.store.binding("1"), self.ctx["umo"])
        with self.store.connection(True) as db:
            db.execute("UPDATE signin_operations SET state='completed',result_json=? WHERE id=?", (json.dumps(old), operation_id))
        replay = await self.domain.execute("signin", self.ctx)
        self.assertEqual(replay["text"], "玩家: rincho\n群内签到成功✅️ 本次获得: 3 樱花币 当前樱花币:13\n游戏签到成功✅️")
        self.assertNotIn("0 天", replay["text"])
        self.assertEqual(self.gateway.calls, [])


class SigninPreviewTests(unittest.TestCase):
    def setUp(self):
        self.store, self.gateway = Mock(), Mock()
        self.settings = Settings()
        self.domain = Domain(self.store, self.settings, self.gateway)

    def tearDown(self):
        self.assertEqual(self.store.mock_calls, [])
        self.assertEqual(self.gateway.mock_calls, [])

    def test_all_four_previews_are_pure_and_use_current_game_reply(self):
        for group_signed in (False, True):
            for game_signed in (False, True):
                with self.subTest(group_signed=group_signed, game_signed=game_signed):
                    result = self.domain.preview_signin(SIGNED_17 if game_signed else ALREADY, "rincho", self.settings.route_data,
                                                        group_signed=group_signed, points_delta=4, balance=99)
                    group = GROUP_DUPLICATE if group_signed else "群内签到成功✅️ 本次获得: 4 樱花币 当前樱花币:99"
                    game = "游戏签到成功✅️ 已连续签到 17 天。" if game_signed else GAME_DUPLICATE
                    self.assertEqual(result["preview"], f"玩家: rincho\n{group}\n{game}")
                    self.assertEqual(result["status"], "SIGNED" if game_signed else "ALREADY_SIGNED")
                    self.assertEqual(result["data"]["points_delta"], 0 if group_signed else 4)

    def test_preview_preserves_custom_templates_and_result_placeholders(self):
        route = dict(self.settings.route_data, templates={"signin": "{player}|{group_result}|{game_result}|{days}|{rcon}",
                                                        "signin_already": "自定义重复 {group_result}/{game_result}"})
        fresh = self.domain.preview_signin(SIGNED_17, "rincho", route, points_delta=4, balance=99)
        self.assertIn("|17|[手记人偶]", fresh["preview"])
        repeated = self.domain.preview_signin(SIGNED_17, "rincho", route, group_signed=True)
        self.assertEqual(repeated["preview"], "自定义重复 " + GROUP_DUPLICATE + "/游戏签到成功✅️ 已连续签到 17 天。")

    def test_unknown_or_wrong_player_preview_does_not_simulate_award(self):
        for raw in ("未知回包", SIGNED_17.replace("rincho", "Other")):
            result = self.domain.preview_signin(raw, "rincho", self.settings.route_data, group_signed=True, points_delta=500, balance=99)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["data"]["group_status"], "NOT_SETTLED")
            self.assertEqual(result["data"]["points_delta"], 0)
            self.assertNotIn("群内签到成功", result["preview"])
            self.assertNotIn("今日已签到", result["preview"])

    def test_preview_uses_route_custom_prefixes_rules_and_validates_sample_values(self):
        route = dict(self.settings.route_data, signin_prefixes=["[自定义]"],
                     signin_rules=[{"status": "SIGNED", "pattern": r"完成 (?P<player>\w+) 连续 (?P<days>\d+)"}])
        result = self.domain.preview_signin("§a[自定义] 完成 rincho 连续 9", "rincho", route)
        self.assertEqual(result["status"], "SIGNED")
        self.assertIn("已连续签到 9 天", result["preview"])
        for kwargs in ({"group_signed": "false"}, {"points_delta": -1}, {"balance": True}):
            with self.assertRaises(ValueError):
                self.domain.preview_signin(SIGNED_17, "rincho", route, **kwargs)


if __name__ == "__main__":
    unittest.main()
