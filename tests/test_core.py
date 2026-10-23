"""Run with: python -m unittest discover -s tests -v (no AstrBot import)."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

PACKAGE = "checkin_points_test_package"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[PACKAGE] = package
from checkin_points_test_package.core import award_points, validate_settings
from checkin_points_test_package.store import Store


class SettingsTests(unittest.TestCase):
    def test_defaults_are_independent(self):
        first, second = validate_settings({}), validate_settings({})
        first["windows"].clear()
        self.assertEqual(second["windows"], [{"start": "00:00", "end": "23:59"}])
        self.assertFalse(second["enabled"])

    def test_invalid_values(self):
        cases = [
            {"min_points": -1}, {"min_points": True}, {"min_points": 31},
            {"enabled": "false"}, {"timezone": "not-a-timezone"},
            {"windows": []}, {"windows": [{"start": "24:00", "end": "23:59"}]},
            {"date_rules": [{"date": "2026-02-29", "multiplier": 2}]},
            {"date_rules": [{"date": "10-23", "multiplier": "NaN"}]},
            {"date_rules": [{"date": "10-23", "multiplier": 0}]},
            {"date_rules": [{"date": "10-23", "multiplier": "1e-9999"}]},
            {"date_rules": [{"date": "10-23", "multiplier": 2}] * 2},
            {"commands": ["signin {player.__class__}"]},
            {"commands": ["signin {player!s}"]}, {"commands": ["signin {player:>20}"]},
            {"commands": ["signin {unknown}"]}, {"commands": ["signin {player"]},
            {"commands": ["signin {player}\nstop"]}, {"commands": ["say \x85"]},
            {"mc_enabled": True},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_settings(case)

    def test_round_half_up(self):
        self.assertEqual(award_points(11, Decimal("1.5")), 17)
        self.assertEqual(award_points(10, Decimal("0.25")), 3)


class StoreTests(unittest.TestCase):
    session = "mybot:GroupMessage:123456789"
    today = datetime(2026, 10, 23, 12, 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "checkin.sqlite3"
        self.store = Store(self.path)
        self.settings = {"enabled": True, "min_points": 11, "max_points": 11}
        self.store.save_settings(self.session, self.settings)

    def configure(self, **kwargs):
        self.settings.update(kwargs)
        self.store.save_settings(self.session, self.settings)

    def mc_settings(self, **kwargs):
        self.configure(mc_enabled=True, server_name="Server", commands=["signin {player}", "points {user_id} {points}"], **kwargs)

    def checkin(self, user="10001", player="", now=None):
        return self.store.checkin(self.session, user, player, self.today if now is None else now)

    def test_disabled_and_unconfigured_sessions(self):
        self.assertEqual(self.store.checkin("another", "1", now=self.today)["status"], "disabled")
        self.configure(enabled=False)
        self.assertEqual(self.checkin()["status"], "disabled")
        self.assertEqual(self.store.history(self.session), [])

    def test_daily_duplicate_and_session_isolation(self):
        self.assertEqual(self.checkin()["points"], 11)
        duplicate = self.checkin()
        self.assertEqual((duplicate["status"], duplicate["total"]), ("duplicate", 11))
        self.assertEqual(self.checkin(now=datetime(2026, 10, 24))["total"], 22)
        self.store.save_settings("other:GroupMessage:1", self.settings)
        other = self.store.checkin("other:GroupMessage:1", "10001", now=self.today)
        self.assertEqual(other["total"], 11)
        self.assertEqual(len(self.store.history(self.session)), 2)
        self.assertEqual(Store(self.path).balance(self.session, "10001"), 22)

    def test_random_range_is_inclusive(self):
        self.configure(min_points=10, max_points=30)
        with patch(f"{PACKAGE}.store.secrets.randbelow", return_value=0) as random:
            self.assertEqual(self.checkin("1")["points"], 10)
            random.assert_called_once_with(21)
        with patch(f"{PACKAGE}.store.secrets.randbelow", return_value=20):
            self.assertEqual(self.checkin("2")["points"], 30)

    def test_specific_date_overrides_recurring_and_default(self):
        self.configure(date_rules=[{"date": "10-23", "multiplier": 2},
                                   {"date": "2026-10-23", "multiplier": 1.5},
                                   {"date": "02-29", "multiplier": 3}])
        self.assertEqual(self.checkin()["points"], 17)
        self.assertEqual(self.checkin(now=datetime(2027, 10, 23))["points"], 22)
        self.assertEqual(self.checkin(now=datetime(2027, 10, 24))["points"], 11)
        self.assertEqual(self.checkin(now=datetime(2028, 2, 29))["points"], 33)

    def test_windows_include_end_minute_and_cross_midnight(self):
        self.configure(windows=[{"start": "22:00", "end": "06:00"}, {"start": "12:00", "end": "12:00"}])
        for index, (hour, minute, second, expected) in enumerate([
            (21, 59, 59, "outside_window"), (22, 0, 0, "success"),
            (6, 0, 59, "success"), (6, 1, 0, "outside_window"),
            (12, 0, 59, "success"), (12, 1, 0, "outside_window"),
        ]):
            with self.subTest(hour=hour, minute=minute):
                result = self.checkin(str(index), now=datetime(2026, 10, 23, hour, minute, second))
                self.assertEqual(result["status"], expected)

    def test_timezone_midnight(self):
        first = self.checkin(now=datetime(2026, 10, 23, 15, 59, tzinfo=timezone.utc))
        second = self.checkin(now=datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc))
        self.assertEqual((first["day"], second["day"]), ("2026-10-23", "2026-10-24"))
        self.assertEqual(second["total"], 22)

    def test_binding_requirement_only_for_mc(self):
        self.assertEqual(self.checkin()["status"], "success")
        self.mc_settings()
        self.assertEqual(self.checkin("2")["status"], "unbound")
        self.assertEqual(self.store.balance(self.session, "2"), 0)
        self.configure(require_binding=False)
        result = self.checkin("2")
        self.assertEqual((result["status"], result["mc_state"], result["commands"]), ("success", "skipped", []))

    def test_command_snapshot_and_single_claim(self):
        self.mc_settings()
        record = self.checkin(player="Alex")
        self.assertEqual(record["commands"], ["signin Alex", "points 10001 11"])
        self.assertEqual(record["mc_state"], "pending")
        self.configure(commands=["new-signin {player}"], server_name="Other")
        self.assertEqual(self.store.pending_mc()[0]["commands"], record["commands"])
        self.assertEqual(self.store.pending_mc()[0]["server_name"], "Server")
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(self.store.claim_mc, [record["id"]] * 16))
        self.assertEqual(sum(claims), 1)
        self.store.set_mc_state(record["id"], "sent", "OK")
        self.store.set_mc_state(record["id"], "failed", "late overwrite")
        persisted = self.store.history(self.session)[0]
        self.assertEqual((persisted["mc_state"], persisted["mc_detail"]), ("sent", "OK"))
        self.assertFalse(self.store.claim_mc(record["id"]))

    def test_restart_marks_inflight_unknown_but_preserves_pending(self):
        self.mc_settings()
        first, second = self.checkin("1", "Alex"), self.checkin("2", "Steve")
        self.assertTrue(self.store.claim_mc(first["id"]))
        reopened = Store(self.path)
        self.assertEqual(reopened.recover_inflight(), 1)
        self.assertEqual(reopened.recover_inflight(), 0)
        self.assertFalse(reopened.claim_mc(first["id"]))
        self.assertEqual([row["id"] for row in reopened.pending_mc()], [second["id"]])
        self.assertEqual(self.checkin("1", "Alex")["status"], "duplicate")

    def test_concurrent_duplicate_and_balance_atomicity(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            records = list(pool.map(lambda _: self.checkin(), range(30)))
        self.assertEqual(sum(row["status"] == "success" for row in records), 1)
        self.assertEqual(self.store.balance(self.session, "10001"), 11)
        self.assertEqual(len(self.store.history(self.session)), 1)

    def test_concurrent_different_days_accumulate(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda day: self.checkin(now=datetime(2026, 10, day)), range(1, 11)))
        self.assertTrue(all(row["status"] == "success" for row in records))
        self.assertEqual(self.store.balance(self.session, "10001"), 110)

    def test_command_injection_rolls_back_award(self):
        self.mc_settings()
        for user, player in [("1", "Alex\nstop"), ("1", "@a"), ("user injected", "Alex")]:
            with self.subTest(user=user, player=player), self.assertRaises(ValueError):
                self.checkin(user, player)
            self.assertEqual(self.store.balance(self.session, user), 0)
        self.assertEqual(self.store.history(self.session), [])
        self.configure(commands=["points {user_id:} {points}"])
        with self.assertRaises(ValueError):
            self.checkin("user injected", "Alex")
        self.assertEqual(self.checkin("1", "Alex")["status"], "success")

    def test_binding_mirror_replaces_and_removes_unbound(self):
        key = "mybot:User:10001"
        self.assertEqual(self.store.sync_bindings({key: "Alex", "other:User:1": "Steve"}), 2)
        self.assertEqual(self.store.get_binding(key), "Alex")
        self.assertEqual(self.store.sync_bindings({key: "NewName"}), 1)
        self.assertEqual(self.store.get_binding("other:User:1"), "")
        self.assertEqual(self.store.get_binding(key), "NewName")
        self.assertEqual(self.store.sync_bindings({}), 0)
        self.assertEqual(self.store.get_binding(key), "")


if __name__ == "__main__":
    unittest.main()
