import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core.store import DomainError, Store
from services.domain import Domain


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
SIGNED = "Rcon:§f[§3手记人偶§f] §a成功给玩家 §6rincho §a签到， 当前该玩家已连续签到 §e1 §a天。"
ALREADY = "Rcon:§f[§3手记人偶§f] §c玩家 §6rincho §c今天已经签到了，请不要重复签到。"


class FakeSettings:
    def __init__(self):
        self.data = {"timezone": "Asia/Shanghai", "binding_cooldown_seconds": 0, "binding_verification_enabled": True}
        self.route_data = {"umo": "bot:GroupMessage:1", "scope": "group1", "server_id": "paper", "enabled": True,
                           "points_min": 10, "points_max": 10, "signin_command": "signin click {player}"}

    def route(self, umo):
        return self.route_data if umo == self.route_data["umo"] else None


class OutcomeUnknown(Exception):
    pass


class GatewayUnavailable(Exception):
    pass


class FakeGateway:
    def __init__(self):
        self.responses = [SIGNED]
        self.calls = []

    async def rcon(self, server, command):
        self.calls.append((server, command))
        value = self.responses.pop(0) if self.responses else ALREADY
        if isinstance(value, Exception):
            raise value
        await asyncio.sleep(0)
        return value


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="domain-")
        self.store = Store(Path(self.temp.name) / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def bind(self, qq="1", player="rincho", identifier=UUID_A, now=100, cd=60):
        challenge = self.store.begin_binding(qq, player, "paper", "umo", now=now)
        return self.store.complete_binding(challenge["code"], identifier, player, "paper", cd, now=now)

    def test_binding_unique_and_cooldown_survives_unbind(self):
        self.bind()
        challenge = self.store.begin_binding("2", "rincho", "paper", "umo", now=170)
        with self.assertRaisesRegex(DomainError, "其他 QQ"):
            self.store.complete_binding(challenge["code"], UUID_A, "rincho", "paper", 60, now=170)
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.store.unbind("1", 60, now=120)
        self.store.unbind("1", 60, now=170)
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.store.complete_binding(challenge["code"], UUID_A, "rincho", "paper", 60, now=171)
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.store.begin_binding("1", "Someone", "paper", "umo", now=171)
        self.store.complete_binding(challenge["code"], UUID_A, "rincho", "paper", 60, now=231)
        self.assertEqual(self.store.binding("2")["mc_uuid"], UUID_A)

    def test_bad_verification_keeps_original_binding(self):
        self.bind(cd=0)
        challenge = self.store.begin_binding("1", "NewPlayer", "paper", "umo", rebind=True, now=101)
        with self.assertRaises(DomainError):
            self.store.complete_binding(challenge["code"], UUID_B, "Other", "paper", 0, now=102)
        self.assertEqual(self.store.binding("1")["mc_uuid"], UUID_A)
        self.store.complete_binding(challenge["code"], UUID_B, "NewPlayer", "paper", 0, now=103)
        self.assertEqual(self.store.binding("1")["mc_uuid"], UUID_B)
        with self.assertRaises(DomainError):
            self.store.complete_binding(challenge["code"], UUID_B, "NewPlayer", "paper", 0, now=104)

    def test_binding_version_survives_unbind_and_same_account_rebind(self):
        original = self.bind(cd=0)["binding"]
        self.store.unbind("1", 0, now=101)
        replacement = self.bind(now=102, cd=0)["binding"]
        self.assertEqual(original["mc_uuid"], replacement["mc_uuid"])
        self.assertGreater(replacement["version"], original["version"])

    def test_point_adjustment_audits_reason_and_retries_once(self):
        self.store.adjust_points("g", "1", "add", 10, "admin", "same-operation", "活动奖励")
        retry = self.store.adjust_points("g", "1", "add", 10, "admin", "same-operation", "活动奖励")
        self.assertTrue(retry["duplicate"])
        self.assertEqual(self.store.balance("g", "1")["total"], 10)
        with self.store.connection() as db:
            records = db.execute("SELECT actor,detail FROM audit_log WHERE action='points_adjust'").fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["actor"], "admin")
        self.assertEqual(json.loads(records[0]["detail"])["reason"], "活动奖励")

    def test_concurrent_signins_only_credit_once(self):
        for index in range(12):
            operation = str(index)
            self.store.begin_signin(operation, "g", "1", "2026-10-31", 10, {}, "umo")
            self.store.set_signin_state(operation, "game_confirmed", "ALREADY_SIGNED")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.store.credit_signin, map(str, range(12))))
        self.assertEqual(sum(r["points_delta"] for r in results), 10)
        self.assertEqual(self.store.balance("g", "1")["total"], 10)
        self.assertEqual(len(self.store.snapshot("g")["ledger"]), 1)

    def test_concurrent_purchases_cannot_overdraw(self):
        self.store.adjust_points("g", "1", "add", 100, "admin", "credit")
        self.store.save_product("g", {"id": "p", "name": "P", "price": 80, "kind": "text", "reward": "reward"}, "admin")
        first = self.store.create_order("g", "1", "p", "first", "umo")
        second = self.store.create_order("g", "1", "p", "second", "umo")
        def reserve(order):
            try:
                return self.store.reserve_order(order["id"], "g", "1")["status"]
            except DomainError:
                return "rejected"
        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(reserve, [first, second]))
        self.assertEqual(sorted(states), ["rejected", "reserved"])
        self.assertEqual(self.store.balance("g", "1")["frozen"], 80)
        with self.assertRaises(DomainError):
            self.store.adjust_points("g", "1", "set", 0, "admin", "bad-adjust")

    def test_recovery_preserves_unknown_and_finishes_confirmed(self):
        self.store.begin_signin("a", "g", "1", "2026-10-31", 10, {}, "umo")
        self.store.begin_signin("b", "g", "2", "2026-10-31", 10, {}, "umo")
        self.store.set_signin_state("a", "sending")
        self.store.set_signin_state("b", "game_confirmed", "SIGNED")
        self.store.recover()
        self.assertEqual(self.store.signin_operation("a")["state"], "unknown")
        self.assertEqual(self.store.signin_operation("b")["state"], "completed")
        self.store.recover()
        self.assertEqual(self.store.balance("g", "2")["total"], 10)

    def legacy_fixture(self):
        path = Path(self.temp.name) / "legacy.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE balances(session TEXT,user_id TEXT,total INTEGER);
                CREATE TABLE checkins(id INTEGER,session TEXT,user_id TEXT,day TEXT,points INTEGER,credited INTEGER);
                CREATE TABLE session_settings(session TEXT,settings TEXT);
                CREATE TABLE bindings(account_key TEXT,player TEXT);
                CREATE TABLE used_codes(session TEXT,product_id TEXT,code TEXT,order_id INTEGER);
                CREATE TABLE purchases(id INTEGER,session TEXT,user_id TEXT,product_id TEXT,product_name TEXT,price INTEGER,status TEXT,product_snapshot TEXT,reward TEXT);
                CREATE TABLE balance_audit(id INTEGER,session TEXT,user_id TEXT,total INTEGER);
            """)
            product = {"id": "code", "name": "旧兑换码", "price": 40, "reward_type": "codes", "codes": ["USED_SECRET", "FREE_SECRET"]}
            db.execute("INSERT INTO balances VALUES('g','1',60)")
            db.execute("INSERT INTO checkins VALUES(1,'g','1','2026-10-30',10,1)")
            db.execute("INSERT INTO session_settings VALUES('g',?)", (json.dumps({"products": [product]}),))
            db.execute("INSERT INTO bindings VALUES('qq:1','rincho')")
            db.execute("INSERT INTO used_codes VALUES('g','code','USED_SECRET',1)")
            db.execute("INSERT INTO purchases VALUES(1,'g','1','code','旧兑换码',40,'delivery_failed',?,'USED_SECRET')", (json.dumps(product),))
            db.execute("INSERT INTO balance_audit VALUES(1,'g','1',60)")
            db.commit()
        return path

    def test_migration_is_explicit_idempotent_and_keeps_used_codes(self):
        path = self.legacy_fixture()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        preview = self.store.preview_legacy(path)
        self.assertEqual(preview["balance_totals"], {"g": 60})
        with self.assertRaises(DomainError):
            self.store.import_legacy(path, "wrong", "admin")
        self.store.import_legacy(path, preview["digest"], "admin")
        self.assertEqual(self.store.balance("g", "1")["total"], 60)
        self.assertIsNone(self.store.binding("1"))
        self.assertEqual(self.store.get_product("g", "code")["stock"], 1)
        self.assertFalse(self.store.get_product("g", "code")["enabled"])
        self.store.add_codes("g", "code", ["USED_SECRET"], "admin")
        self.assertEqual(self.store.get_product("g", "code")["stock"], 1)
        order = self.store.orders("g", "1")[0]
        self.assertEqual(order["status"], "paid_pending")
        self.store.claim_delivery(order["id"], reclaim=True)
        self.store.finish_order(order["id"], "fulfilled")
        self.assertEqual(self.store.balance("g", "1")["total"], 60)
        self.assertTrue(self.store.import_legacy(path, preview["digest"], "admin")["already_imported"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        public = json.dumps(self.store.snapshot())
        self.assertNotIn("USED_SECRET", public)
        self.assertNotIn("FREE_SECRET", public)


class DomainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="service-")
        self.store = Store(Path(self.temp.name) / "db.sqlite")
        self.settings, self.gateway = FakeSettings(), FakeGateway()
        self.domain = Domain(self.store, self.settings, self.gateway)
        self.ctx = {"umo": "bot:GroupMessage:1", "user_id": "1", "scope": "group1", "server_id": "paper", "message_id": "1", "is_admin": False}
        challenge = self.store.begin_binding("1", "rincho", "paper", self.ctx["umo"])
        self.store.complete_binding(challenge["code"], UUID_A, "rincho", "paper", 0)

    async def asyncTearDown(self):
        await self.domain.close()
        self.store.close()
        self.temp.cleanup()

    async def test_game_already_signed_still_awards_group_once(self):
        self.gateway.responses = [ALREADY, SIGNED, ALREADY]
        first = await self.domain.execute("signin", self.ctx)
        self.assertEqual(first["data"]["points_delta"], 10)
        second = await self.domain.execute("signin", dict(self.ctx, message_id="2"))
        self.assertEqual(second["data"]["points_delta"], 0)
        self.assertEqual(len(self.gateway.calls), 2)  # Group already signed does not skip MC.
        replay = await self.domain.execute("signin", dict(self.ctx, message_id="2"))
        self.assertEqual(len(self.gateway.calls), 2)  # Duplicate delivery of same request does.
        self.assertEqual(replay["data"], second["data"])  # Parsed continuous days persist as well.

    async def test_unknown_wrong_player_and_midnight_do_not_award(self):
        self.gateway.responses = [SIGNED.replace("rincho", "Other"), "", SIGNED]
        self.assertEqual((await self.domain.execute("signin", self.ctx))["status"], "unknown")
        self.assertEqual((await self.domain.execute("signin", dict(self.ctx, message_id="2")))["status"], "unknown")
        with patch.object(self.domain, "_day", side_effect=["2026-10-31", "2026-11-01"]):
            result = await self.domain.execute("signin", dict(self.ctx, message_id="3"))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.store.balance("group1", "1")["total"], 0)

    async def test_default_signin_rules_honor_custom_cleanup_prefixes(self):
        self.settings.route_data.update(signin_rules=[], signin_prefixes=["[自定签到]"])
        self.gateway.responses = [SIGNED.replace("手记人偶", "自定签到")]
        result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["game_status"], "SIGNED")
        self.assertEqual(result["data"]["game_continuous_days"], "1")
        self.assertEqual(self.store.balance("group1", "1")["total"], 10)

    async def test_old_unsent_request_does_not_sign_for_new_day(self):
        operation_id = self.domain._key("signin", self.ctx)
        self.store.begin_signin(operation_id, "group1", "1", "2000-01-01", 10, self.store.binding("1"), self.ctx["umo"])
        result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.gateway.calls, [])

    async def test_same_user_signins_remain_serial_with_transient_locks(self):
        active, maximum = 0, 0
        async def remote(server, command):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return SIGNED
        self.gateway.rcon = remote
        results = await asyncio.gather(*(self.domain.execute("signin", dict(self.ctx, message_id=str(i))) for i in range(5)))
        self.assertTrue(all(r["status"] == "success" for r in results))
        self.assertEqual(maximum, 1)
        self.assertEqual(self.store.balance("group1", "1")["total"], 10)
        self.assertEqual(len(self.domain._locks), 0)

    async def test_private_code_reclaim_reuses_same_reward_and_charges_once(self):
        self.store.adjust_points("group1", "1", "add", 100, "admin", "fund")
        self.store.save_product("group1", {"id": "code", "name": "激活码", "price": 30, "kind": "code"}, "admin")
        self.store.add_codes("group1", "code", ["SECRET_ONE", "SECRET_TWO"], "admin")
        received = []
        async def send(user, text):
            received.append(text)
            return len(received) > 1
        ctx = dict(self.ctx, reply_private=send)
        await self.domain.execute("purchase", ctx, "code")
        order = self.store.orders("group1", "1")[0]
        self.assertEqual((await self.domain.execute("confirm", ctx, order["id"]))["status"], "unknown")
        self.assertEqual(self.store.balance("group1", "1"), {"scope": "group1", "qq_id": "1", "total": 100, "frozen": 30, "available": 70})
        await self.domain.execute("reclaim", ctx, order["id"])
        await self.domain.execute("reclaim", ctx, order["id"])
        self.assertEqual(self.store.balance("group1", "1")["total"], 70)
        self.assertEqual(received[0], received[1])
        self.assertEqual(received[1], received[2])
        self.assertEqual(self.store.get_product("group1", "code")["stock"], 1)
        self.assertNotIn("SECRET_ONE", json.dumps(self.store.snapshot()))

    async def test_rcon_unknown_is_not_replayed_or_refunded(self):
        self.store.adjust_points("group1", "1", "add", 100, "admin", "fund")
        self.store.save_product("group1", {"id": "reward", "name": "游戏奖励", "price": 30, "kind": "rcon", "command": "give {player} diamond 1", "rules": [{"status": "SUCCESS", "pattern": "Done"}]}, "admin")
        self.gateway.responses = [OutcomeUnknown("timeout")]
        await self.domain.execute("purchase", self.ctx, "reward")
        order = self.store.orders("group1", "1")[0]
        await self.domain.execute("confirm", self.ctx, order["id"])
        await self.domain.execute("confirm", self.ctx, order["id"])
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.store.balance("group1", "1")["frozen"], 30)
        self.assertEqual(self.store.order(order["id"])["status"], "unknown")
        await self.domain.admin("order_resolve", {"order_id": order["id"], "outcome": "success", "reason": "已在游戏内核实"}, "admin")
        self.assertEqual(self.store.balance("group1", "1")["total"], 70)
        self.assertEqual(self.store.balance("group1", "1")["frozen"], 0)

    async def test_binding_code_consumed_and_notification_uses_origin(self):
        other = dict(self.ctx, user_id="2")
        result = await self.domain.execute("binding", other, "Another")
        code = result["text"].split("bind ")[1].split("\n")[0]
        notices = []
        async def notify(umo, text):
            notices.append((umo, text))
        self.domain.notify = notify
        handled = await self.domain.on_game_event("paper", {"event_name": "PlayerChatEvent", "message": "bind " + code, "player": {"uuid": UUID_B, "nickname": "Another"}})
        self.assertTrue(handled)
        self.assertEqual(self.store.binding("2")["mc_uuid"], UUID_B)
        self.assertEqual(notices[0][0], self.ctx["umo"])

    async def test_reserved_game_order_rechecks_binding_before_recovery_send(self):
        self.store.adjust_points("group1", "1", "add", 100, "admin", "fund")
        self.store.save_product("group1", {"id": "reward", "name": "游戏奖励", "price": 30, "kind": "rcon", "command": "give {player} diamond 1", "rules": [{"status": "SUCCESS", "pattern": "Done"}]}, "admin")
        await self.domain.execute("purchase", self.ctx, "reward")
        order = self.store.orders("group1", "1")[0]
        self.store.reserve_order(order["id"], "group1", "1")
        self.store.unbind("1", 0)
        challenge = self.store.begin_binding("1", "rincho", "paper", self.ctx["umo"])
        self.store.complete_binding(challenge["code"], UUID_A, "rincho", "paper", 0)
        result = await self.domain.execute("confirm", self.ctx, order["id"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.store.balance("group1", "1")["frozen"], 30)

    async def test_non_admin_cannot_adjust_other_points(self):
        result = await self.domain.execute("points_add", self.ctx, "2 500")
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.store.balance("group1", "2")["total"], 0)

    async def test_chat_admin_point_adjustment_preserves_reason_and_self_shorthand(self):
        admin = dict(self.ctx, is_admin=True)
        args = "2 12 周末活动 额外奖励"
        result = await self.domain.execute("points_add", admin, args)
        self.assertEqual(result["status"], "success")
        await self.domain.execute("points_add", admin, args)
        self.assertEqual(self.store.balance("group1", "2")["total"], 12)
        await self.domain.execute("points_sub", dict(admin, message_id="2"), "2 2")
        await self.domain.execute("points_set", dict(admin, message_id="3"), "7")
        self.assertEqual(self.store.balance("group1", "2")["total"], 10)
        self.assertEqual(self.store.balance("group1", "1")["total"], 7)
        with self.store.connection() as db:
            details = [json.loads(row[0]) for row in db.execute("SELECT detail FROM audit_log WHERE action='points_adjust' ORDER BY id")]
        self.assertEqual([d["reason"] for d in details], ["周末活动 额外奖励", "群管理员调整", "群管理员调整"])


if __name__ == "__main__":
    unittest.main()
