"""Top-ten group ranking uses current MC names without disclosing QQ IDs."""
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from core.store import Store
from services.domain import Domain
from tests.test_domain_revision import Gateway, Settings


WORK = Path(__file__).resolve().parents[1] / "work"
WORK.mkdir(exist_ok=True)


class RankTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="rank-")
        self.store = Store(Path(self.temp.name) / "domain.sqlite3")
        self.settings, self.gateway = Settings(), Gateway()
        self.domain = Domain(self.store, self.settings, self.gateway)
        self.ctx = {"umo": self.settings.route_data["umo"], "scope": "group1", "server_id": "paper", "user_id": "viewer", "message_id": "rank"}

    async def asyncTearDown(self):
        await self.domain.close()
        self.store.close()
        self.temp.cleanup()

    def account(self, qq_id, total, *, player=None, scope="group1", frozen=0):
        with self.store.connection(True) as db:
            db.execute("INSERT INTO accounts VALUES(?,?,?,?)", (scope, str(qq_id), total, frozen))
        if player:
            self.store.bind_name(str(qq_id), player, "paper", 0)

    async def test_fixed_top_ten_current_player_names_and_scope_isolation(self):
        for number in range(15):
            self.account(str(800000000 + number), 200 - number, player=f"Player_{number}")
        self.account("900000000", 99999, player="AnotherScope", scope="other")
        result = await self.domain.execute("rank", self.ctx)
        lines = result["text"].splitlines()
        self.assertEqual(lines[0], "樱花币排行榜")
        self.assertEqual(len(lines), 11)
        self.assertEqual(lines[1], "1. Player_0：200")
        self.assertEqual(lines[10], "10. Player_9：191")
        self.assertNotIn("Player_10", result["text"])
        self.assertNotIn("AnotherScope", result["text"])
        for number in range(15):
            self.assertNotIn(str(800000000 + number), json.dumps(result))
        rows = self.store.rank("group1")
        self.assertEqual(len(rows), 10)
        self.assertTrue(all("qq_id" not in row for row in rows))
        self.assertEqual(self.gateway.calls, [])

    async def test_unbound_accounts_remain_ranked_without_qq_fallback(self):
        self.account("123456789", 300)
        self.account("234567890", 200, player="BoundPlayer")
        result = await self.domain.execute("rank", self.ctx)
        self.assertEqual(result["text"], "樱花币排行榜\n1. 未绑定玩家：300\n2. BoundPlayer：200")
        self.assertNotIn("123456789", json.dumps(result))

    async def test_rebind_and_unbind_use_current_identity_instead_of_history(self):
        self.account("123456789", 100, player="FormerName")
        self.store.bind_name("123456789", "CurrentName", "paper", 0, rebind=True)
        result = await self.domain.execute("rank", self.ctx)
        self.assertIn("CurrentName", result["text"])
        self.assertNotIn("FormerName", result["text"])
        self.store.unbind("123456789", 0)
        result = await self.domain.execute("rank", self.ctx)
        self.assertEqual(result["text"], "樱花币排行榜\n1. 未绑定玩家：100")
        with self.store.connection() as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM binding_history").fetchone()[0], 0)

    async def test_total_includes_frozen_points_and_ties_keep_original_id_order(self):
        # Text QQ ordering is deliberate: the previous query also sorted TEXT IDs.
        self.account("2", 200, player="TieSecond")
        self.account("10", 200, player="TieFirst", frozen=190)
        self.account("3", 199, player="Third")
        rows = self.store.rank("group1")
        self.assertEqual([row["player"] for row in rows], ["TieFirst", "TieSecond", "Third"])
        self.assertEqual((rows[0]["total"], rows[0]["frozen"]), (200, 190))
        result = await self.domain.execute("rank", self.ctx)
        self.assertEqual(result["text"].splitlines()[1:3], ["1. TieFirst：200", "2. TieSecond：200"])

    async def test_empty_scope_keeps_existing_empty_message(self):
        self.account("123456789", 10, player="Other", scope="other")
        result = await self.domain.execute("rank", self.ctx)
        self.assertEqual(result["text"], "樱花币排行榜\n暂无记录")

    async def test_single_join_query_avoids_per_player_lookups(self):
        self.account("123456789", 10, player="One")
        self.account("234567890", 9)
        queries = []
        original = self.store.connection
        @contextmanager
        def traced(write=False):
            with original(write) as db:
                db.set_trace_callback(queries.append)
                yield db
        with patch.object(self.store, "connection", traced), patch.object(self.store, "binding", side_effect=AssertionError("no per-row lookup")):
            self.store.rank("group1")
        selects = [query for query in queries if query.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1)
        self.assertIn("LEFT JOIN bindings", selects[0])


if __name__ == "__main__":
    unittest.main()
