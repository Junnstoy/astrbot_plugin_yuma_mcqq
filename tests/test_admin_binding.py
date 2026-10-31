"""Administrator binding operations against real isolated SQLite databases."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from core.store import DomainError, Store
from services.domain import Domain


WORK = Path(__file__).resolve().parents[1] / "work"
WORK.mkdir(exist_ok=True)
UUID_A = "11111111-1111-4111-8111-111111111111"


class Settings:
    def __init__(self):
        self.data = {"timezone": "Asia/Shanghai", "binding_verification_enabled": True,
                     "binding_cooldown_seconds": 604800, "points_name": "点数"}
        self.route_data = {"scope": "group", "server_id": "paper", "enabled": True}

    def route(self, umo):
        return self.route_data


class AdminBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="admin-binding-")
        self.store = Store(Path(self.temp.name) / "data.sqlite3")
        self.settings = Settings()
        self.domain = Domain(self.store, self.settings, object())
        self.admin = {"umo": "bot:GroupMessage:1", "scope": "group", "server_id": "paper",
                      "user_id": "90001", "is_admin": True, "message_id": "1"}
        self.user = dict(self.admin, user_id="10001", is_admin=False)

    async def asyncTearDown(self):
        await self.domain.close()
        self.store.close()
        self.temp.cleanup()

    async def test_admin_can_bind_rebind_unbind_target_without_verification_or_cd(self):
        result = await self.domain.execute("binding", self.admin, "10001 Rincho")
        self.assertEqual(result["status"], "success")
        first = self.store.binding("10001")
        self.assertEqual(first["identity"], "name:rincho")
        self.assertIsNone(first["mc_uuid"])
        self.assertFalse(first["verified"])
        self.assertGreater(first["cooldown_until"], time.time())
        self.assertIsNone(self.store.binding("90001"))
        self.assertEqual((await self.domain.execute("rebind", self.admin, "10001 Another"))["status"], "success")
        second = self.store.binding("10001")
        self.assertGreater(second["version"], first["version"])
        self.assertEqual((await self.domain.execute("unbind", self.admin, "10001"))["status"], "success")
        self.assertIsNone(self.store.binding("10001"))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM binding_challenges").fetchone()[0], 0)
            histories = db.execute("SELECT qq_id,actor FROM binding_history ORDER BY id").fetchall()
            audits = db.execute("SELECT actor,detail FROM audit_log WHERE action LIKE 'binding_%'").fetchall()
        self.assertEqual([(r["qq_id"], r["actor"]) for r in histories], [("10001", "90001")] * 3)
        self.assertEqual([r["actor"] for r in audits], ["90001"] * 3)
        self.assertTrue(all(json.loads(r["detail"])["target"] == "10001" for r in audits))
        self.assertTrue(all(json.loads(r["detail"])["admin_override"] for r in audits))
        self.settings.data["binding_verification_enabled"] = False
        rejected = await self.domain.execute("binding", self.user, "Third")
        self.assertEqual(rejected["status"], "error")
        self.assertIn("冷却", rejected["text"])

    async def test_admin_self_shorthand_also_ignores_current_and_historic_cd(self):
        for action, args in (("binding", "Rincho"), ("rebind", "Another"), ("unbind", ""), ("binding", "Rincho")):
            result = await self.domain.execute(action, self.admin, args)
            self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.store.binding("90001")["player"], "Rincho")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM binding_challenges").fetchone()[0], 0)

    async def test_members_cannot_target_others_and_keep_original_cooldown(self):
        self.settings.data["binding_verification_enabled"] = False
        await self.domain.execute("binding", self.user, "Rincho")
        for action, args in (("binding", "10002 Other"), ("rebind", "10002 Other"), ("unbind", "10002")):
            rejected = await self.domain.execute(action, self.user, args)
            self.assertEqual(rejected["status"], "error")
            self.assertIn("管理员", rejected["text"])
            self.assertEqual(self.store.binding("10001")["player"], "Rincho")
            self.assertIsNone(self.store.binding("10002"))
        for action, args in (("rebind", "Another"), ("unbind", "")):
            rejected = await self.domain.execute(action, self.user, args)
            self.assertIn("冷却", rejected["text"])
        # Ordinary binding still creates a challenge when the switch is enabled.
        self.settings.data["binding_verification_enabled"] = True
        pending = await self.domain.execute("binding", dict(self.user, user_id="10003"), "Third")
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(self.store.binding("10003"))

    async def test_numeric_only_admin_bind_is_usage_error_instead_of_player_name(self):
        for action in ("binding", "rebind"):
            result = await self.domain.execute(action, self.admin, "10001")
            self.assertEqual(result["status"], "error")
            self.assertIn("QQ号 MC名", result["text"])
        self.assertEqual(self.store.snapshot()["bindings"], [])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM binding_challenges").fetchone()[0], 0)

    async def test_members_keep_numeric_names_and_admins_can_use_explicit_target(self):
        self.settings.data.update(binding_verification_enabled=False, binding_cooldown_seconds=0)
        bound = await self.domain.execute("binding", self.user, "12345")
        self.assertEqual(bound["status"], "success")
        self.assertEqual(self.store.binding("10001")["identity"], "name:12345")
        changed = await self.domain.execute("rebind", self.user, "67890")
        self.assertEqual(changed["status"], "success")
        self.assertEqual(self.store.binding("10001")["player"], "67890")
        admin_numeric = await self.domain.execute("binding", self.admin, "90001 12345")
        self.assertEqual(admin_numeric["status"], "success")
        self.assertEqual(self.store.binding("90001")["identity"], "name:12345")

    async def test_admin_override_does_not_steal_name_or_downgrade_same_verified_uuid(self):
        code = self.store.begin_binding("10001", "Rincho", "paper", self.user["umo"])["code"]
        verified = self.store.complete_binding(code, UUID_A, "Rincho", "paper", 604800)["binding"]
        retained = await self.domain.execute("binding", self.admin, "10001 RINCHO")
        self.assertEqual(retained["status"], "success")
        self.assertEqual(self.store.binding("10001")["mc_uuid"], UUID_A)
        self.assertEqual(self.store.binding("10001")["version"], verified["version"])
        stolen = await self.domain.execute("binding", self.admin, "10002 rincho")
        self.assertEqual(stolen["status"], "error")
        self.assertIn("其他 QQ", stolen["text"])
        self.assertIsNone(self.store.binding("10002"))

    async def test_gui_unbind_uses_admin_override_by_default_and_returns_feedback(self):
        self.store.bind_name("10001", "Rincho", "paper", 604800)
        self.store.adjust_points("group", "10001", "add", 20, "fixture", "opening")
        result = await self.domain.admin("binding_unbind", {"user_id": "10001", "force": False}, "web-admin")
        self.assertIsNone(self.store.binding("10001"))
        self.assertIn("已解绑", result["text"])
        self.assertEqual(self.store.balance("group", "10001")["total"], 20)
        with self.store.connection() as db:
            history = db.execute("SELECT actor FROM binding_history ORDER BY id DESC LIMIT 1").fetchone()
            audit = db.execute("SELECT actor,detail FROM audit_log WHERE action='binding_unbind'").fetchone()
        self.assertEqual(history[0], "web-admin")
        self.assertEqual(audit["actor"], "web-admin")
        self.assertEqual(json.loads(audit["detail"])["target"], "10001")
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.store.bind_name("10001", "Another", "paper", 0)

    async def test_admin_bind_and_same_name_confirmation_invalidate_pending_codes(self):
        code = self.store.begin_binding("10001", "OldName", "paper", self.user["umo"])["code"]
        await self.domain.execute("binding", self.admin, "10001 Rincho")
        with self.assertRaisesRegex(DomainError, "验证码无效"):
            self.store.complete_binding(code, UUID_A, "OldName", "paper", 0)
        # A same-name proof can be requested during the name cooldown.
        upgrade = self.store.begin_binding("10001", "Rincho", "paper", self.user["umo"])["code"]
        await self.domain.execute("binding", self.admin, "10001 Rincho")
        with self.assertRaisesRegex(DomainError, "验证码无效"):
            self.store.complete_binding(upgrade, UUID_A, "Rincho", "paper", 0)
        self.assertFalse(self.store.binding("10001")["verified"])

    async def test_admin_unbind_cancels_a_pending_only_binding_and_records_target(self):
        code = self.store.begin_binding("10001", "Rincho", "paper", self.user["umo"])["code"]
        result = await self.domain.admin("binding_unbind", {"user_id": "10001"}, "web-admin")
        self.assertTrue(result["cancelled_pending"])
        self.assertIn("验证码已作废", result["text"])
        self.assertIsNone(self.store.binding("10001"))
        with self.assertRaisesRegex(DomainError, "验证码无效"):
            self.store.complete_binding(code, UUID_A, "Rincho", "paper", 0)
        with self.store.connection() as db:
            audit = db.execute("SELECT actor,detail FROM audit_log WHERE action='binding_pending_cancel'").fetchone()
        self.assertEqual(audit["actor"], "web-admin")
        self.assertEqual(json.loads(audit["detail"])["target"], "10001")

    async def test_failed_admin_rebind_preserves_old_identity_and_pending_proof(self):
        self.store.bind_name("10001", "Rincho", "paper", 0)
        self.store.bind_name("10002", "Taken", "paper", 0)
        challenge = self.store.begin_binding("10001", "Rincho", "paper", self.user["umo"])["code"]
        before = self.store.binding("10001")
        result = await self.domain.execute("rebind", self.admin, "10001 TAKEN")
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.store.binding("10001"), before)
        verified = self.store.complete_binding(challenge, UUID_A, "Rincho", "paper", 0)
        self.assertEqual(verified["binding"]["mc_uuid"], UUID_A)


if __name__ == "__main__":
    unittest.main()
