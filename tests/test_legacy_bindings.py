"""Synthetic legacy databases only; no user database is copied into the repo."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core.store import DomainError, Store


WORK = Path(__file__).resolve().parents[1] / "work" / "legacy-binding-tests"
WORK.mkdir(parents=True, exist_ok=True)
SESSION = "old_a:GroupMessage:20001"
FIRST = "old_a:User:10001"
UUID = "11111111-1111-4111-8111-111111111111"


class LegacyBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="migration-", dir=WORK)
        self.store = Store(Path(self.temp.name) / "new.sqlite")
        self.source = Path(self.temp.name) / "old.sqlite"

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def fixture(self, bindings=None):
        bindings = [(FIRST, "Alice")] if bindings is None else bindings
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript("""
                CREATE TABLE bindings(account_key TEXT PRIMARY KEY,player TEXT NOT NULL);
                CREATE TABLE balances(session TEXT,user_id TEXT,total INTEGER);
                CREATE TABLE checkins(id INTEGER,session TEXT,user_id TEXT,day TEXT,points INTEGER,credited INTEGER);
                CREATE TABLE session_settings(session TEXT,settings TEXT);
                CREATE TABLE purchases(id INTEGER,session TEXT,user_id TEXT,product_id TEXT,product_name TEXT,price INTEGER,status TEXT,product_snapshot TEXT,reward TEXT);
                CREATE TABLE used_codes(session TEXT,product_id TEXT,code TEXT,order_id INTEGER);
            """)
            db.executemany("INSERT INTO bindings VALUES(?,?)", bindings)
            db.execute("INSERT INTO balances VALUES(?,'10001',60)", (SESSION,))
            db.execute("INSERT INTO checkins VALUES(1,?,'10001','2026-10-30',10,1)", (SESSION,))
            product = dict(id="gift", name="Legacy gift", price=40, reward_type="codes", codes=["SECRET_USED", "SECRET_FREE"])
            db.execute("INSERT INTO session_settings VALUES(?,?)", (SESSION, json.dumps({"products": [product]})))
            db.execute("INSERT INTO purchases VALUES(1,?,'10001','gift','Legacy gift',40,'delivery_failed',?,'SECRET_USED')", (SESSION, json.dumps(product)))
            db.execute("INSERT INTO used_codes VALUES(?,'gift','SECRET_USED',1)", (SESSION,))
            db.commit()
        return self.source

    def preview(self, **options):
        return self.store.preview_legacy(self.source, **options)

    def apply(self, preview=None, **options):
        preview = preview or self.preview(**options)
        return self.store.import_legacy(self.source, preview["digest"], "test-admin", plan_digest=preview["plan_digest"], **options)

    def counts(self):
        with self.store.connection() as db:
            return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    for table in ("bindings", "binding_history", "accounts", "ledger", "signins", "products", "orders", "migration_batches")}

    def test_real_key_layout_preview_and_import_name_identity_without_uuid(self):
        self.fixture()
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        preview = self.preview(binding_server_id="paper")
        self.assertEqual(preview["binding_plan"]["counts"]["ready"], 1)
        self.assertTrue(preview["binding_plan"]["can_apply"])
        self.assertNotIn("SECRET_USED", json.dumps(preview))
        self.assertNotIn("SECRET_FREE", json.dumps(preview))
        self.assertEqual(self.counts()["bindings"], 0)
        result = self.apply(preview, binding_server_id="paper")
        self.assertEqual(result["bindings_added"], 1)
        binding = self.store.binding("10001")
        self.assertEqual(binding["identity"], "name:alice")
        self.assertEqual(binding["server_id"], "paper")
        self.assertIsNone(binding["mc_uuid"])
        self.assertFalse(binding["verified"])
        self.assertEqual(self.store.balance(SESSION, "10001")["total"], 60)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)
        self.assertFalse(Path(str(self.source) + "-wal").exists())
        self.assertFalse(Path(str(self.source) + "-shm").exists())

    def test_cross_platform_duplicates_collapse_and_completed_markers_prevent_resurrection(self):
        duplicate = "old_b:User:10001"
        self.fixture([(FIRST, "Alice"), (duplicate, "ALICE")])
        preview = self.preview(binding_server_id="paper")
        self.assertEqual(preview["binding_plan"]["counts"]["ready"], 1)
        self.assertEqual(preview["binding_plan"]["counts"]["duplicates"], 1)
        self.apply(preview, binding_server_id="paper")
        before = self.counts()
        repeated = self.apply(binding_server_id="paper")
        self.assertTrue(repeated["already_imported"])
        self.assertEqual(repeated["bindings_added"], 0)
        self.assertEqual(before, self.counts())
        self.store.unbind("10001", 0)
        self.assertEqual(self.apply(binding_server_id="paper")["bindings_added"], 0)
        self.assertIsNone(self.store.binding("10001"))

    def test_unselected_duplicate_also_cannot_resurrect_completed_pair(self):
        duplicate = "old_b:User:10001"
        self.fixture([(FIRST, "Alice"), (duplicate, "Alice")])
        self.apply(binding_server_id="paper", binding_choices=[FIRST])
        self.store.unbind("10001", 0)
        result = self.apply(binding_server_id="paper", binding_choices=[duplicate])
        self.assertEqual(result["binding_plan"]["counts"]["imported"], 1)
        self.assertIsNone(self.store.binding("10001"))

    def test_archived_old_batch_can_add_bindings_without_reimporting_finances(self):
        self.fixture()
        result = self.apply(binding_choices=[])
        # Simulate a batch created by the previous plugin release without markers.
        with self.store.connection(True) as db:
            summary = json.loads(db.execute("SELECT summary FROM migration_batches").fetchone()[0])
            summary.pop("binding_imported_keys")
            db.execute("UPDATE migration_batches SET summary=?", (json.dumps(summary),))
        self.store.adjust_points(SESSION, "10001", "add", 7, "admin", "after-import")
        before = self.counts()
        preview = self.preview(binding_server_id="paper", scope_map={SESSION: "another-scope"})
        self.assertTrue(preview["already_imported"])
        added = self.apply(preview, binding_server_id="paper", scope_map={SESSION: "another-scope"})
        self.assertEqual(added["bindings_added"], 1)
        self.assertEqual(self.store.balance(SESSION, "10001")["total"], 67)
        self.assertEqual(self.store.balance("another-scope", "10001")["total"], 0)
        after = self.counts()
        for table in ("accounts", "ledger", "signins", "products", "orders", "migration_batches"):
            self.assertEqual(before[table], after[table])
        self.assertEqual(result["digest"], added["digest"])

    def test_source_conflicts_require_explicit_selection_and_roll_back_whole_import(self):
        conflict_qq = "old_b:User:10001"
        conflict_player = "old_a:User:10002"
        self.fixture([(FIRST, "Alice"), (conflict_qq, "Bob"), (conflict_player, "Alice")])
        preview = self.preview(binding_server_id="paper")
        self.assertFalse(preview["binding_plan"]["can_apply"])
        self.assertEqual({c["code"] for c in preview["binding_plan"]["conflicts"]}, {"source_qq_conflict", "source_player_conflict"})
        with self.assertRaisesRegex(DomainError, "冲突"):
            self.apply(preview, binding_server_id="paper")
        self.assertTrue(all(count == 0 for count in self.counts().values()))
        result = self.apply(binding_server_id="paper", binding_choices=[FIRST])
        self.assertEqual(result["bindings_added"], 1)
        self.assertEqual(self.store.binding("10001")["player"], "Alice")
        self.assertIsNone(self.store.binding("10002"))

    def test_existing_qq_binding_is_never_overwritten(self):
        self.fixture()
        self.store.bind_name("10001", "Other", "paper", 0)
        preview = self.preview(binding_server_id="paper")
        self.assertEqual(preview["binding_plan"]["conflicts"][0]["code"], "target_qq_conflict")
        with self.assertRaises(DomainError):
            self.apply(preview, binding_server_id="paper")
        self.assertEqual(self.store.binding("10001")["player"], "Other")
        self.assertEqual(self.counts()["accounts"], 0)

    def test_existing_player_owner_is_never_reassigned(self):
        self.fixture()
        self.store.bind_name("10002", "alice", "paper", 0)
        preview = self.preview(binding_server_id="paper")
        self.assertEqual(preview["binding_plan"]["conflicts"][0]["code"], "target_player_conflict")
        with self.assertRaises(DomainError):
            self.apply(preview, binding_server_id="paper")
        self.assertIsNone(self.store.binding("10001"))

    def test_equal_verified_target_binding_keeps_uuid_version_and_cooldown(self):
        self.fixture()
        challenge = self.store.begin_binding("10001", "Alice", "paper", SESSION, now=100)
        self.store.complete_binding(challenge["code"], UUID, "Alice", "paper", 3600, now=101)
        original = self.store.binding("10001")
        preview = self.preview(binding_server_id="paper")
        self.assertEqual(preview["binding_plan"]["counts"]["existing"], 1)
        result = self.apply(preview, binding_server_id="paper")
        self.assertEqual(result["bindings_added"], 0)
        self.assertEqual(self.store.binding("10001"), original)
        self.assertEqual(self.counts()["binding_history"], 1)

    def test_server_mapping_precedence_supports_full_key_prefix_and_platform(self):
        second, third = "old_b:User:10002", "old_c:User:10003"
        self.fixture([(FIRST, "Alice"), (second, "Bob"), (third, "Carol")])
        mapping = {FIRST: "paper1", "old_a:User": "unused", "old_b:User": "paper2", "old_c": "paper3"}
        result = self.apply(binding_server_id="fallback", server_map=mapping)
        self.assertEqual(result["bindings_added"], 3)
        self.assertEqual([self.store.binding(qq)["server_id"] for qq in ("10001", "10002", "10003")], ["paper1", "paper2", "paper3"])

    def test_group_mapping_and_scope_routes_can_resolve_server(self):
        self.fixture()
        for mapping in ({SESSION: "paper"}, {"20001": "paper"}):
            preview = self.preview(server_map=mapping)
            self.assertEqual(preview["binding_plan"]["rows"][0]["server_id"], "paper")
        options = dict(scope_map={SESSION: "community"}, routes=[{"umo": "new:GroupMessage:20001", "scope": "community", "server_id": "paper"}])
        self.assertEqual(self.apply(**options)["bindings_added"], 1)
        self.assertEqual(self.store.balance("community", "10001")["total"], 60)

    def test_unresolved_or_ambiguous_server_cannot_be_guessed(self):
        self.fixture()
        self.assertFalse(self.preview()["binding_plan"]["can_apply"])
        routes = [{"umo": SESSION, "server_id": "paper1"}, {"scope": SESSION, "server_id": "paper2"}]
        self.assertFalse(self.preview(routes=routes)["binding_plan"]["can_apply"])
        self.assertTrue(self.preview(routes=routes, binding_server_id="paper1")["binding_plan"]["can_apply"])

    def test_plan_digest_covers_choices_mapping_and_target_state(self):
        self.fixture()
        preview = self.preview(binding_server_id="paper")
        for options in ({"binding_server_id": "paper2"}, {"binding_server_id": "paper", "binding_choices": []}):
            with self.assertRaisesRegex(DomainError, "plan_digest"):
                self.apply(preview, **options)
        with self.assertRaisesRegex(DomainError, "plan_digest"):
            self.store.import_legacy(self.source, preview["digest"], "admin", binding_server_id="paper")
        self.store.bind_name("10001", "Alice", "paper", 0)
        with self.assertRaisesRegex(DomainError, "plan_digest"):
            self.apply(preview, binding_server_id="paper")
        self.assertEqual(self.counts()["accounts"], 0)

    def test_unknown_choices_or_unsupported_source_identity_are_rejected(self):
        self.fixture([("unknown:10001", "Alice"), (FIRST, "not valid")])
        preview = self.preview(binding_server_id="paper")
        self.assertEqual(preview["binding_plan"]["counts"]["blocked"], 2)
        with self.assertRaisesRegex(DomainError, "account_key"):
            self.preview(binding_server_id="paper", binding_choices=["absent:User:10001"])
        self.assertEqual(self.apply(binding_choices=[])["bindings_added"], 0)

    def test_nonempty_wal_is_rejected_until_consistent_checkpoint(self):
        self.fixture()
        db = sqlite3.connect(self.source)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("UPDATE balances SET total=61")
            db.commit()
            with self.assertRaisesRegex(DomainError, "WAL"):
                self.preview(binding_server_id="paper")
        finally:
            db.close()
        self.assertEqual(self.preview(binding_server_id="paper")["balance_totals"][SESSION], 61)

    def test_source_change_invalidates_original_preview(self):
        self.fixture()
        preview = self.preview(binding_server_id="paper")
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE balances SET total=61")
            db.commit()
        with self.assertRaisesRegex(DomainError, "源数据库已变化"):
            self.apply(preview, binding_server_id="paper")
        self.assertTrue(all(count == 0 for count in self.counts().values()))

    def test_binding_write_failure_rolls_back_finances_and_partial_bindings(self):
        self.fixture([(FIRST, "Alice"), ("old_a:User:10002", "Bob")])
        original = self.store._save_binding
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DomainError("synthetic insertion failure")
            return original(*args, **kwargs)

        with patch.object(self.store, "_save_binding", side_effect=fail_second):
            with self.assertRaisesRegex(DomainError, "synthetic"):
                self.apply(binding_server_id="paper")
        self.assertTrue(all(count == 0 for count in self.counts().values()))


if __name__ == "__main__":
    unittest.main()
