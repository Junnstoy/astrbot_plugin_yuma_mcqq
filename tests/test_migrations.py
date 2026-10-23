"""Compatibility checks for independent check-in prefix settings."""
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path

package = types.ModuleType("checkin_migration_test_package")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
from checkin_migration_test_package.core import DEFAULT_TEMPLATES
from checkin_migration_test_package.store import Store, V3_USAGE


class PrefixMigrationTests(unittest.TestCase):
    def test_v4_upgrade_preserves_assets_custom_templates_and_other_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "points.sqlite3"
            store = Store(path)
            for session, prefix in (("group-a", "shop"), ("group-b", "market")):
                store.save_settings(session, {"enabled": True, "command_prefix": prefix,
                                              "min_points": 10, "max_points": 10,
                                              "products": [{"id": "one", "name": "测试奖品", "price": 2,
                                                            "reward_type": "codes", "codes": ["FAKE-TEST-CODE"]}]})
                store.checkin(session, "100")
                store.create_purchase(session, "100", "one")
                store.confirm_purchase(session, "100")
            with closing(sqlite3.connect(path)) as db, db:
                for session, text in (("group-a", V3_USAGE), ("group-b", "自定义帮助 {command_list}")):
                    raw = json.loads(db.execute("SELECT settings FROM session_settings WHERE session=?", (session,)).fetchone()[0])
                    raw.pop("checkin_prefix")
                    raw["templates"]["usage"] = text
                    db.execute("UPDATE session_settings SET settings=? WHERE session=?", (json.dumps(raw), session))
                db.execute("PRAGMA user_version=4")
                original = {name: db.execute("SELECT * FROM " + name).fetchall()
                            for name in ("balances", "checkins", "balance_audit", "purchases", "used_codes")}
            migrated = Store(path)
            first, second = migrated.get_settings("group-a"), migrated.get_settings("group-b")
            self.assertEqual(first["checkin_prefix"], "")
            self.assertEqual(second["checkin_prefix"], "")
            self.assertEqual(first["command_prefix"], "shop")
            self.assertEqual(second["command_prefix"], "market")
            self.assertEqual(first["templates"]["usage"], DEFAULT_TEMPLATES["usage"])
            self.assertEqual(second["templates"]["usage"], "自定义帮助 {command_list}")
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
                for name, rows in original.items():
                    self.assertEqual(db.execute("SELECT * FROM " + name).fetchall(), rows)
            self.assertEqual(Store(path).get_settings("group-a"), first)

    def test_upgrade_keeps_an_existing_checkin_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "points.sqlite3"
            store = Store(path)
            expected = store.save_settings("group", {"checkin_prefix": "mc", "command_prefix": "shop"})
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("PRAGMA user_version=4")
            self.assertEqual(Store(path).get_settings("group"), expected)
