"""User-facing binding/sign-in/currency regression coverage using offline fixtures."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core.store import DomainError, Store
from services.domain import Domain


WORK = Path(__file__).resolve().parents[1] / "work"
WORK.mkdir(exist_ok=True)
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
SIGNED = "Rcon:§f[§3手记人偶§f] §a成功给玩家 §6rincho §a签到， 当前该玩家已连续签到 §e1 §a天。"
ALREADY = "Rcon:§f[§3手记人偶§f] §c玩家 §6rincho §c今天已经签到了，请不要重复签到。"
CLEAN_SIGNED = "[手记人偶] 成功给玩家 rincho 签到， 当前该玩家已连续签到 1 天。"
CLEAN_ALREADY = "[手记人偶] 玩家 rincho 今天已经签到了，请不要重复签到。"


class Settings:
    def __init__(self):
        self.data = {"timezone": "Asia/Shanghai", "binding_cooldown_seconds": 0, "points_name": "樱花币"}
        self.route_data = {"scope": "group1", "server_id": "paper", "umo": "bot:GroupMessage:1", "enabled": True,
                           "points_min": 10, "points_max": 10, "templates": {}}

    def route(self, umo):
        return self.route_data


class Gateway:
    def __init__(self):
        self.calls = []
        self.replies = [SIGNED]

    async def rcon(self, server, command):
        self.calls.append((server, command))
        await asyncio.sleep(0)
        return self.replies.pop(0) if self.replies else ALREADY


class BindingRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="identity-revision-")
        self.path = Path(self.temp.name) / "data.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_name_binding_is_case_insensitive_and_has_no_fake_uuid(self):
        result = self.store.bind_name("1", "RinCho", "paper", 0)
        binding = result["binding"]
        self.assertEqual(binding["identity"], "name:rincho")
        self.assertFalse(binding["verified"])
        self.assertIsNone(binding["mc_uuid"])
        self.assertTrue(self.store.bind_name("1", "RINCHO", "paper", 0)["already"])
        with self.assertRaisesRegex(DomainError, "其他 QQ"):
            self.store.bind_name("2", "rincho", "paper", 0)
        with self.assertRaisesRegex(DomainError, "修改绑定"):
            self.store.bind_name("1", "Another", "paper", 0)
        self.assertFalse(self.store.snapshot()["bindings"][0]["verified"])

    def test_name_cooldowns_survive_unbind_for_both_parties(self):
        self.store.bind_name("1", "rincho", "paper", 60, now=100)
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.store.bind_name("1", "Another", "paper", 60, rebind=True, now=120)
        self.store.unbind("1", 60, now=170)
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.store.bind_name("2", "RINCHO", "paper", 60, now=200)
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.store.bind_name("1", "Another", "paper", 60, now=200)
        self.store.bind_name("2", "RINCHO", "paper", 60, now=231)

    def test_verifying_same_name_preserves_cooldown_and_increments_version(self):
        original = self.store.bind_name("1", "rincho", "paper", 60, now=100)["binding"]
        challenge = self.store.begin_binding("1", "RINCHO", "paper", "umo", now=101)
        verified = self.store.complete_binding(challenge["code"], UUID_A, "rincho", "paper", 60, now=102)["binding"]
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["mc_uuid"], UUID_A)
        self.assertEqual(verified["identity"], "uuid:" + UUID_A)
        self.assertGreater(verified["version"], original["version"])
        self.assertEqual(self.store.binding("1")["cooldown_until"], 160)
        unchanged = self.store.bind_name("1", "RINCHO", "paper", 60, now=103)["binding"]
        self.assertEqual(unchanged["mc_uuid"], UUID_A)
        self.assertEqual(unchanged["version"], verified["version"])
        with self.assertRaisesRegex(DomainError, "其他 QQ"):
            self.store.bind_name("2", "rincho", "paper", 0, now=200)
        other = self.store.begin_binding("2", "Other", "paper", "umo", now=200)
        with self.assertRaisesRegex(DomainError, "其他 QQ"):
            self.store.complete_binding(other["code"], UUID_A, "Other", "paper", 0, now=201)

    def test_name_upgrade_cannot_bypass_released_uuid_cooldown(self):
        challenge = self.store.begin_binding("9", "Previous", "paper", "umo", now=100)
        self.store.complete_binding(challenge["code"], UUID_A, "Previous", "paper", 0, now=100)
        self.store.unbind("9", 60, now=101)
        self.store.bind_name("1", "rincho", "paper", 0, now=102)
        upgrade = self.store.begin_binding("1", "rincho", "paper", "umo", now=103)
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.store.complete_binding(upgrade["code"], UUID_A, "rincho", "paper", 0, now=104)
        self.assertFalse(self.store.binding("1")["verified"])

    def test_v1_database_upgrade_preserves_uuid_and_name_cooldown(self):
        old_path = Path(self.temp.name) / "v1.sqlite3"
        with closing(sqlite3.connect(old_path)) as db:
            db.executescript("""CREATE TABLE bindings(qq_id TEXT PRIMARY KEY,mc_uuid TEXT UNIQUE NOT NULL,
                player TEXT NOT NULL,server_id TEXT NOT NULL,version INTEGER NOT NULL,updated REAL NOT NULL);
                CREATE TABLE binding_cooldowns(identity TEXT PRIMARY KEY,until_time REAL NOT NULL);""")
            db.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?)", ("1", UUID_A, "rincho", "paper", 7, 100))
            db.execute("INSERT INTO binding_cooldowns VALUES(?,?)", ("mc:" + UUID_A, 200))
            db.commit()
        upgraded = Store(old_path)
        self.assertEqual(upgraded.binding("1")["mc_uuid"], UUID_A)
        self.assertTrue(upgraded.binding("1")["verified"])
        self.assertEqual(upgraded.binding("1")["version"], 7)
        upgraded.unbind("1", 0, force=True, now=101)
        with self.assertRaisesRegex(DomainError, "冷却"):
            upgraded.bind_name("2", "RINCHO", "paper", 0, now=102)
        # Reopening an upgraded database is idempotent.
        reopened = Store(old_path)
        self.assertIsNone(reopened.binding("1"))


class DomainRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="domain-revision-")
        self.store = Store(Path(self.temp.name) / "data.sqlite3")
        self.settings, self.gateway = Settings(), Gateway()
        self.domain = Domain(self.store, self.settings, self.gateway)
        self.ctx = {"umo": "bot:GroupMessage:1", "scope": "group1", "server_id": "paper", "user_id": "1", "message_id": "1"}

    async def asyncTearDown(self):
        await self.domain.close()
        self.store.close()
        self.temp.cleanup()

    async def bind(self):
        return await self.domain.execute("binding", self.ctx, "rincho")

    async def test_verification_off_is_default_and_needs_no_game_operation(self):
        result = await self.bind()
        self.assertEqual(result["text"], "绑定成功：rincho")
        self.assertEqual(result["status"], "success")
        self.assertNotIn("bind ", result["text"])
        self.assertEqual(self.gateway.calls, [])
        self.assertIsNone(self.store.binding("1")["mc_uuid"])
        status = await self.domain.execute("binding_status", self.ctx)
        self.assertIn("玩家名（未验证）", status["text"])
        self.assertNotIn("UUID：", status["text"])

    async def test_verification_on_requires_matching_game_event(self):
        self.settings.data["binding_verification_enabled"] = True
        result = await self.bind()
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(self.store.binding("1"))
        code = result["text"].split("bind ", 1)[1].split("\n", 1)[0]
        await self.domain.on_game_event("paper", {"event_name": "PlayerChatEvent", "message": "bind " + code,
                                                    "player": {"name": "rincho", "uuid": UUID_A}})
        self.assertTrue(self.store.binding("1")["verified"])

    async def test_success_reply_preserves_complete_clean_rcon_and_replays(self):
        await self.bind()
        result = await self.domain.execute("signin", self.ctx)
        expected = "签到成功✅️\n本次获得 10樱花币，当前樱花币：10\n" + CLEAN_SIGNED
        self.assertEqual(result["text"], expected)
        self.assertEqual(result["data"]["rcon"], CLEAN_SIGNED)
        stored = self.store.signin_operation(self.domain._key("signin", self.ctx))
        self.assertEqual(stored["result"]["rcon"], CLEAN_SIGNED)
        self.settings.data["points_name"] = "新名称"
        replay = await self.domain.execute("signin", self.ctx)
        self.assertEqual(replay["text"], expected)
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_game_already_signed_allows_first_group_credit_then_short_duplicate(self):
        await self.bind()
        self.gateway.replies = [ALREADY, ALREADY]
        first = await self.domain.execute("signin", self.ctx)
        self.assertEqual(first["text"], "签到成功✅️\n本次获得 10樱花币，当前樱花币：10\n" + CLEAN_ALREADY)
        second = await self.domain.execute("signin", dict(self.ctx, message_id="2"))
        self.assertEqual(second["text"], "签到失败❌️ 今日已签到，请不要重复签到。")
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertEqual(self.store.balance("group1", "1")["total"], 10)

    async def test_failed_game_response_is_not_misreported_as_group_duplicate(self):
        await self.bind()
        self.gateway.replies = ["§c[手记人偶] 玩家不存在"]
        result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("玩家不存在", result["text"])
        self.assertNotIn("今日已签到", result["text"])
        replay = await self.domain.execute("signin", self.ctx)
        self.assertEqual(replay["text"], result["text"])
        self.assertEqual(self.store.balance("group1", "1")["total"], 0)

    async def test_confirmed_signin_recovers_original_rcon_and_currency(self):
        await self.bind()
        with patch.object(self.store, "credit_signin", side_effect=sqlite3.OperationalError("synthetic commit interruption")):
            await self.domain.execute("signin", self.ctx)
        operation = self.store.signin_operation(self.domain._key("signin", self.ctx))
        self.assertEqual(operation["state"], "game_confirmed")
        self.settings.data["points_name"] = "新名称"
        await self.domain.recover()
        result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["text"], "签到成功✅️\n本次获得 10樱花币，当前樱花币：10\n" + CLEAN_SIGNED)
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_currency_name_reaches_balance_shop_order_admin_and_errors(self):
        admin = dict(self.ctx, is_admin=True)
        result = await self.domain.execute("points_add", admin, "100")
        self.assertIn("当前樱花币：100", result["text"])
        for action in ("balance", "rank"):
            self.assertIn("樱花币", (await self.domain.execute(action, self.ctx))["text"])
        self.store.save_product("group1", {"id": "p", "name": "商品", "price": 30, "kind": "text", "reward": "内容"}, "admin")
        self.assertIn("30 樱花币", (await self.domain.execute("shop", self.ctx))["text"])
        async def private(user, text):
            return True
        ctx = dict(self.ctx, reply_private=private)
        purchase = await self.domain.execute("purchase", ctx, "p")
        self.assertIn("尚未扣除樱花币", purchase["text"])
        order = self.store.orders("group1", "1")[0]
        delivered = await self.domain.execute("confirm", ctx, order["id"])
        self.assertIn("樱花币只扣除一次", delivered["text"])
        denied = await self.domain.execute("points_sub", dict(admin, message_id="3"), "9999")
        self.assertIn("可用樱花币不足", denied["text"])
        with self.assertRaisesRegex(DomainError, "樱花币"):
            await self.domain.admin("points_adjust", {"scope": "group1", "user_id": "1", "mode": "sub", "amount": 9999, "operation_id": "bad"}, "admin")

    async def test_custom_signin_templates_receive_rcon_and_currency(self):
        await self.bind()
        self.settings.route_data["templates"] = {"signin": "{points_delta}{points_name}|{rcon}", "signin_already": "重复/{points_name}"}
        first = await self.domain.execute("signin", self.ctx)
        self.assertEqual(first["text"], "10樱花币|" + CLEAN_SIGNED)
        second = await self.domain.execute("signin", dict(self.ctx, message_id="2"))
        self.assertEqual(second["text"], "重复/樱花币")

    async def test_pending_unverified_identity_change_is_rejected_before_rcon(self):
        await self.bind()
        operation_id = self.domain._key("signin", self.ctx)
        self.store.begin_signin(operation_id, "group1", "1", self.domain._day(self.settings.route_data), 10, self.store.binding("1"), self.ctx["umo"])
        await self.domain.execute("rebind", dict(self.ctx, message_id="2"), "Another")
        result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.gateway.calls, [])


if __name__ == "__main__":
    unittest.main()
