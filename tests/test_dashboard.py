"""SQL paging is bounded, deterministic, scoped before counting, and read-only."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.store import Store
from services.dashboard import read_page


WORK = Path(__file__).resolve().parents[1] / "work" / "dashboard-tests"
WORK.mkdir(parents=True, exist_ok=True)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.store = Store(Path(self.temp.name) / "domain.sqlite")
        self.ops = sqlite3.connect(":memory:")
        self.ops.executescript("""
            CREATE TABLE runs(id TEXT PRIMARY KEY,umo TEXT,action TEXT,server_id TEXT,status TEXT,text TEXT,created_at REAL,updated_at REAL);
            CREATE TABLE audit(id INTEGER PRIMARY KEY,actor TEXT,action TEXT,detail TEXT,created_at REAL);
        """)

    def tearDown(self):
        self.ops.close()
        self.store.close()
        self.temp.cleanup()

    def populate(self, count=31):
        with self.store.connection(True) as db:
            for index in range(count):
                qq = f"{10000 + index}"
                scope = "group-a" if index % 2 == 0 else "group-b"
                db.execute("INSERT INTO bindings VALUES(?,?,NULL,?,'paper',1,100,0)", (qq, "name:p" + qq, "P" + qq))
                db.execute("INSERT INTO accounts VALUES(?,?,100,10)", (scope, qq))
                db.execute("INSERT INTO ledger(operation_id,scope,qq_id,delta,total_after,reason,actor,created) VALUES(?,?,?,10,100,'signin','system',100)", ("ledger-" + qq, scope, qq))
                db.execute("INSERT INTO signins VALUES(?,?,?,10,?,'SIGNED',100)", (scope, qq, "2026-10-31", "signin-" + qq))
                self.ops.execute("INSERT INTO runs VALUES(?,?,'custom:list','paper','SUCCESS','SECRET_RAW_RCON',100,100)", ("run-" + qq, "private-umo"))
                self.ops.execute("INSERT INTO audit(actor,action,detail,created_at) VALUES('admin','test','safe-detail',100)")
        self.ops.commit()

    def page(self, table, **options):
        return read_page(self.store, self.ops, dict(table=table, **options))

    def test_scope_is_applied_before_total_and_page_clamping(self):
        self.populate()
        for table in ("balances", "ledger", "signins"):
            result = self.page(table, scope="group-a", page_size=10, page=999999999999)
            self.assertEqual((result["total"], result["page"], result["pages"]), (16, 2, 2))
            self.assertEqual(len(result["rows"]), 6)
            self.assertTrue(all(row["scope"] == "group-a" for row in result["rows"]))
        self.assertEqual(self.page("balances", scope="group-a")["rows"][0]["available"], 90)

    def test_scope_values_are_bound_parameters(self):
        self.populate()
        result = self.page("ledger", scope="group-a' OR 1=1 --", page=50)
        self.assertEqual(result, dict(rows=[], total=0, page=1, page_size=20, pages=1))
        self.assertEqual(self.page("ledger")["total"], 31)

    def test_global_tables_do_not_invent_a_scope(self):
        self.populate()
        for table in ("bindings", "runs", "audit"):
            self.assertEqual(self.page(table, scope="nonexistent"), self.page(table))

    def test_all_page_sizes_and_stable_tie_order_across_all_tables(self):
        self.populate()
        for table in ("bindings", "balances", "ledger", "signins", "runs", "audit"):
            expected = self.page(table, page_size=100)["rows"]
            for size in (10, 20, 50, 100):
                first = self.page(table, page_size=size)
                rows = []
                for page in range(1, first["pages"] + 1):
                    result = self.page(table, page_size=size, page=page)
                    self.assertLessEqual(len(result["rows"]), size)
                    rows.extend(result["rows"])
                self.assertEqual(rows, expected, table)

    def test_empty_and_negative_pages_clamp_to_one(self):
        for table in ("bindings", "balances", "ledger", "signins", "runs", "audit"):
            for page in (-50, 0, 100):
                self.assertEqual(self.page(table, page=page), dict(rows=[], total=0, page=1, page_size=20, pages=1))

    def test_invalid_requests_cannot_select_arbitrary_tables_or_limits(self):
        for payload in (None, [], {"table": "product_codes"}, {"table": "accounts; DROP TABLE accounts"},
                        {"table": "bindings", "page_size": 1000}, {"table": "bindings", "page_size": True},
                        {"table": "bindings", "page": 1.5}, {"table": "bindings", "page": True},
                        {"table": "bindings", "scope": ["group-a"]}):
            with self.assertRaises(ValueError):
                read_page(self.store, self.ops, payload)
        self.assertEqual(self.page("balances")["total"], 0)

    def test_binding_boolean_and_cooldown_match_store_identity_rules(self):
        self.store.bind_name("10001", "Player", "paper", 600, now=100)
        row = self.page("bindings")["rows"][0]
        self.assertIs(row["verified"], False)
        self.assertIsNone(row["mc_uuid"])
        self.assertEqual(row["cooldown_until"], 700)

    def test_runs_exclude_raw_replies_and_private_origin(self):
        self.populate(1)
        row = self.page("runs")["rows"][0]
        self.assertEqual(set(row), {"id", "action", "server_id", "status", "created_at", "updated_at"})
        self.assertNotIn("SECRET_RAW_RCON", str(row))
        self.assertNotIn("private-umo", str(row))

    def test_read_does_not_commit_or_change_existing_ops_transaction(self):
        self.ops.execute("INSERT INTO audit(actor,action,detail,created_at) VALUES('admin','pending','detail',100)")
        changes = self.ops.total_changes
        self.assertTrue(self.ops.in_transaction)
        self.assertEqual(self.page("audit")["total"], 1)
        self.assertEqual(self.ops.total_changes, changes)
        self.assertTrue(self.ops.in_transaction)
        self.ops.rollback()
        self.assertEqual(self.page("audit")["total"], 0)
        self.assertFalse(self.ops.in_transaction)

    def test_more_than_previous_snapshot_limit_is_reachable(self):
        self.populate(1005)
        result = self.page("bindings", page_size=10, page=101)
        self.assertEqual((result["total"], result["pages"], result["page"]), (1005, 101, 101))
        self.assertEqual(len(result["rows"]), 5)


if __name__ == "__main__":
    unittest.main()
