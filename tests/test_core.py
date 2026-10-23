"""Run with: python -m unittest discover -s tests -v (no AstrBot import)."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
import sqlite3
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

PACKAGE = "checkin_points_test_package"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[PACKAGE] = package
from checkin_points_test_package.core import DEFAULT_TEMPLATES, award_points, classify_mc_output, render_commands, validate_settings
from checkin_points_test_package.store import LEGACY_TEMPLATES, LEGACY_USAGE, Store


class SettingsTests(unittest.TestCase):
    def test_defaults_are_independent(self):
        first, second = validate_settings({}), validate_settings({})
        first["windows"].clear()
        self.assertEqual(second["windows"], [{"start": "00:00", "end": "23:59"}])
        self.assertFalse(second["enabled"])
        self.assertEqual(second["checkin_prefix"], "")

    def test_both_command_prefixes_are_independent_and_validated(self):
        configured = validate_settings({"checkin_prefix": "  mc  ", "command_prefix": "萌妹点"})
        self.assertEqual(configured["checkin_prefix"], "mc")
        self.assertEqual(configured["command_prefix"], "萌妹点")
        self.assertEqual(validate_settings({"command_prefix": "shop"})["checkin_prefix"], "")
        self.assertEqual(validate_settings({"checkin_prefix": "mc"})["command_prefix"], "")
        for key in ("checkin_prefix", "command_prefix"):
            for value in (None, True, 123, [], {}, "a" * 31, "two words", "a\tb", "a\nb", "a\x00b"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate_settings({key: value})
            self.assertEqual(validate_settings({key: ""})[key], "")

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

    def test_mc_result_keywords_and_failure_precedence(self):
        for output, expected in [("§a签到成功§r", "sent"), ("成功给玩家 Alex 签到!", "sent"),
                                 ("今日签到已完成!", "sent"), ("Successfully signed in!", "sent"),
                                 ("今天已经签到了", "failed"), ("Please don't sign in again", "failed"),
                                 ("签到成功，但执行失败", "failed"), ("player is not exist", "failed"),
                                 ("accepted", "unknown"), ("", "unknown"), (None, "unknown")]:
            with self.subTest(output=output):
                state, clean = classify_mc_output(output, validate_settings({}))
                self.assertEqual(state, expected)
                self.assertNotIn("§", clean)

    def test_extended_validation(self):
        for value in [{"currency_name": ""}, {"command_prefix": "two words"},
                      {"admin_ids": ["bad user"]}, {"confirmation_minutes": 0},
                      {"mc_success_keywords": [""]}, {"templates": {"balance": "{total.__class__}"}},
                      {"templates": {"balance": "{total!r}"}}, {"templates": {"fake": "hi"}},
                      {"products": [{"id": "1", "name": "A", "price": -1}]},
                      {"products": [{"id": "1", "name": "A", "price": 1, "reward_type": "codes", "codes": ["a", "a"]}]},
                      {"products": [{"id": "1", "name": "A", "price": 1, "reward_text": ""}]}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings(value)

    def test_private_template_must_render_actual_reward(self):
        for template in ["", "订单 {order_id} 已完成", "奖励占位符字样：{{reward}}"]:
            with self.subTest(template=template), self.assertRaises(ValueError):
                validate_settings({"templates": {"purchase_private": template}})
        self.assertEqual(validate_settings({"templates": {"purchase_private": "领取：{reward}"}})
                         ["templates"]["purchase_private"], "领取：{reward}")

    def test_product_prices_roundtrip_through_browser_safe_integers(self):
        product = dict(id="safe", name="奖品", price=2**53-1, reward_text="奖励")
        self.assertEqual(validate_settings({"products": [product]})["products"][0]["price"], 2**53-1)
        for price in [2**53, 2**63-1, 1.5, True]:
            with self.subTest(price=price), self.assertRaises(ValueError):
                validate_settings({"products": [{**product, "price": price}]})

    def test_purchase_limit_validation(self):
        product = dict(id="limited", name="奖品", price=1, reward_text="奖励")
        self.assertEqual(validate_settings({"products": [product]})["products"][0]["purchase_limit"], 0)
        self.assertEqual(validate_settings({})["currency_name"], "点数")
        for limit in [-1, True, 1.5, "2", 2**53]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                validate_settings({"products": [{**product, "purchase_limit": limit}]})

    def test_makeup_and_leaderboard_command_validation(self):
        settings = validate_settings({"commands": ["signin click {date} {player}"]})
        self.assertEqual(settings["makeup_commands"], ["signin click {date} {player}"])
        self.assertEqual(settings["leaderboard_command"], "signin leaderboard")
        self.assertEqual(render_commands(settings, "Alex", 10, "1", date="2020-02-29"), ["signin click 2020-02-29 Alex"])
        for value in [None, "2020-2-29", "2021-02-29", "2020-01-01\nstop"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                render_commands(settings, "Alex", 10, "1", date=value)
        self.assertEqual(render_commands({"commands": ["signin {player}"]}, "Alex", 10, "1"), ["signin Alex"])
        for value in [{"makeup_commands": ["signin {date.__class__}"]},
                      {"makeup_commands": ["signin {date}\nstop"]},
                      {"leaderboard_command": "signin leaderboard\nstop"},
                      {"leaderboard_command": "signin {player}"}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings(value)


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

    def test_group_prefixes_roundtrip_per_session_and_invalid_save_keeps_configuration(self):
        self.configure(checkin_prefix="mc", command_prefix="shop")
        self.store.save_settings("other:GroupMessage:1", {**self.settings, "checkin_prefix": "qd", "command_prefix": "points"})
        restarted = Store(self.path)
        for session, expected in ((self.session, ("mc", "shop")), ("other:GroupMessage:1", ("qd", "points"))):
            settings = restarted.get_settings(session)
            self.assertEqual((settings["checkin_prefix"], settings["command_prefix"]), expected)
        before = restarted.get_settings(self.session)
        with self.assertRaises(ValueError):
            restarted.save_settings(self.session, {**before, "checkin_prefix": "two words"})
        self.assertEqual(restarted.get_settings(self.session), before)

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


    def test_failed_unknown_mc_never_credit_or_retry(self):
        self.mc_settings()
        for user, state in [("1", "failed"), ("2", "unknown")]:
            record = self.checkin(user, "Alex")
            self.assertFalse(record["credited"])
            self.assertEqual(self.store.balance(self.session, user), 0)
            self.assertTrue(self.store.claim_mc(record["id"]))
            self.store.set_mc_state(record["id"], state)
            self.assertFalse(self.store.get_checkin(record["id"])["credited"])
            self.assertEqual(self.checkin(user, "Alex")["status"], "duplicate")
            self.store.set_mc_state(record["id"], "sent")
            self.assertEqual(self.store.balance(self.session, user), 0)

    def test_concurrent_mc_finalize_and_admin_adjust(self):
        self.mc_settings()
        record = self.checkin(player="Alex")
        self.store.claim_mc(record["id"])
        def work(index):
            if index % 2:
                self.store.set_mc_state(record["id"], "sent")
            else:
                self.store.adjust_balance(self.session, "10001", "add", 1, "admin")
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(work, range(40)))
        self.assertEqual(self.store.balance(self.session, "10001"), 31)
        self.assertTrue(self.store.get_checkin(record["id"])["credited"])

    def test_adjust_audit_ranking_and_overflow(self):
        self.assertEqual(self.store.adjust_balance(self.session, "1", "set", 20, "admin")["total"], 20)
        self.assertEqual(self.store.adjust_balance(self.session, "1", "subtract", 5, "admin")["delta"], -5)
        self.store.adjust_balance(self.session, "2", "add", 30, "admin")
        self.assertEqual(self.store.leaderboard(self.session)[0], {"user_id": "2", "total": 30})
        with self.assertRaises(ValueError):
            self.store.adjust_balance(self.session, "1", "subtract", 16, "admin")
        self.store.adjust_balance(self.session, "3", "set", 2**63-1, "admin")
        with self.assertRaises(ValueError):
            self.store.adjust_balance(self.session, "3", "add", 1, "admin")
        with self.assertRaises(ValueError):
            self.checkin("3")
        self.assertEqual(self.store.balance(self.session, "3"), 2**63-1)
        self.assertEqual(len(self.store.audit(self.session)), 4)

    def test_delete_session_preserves_assets_and_cancels_inflight(self):
        self.checkin("1")
        self.mc_settings()
        first, second = self.checkin("2", "Alex"), self.checkin("3", "Steve")
        self.store.claim_mc(first["id"])
        self.store.delete_session(self.session)
        self.assertFalse(self.store.get_settings(self.session)["enabled"])
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.balance(self.session, "1"), 11)
        self.assertEqual(len(self.store.history(self.session)), 3)
        self.store.save_settings(self.session, self.settings)
        self.store.set_mc_state(first["id"], "sent")
        self.assertEqual(self.store.balance(self.session, "2"), 0)
        self.assertFalse(self.store.claim_mc(second["id"]))

    def test_toggle_and_binding_lookup(self):
        self.store.sync_bindings({"one:User:1": "Alex", "two:User:2": "Alex", "one:User:3": "alex"})
        self.assertEqual(self.store.lookup_users("one", "Alex"), ["1", "3"])
        self.store.toggle_session(self.session, False)
        self.assertEqual(self.checkin()["status"], "disabled")
        self.store.toggle_session(self.session, True)
        self.assertEqual(self.checkin()["status"], "success")

    def test_v1_migration_preserves_assets_without_recredit(self):
        self.mc_settings()
        record = self.checkin("1", "Alex")
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO balances(session,user_id,total) VALUES(?,?,?)", (self.session, "1", 11))
            db.execute("UPDATE checkins SET total=11")
            db.execute("ALTER TABLE checkins DROP COLUMN credited")
            db.execute("PRAGMA user_version=1")
        db.close()
        upgraded = Store(self.path)
        self.assertTrue(upgraded.get_checkin(record["id"])["credited"])
        self.assertEqual(upgraded.balance(self.session, "1"), 11)
        upgraded.claim_mc(record["id"])
        upgraded.set_mc_state(record["id"], "sent")
        self.assertEqual(upgraded.balance(self.session, "1"), 11)
        Store(self.path)
        self.assertEqual(upgraded.balance(self.session, "1"), 11)

    def test_disable_then_reenable_cannot_credit_old_mc_job(self):
        self.mc_settings()
        record = self.checkin("1", "Alex")
        self.store.claim_mc(record["id"])
        self.store.toggle_session(self.session, False)
        self.store.toggle_session(self.session, True)
        self.store.set_mc_state(record["id"], "sent")
        self.assertFalse(self.store.get_checkin(record["id"])["credited"])
        self.assertEqual(self.store.balance(self.session, "1"), 0)
        second = self.checkin("2", "Steve")
        self.store.claim_mc(second["id"])
        self.configure(mc_enabled=False)
        self.mc_settings()
        self.store.set_mc_state(second["id"], "sent")
        self.assertFalse(self.store.get_checkin(second["id"])["credited"])

    def test_mc_credit_overflow_is_recorded_without_wrapping(self):
        self.mc_settings()
        self.store.adjust_balance(self.session, "1", "set", 2**63-1, "admin")
        record = self.checkin("1", "Alex")
        self.store.claim_mc(record["id"])
        self.store.set_mc_state(record["id"], "sent")
        self.assertEqual(self.store.get_checkin(record["id"])["mc_state"], "failed")
        self.assertFalse(self.store.get_checkin(record["id"])["credited"])
        self.assertEqual(self.store.balance(self.session, "1"), 2**63-1)

    def test_v3_migrates_only_old_currency_and_default_usage_once(self):
        old = {**self.settings, "currency_name": "积分", "templates": dict(LEGACY_TEMPLATES)}
        custom = {**self.settings, "currency_name": "金币", "templates": {
            "usage": "自定义积分使用说明", "purchase_pending": "自定义兑换提醒", "purchase_failed": "自定义重试说明"}}
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE session_settings SET settings=? WHERE session=?", (json.dumps(old), self.session))
            db.execute("INSERT INTO session_settings(session,settings) VALUES(?,?)", ("custom", json.dumps(custom)))
            db.execute("PRAGMA user_version=2")
        db.close()
        upgraded = Store(self.path)
        self.assertEqual(upgraded.get_settings(self.session)["currency_name"], "点数")
        self.assertEqual(upgraded.get_settings(self.session)["templates"]["usage"], DEFAULT_TEMPLATES["usage"])
        for key in LEGACY_TEMPLATES:
            self.assertEqual(upgraded.get_settings(self.session)["templates"][key], DEFAULT_TEMPLATES[key])
        self.assertEqual(upgraded.get_settings("custom")["currency_name"], "金币")
        self.assertEqual(upgraded.get_settings("custom")["templates"]["usage"], "自定义积分使用说明")
        self.assertEqual(upgraded.get_settings("custom")["templates"]["purchase_pending"], "自定义兑换提醒")
        self.assertEqual(upgraded.get_settings("custom")["templates"]["purchase_failed"], "自定义重试说明")
        upgraded.save_settings("custom", {**custom, "currency_name": "积分"})
        self.assertEqual(Store(self.path).get_settings("custom")["currency_name"], "积分")

    def test_makeup_ignores_window_uses_target_date_multiplier_and_real_audit_time(self):
        self.mc_settings()
        self.configure(windows=[{"start": "00:00", "end": "00:00"}],
                       date_rules=[{"date": "2020-01-01", "multiplier": 2}])
        self.assertEqual(self.checkin("1")["status"], "outside_window")
        record = self.store.makeup_checkin(self.session, "1", "Alex", "2020-01-01", "admin-1")
        self.assertEqual((record["status"], record["points"], record["day"], record["credited"]),
                         ("success", 22, "2020-01-01", False))
        self.assertEqual(self.store.balance(self.session, "1"), 0)
        self.store.claim_mc(record["id"])
        self.store.set_mc_state(record["id"], "sent")
        self.assertTrue(self.store.get_checkin(record["id"])["credited"])
        self.assertFalse(record["created_at"].startswith("2020-01-01"))
        audit = next(row for row in self.store.audit(self.session) if row["operation"] == "makeup_requested")
        self.assertEqual((audit["delta"], audit["actor"]), (0, "admin-1"))
        duplicate = self.store.makeup_checkin(self.session, "1", "Alex", "2020-01-01", "admin-2")
        self.assertEqual((duplicate["status"], duplicate["total"]), ("duplicate", 22))
        self.assertEqual(sum(row["operation"] == "makeup_requested" for row in self.store.audit(self.session)), 1)

    def test_makeup_future_or_invalid_dates_never_change_balance(self):
        for day in ["2999-01-01", "2020-1-01", "2021-02-29", "2020-01-01\nstop", "2020-01-01T12:00:00"]:
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.store.makeup_checkin(self.session, "1", "Alex", day, "admin")
        self.assertEqual(self.store.balance(self.session, "1"), 0)
        self.assertEqual(self.store.history(self.session), [])

    def test_makeup_today_allowed_and_future_uses_session_local_day(self):
        self.mc_settings()
        with patch(f"{PACKAGE}.store.local_now", return_value=datetime(2026, 10, 23, 23, 59)):
            self.assertEqual(self.store.makeup_checkin(self.session, "1", "Alex", "2026-10-23", "admin")["status"], "success")
            with self.assertRaises(ValueError):
                self.store.makeup_checkin(self.session, "1", "Alex", "2026-10-24", "admin")
        with patch(f"{PACKAGE}.store.local_now", return_value=datetime(2026, 10, 24, 0, 0)):
            self.assertEqual(self.store.makeup_checkin(self.session, "1", "Alex", "2026-10-24", "admin")["status"], "success")

    def test_makeup_requires_mc_and_binding_even_when_binding_is_optional(self):
        with self.assertRaisesRegex(ValueError, "开启 MC 联动"):
            self.store.makeup_checkin(self.session, "1", "Alex", "2020-01-01", "admin")
        self.assertEqual(self.store.history(self.session), [])
        self.assertEqual(self.store.audit(self.session), [])
        self.assertEqual(self.store.balance(self.session, "1"), 0)
        self.mc_settings(require_binding=False)
        for player in ["", "@a", "Alex\nstop"]:
            with self.subTest(player=player):
                self.assertEqual(self.store.makeup_checkin(self.session, "1", player, "2020-01-01", "admin")["status"], "unbound")
        self.assertEqual(self.store.history(self.session), [])
        self.assertEqual(self.store.audit(self.session), [])
        # The transaction reads the latest configuration, not the caller's cached one.
        self.configure(mc_enabled=False)
        with self.assertRaisesRegex(ValueError, "开启 MC 联动"):
            self.store.makeup_checkin(self.session, "1", "Alex", "2020-01-01", "admin")
        self.assertEqual(self.checkin("1")["status"], "success")
        self.assertEqual(self.store.balance(self.session, "1"), 11)

    def test_makeup_mc_success_required_and_failed_day_cannot_replay(self):
        self.mc_settings()
        for user, state in [("1", "sent"), ("2", "failed"), ("3", "unknown")]:
            record = self.store.makeup_checkin(self.session, user, "Alex", "2020-01-01", "admin")
            self.assertEqual(record["commands"], ["signin click 2020-01-01 Alex"])
            self.assertFalse(record["credited"])
            self.store.claim_mc(record["id"])
            self.store.set_mc_state(record["id"], state)
            self.assertEqual(self.store.balance(self.session, user), 11 if state == "sent" else 0)
            self.assertEqual(self.store.makeup_checkin(self.session, user, "Alex", "2020-01-01", "admin")["status"], "duplicate")

    def test_makeup_honors_enabled_binding_and_configured_mc_commands(self):
        self.mc_settings()
        self.assertEqual(self.store.makeup_checkin(self.session, "1", "", "2020-01-01", "admin")["status"], "unbound")
        self.configure(makeup_commands=[])
        with self.assertRaisesRegex(ValueError, "补签指令"):
            self.store.makeup_checkin(self.session, "1", "Alex", "2020-01-01", "admin")
        self.configure(enabled=False)
        self.assertEqual(self.store.makeup_checkin(self.session, "1", "Alex", "2020-01-01", "admin")["status"], "disabled")

    def test_normal_command_date_is_current_session_day(self):
        self.mc_settings()
        self.configure(commands=["signin click {date} {player}"])
        self.assertEqual(self.checkin(player="Alex")["commands"], ["signin click 2026-10-23 Alex"])

    def test_reset_archives_record_preserves_points_and_allows_explicit_second_award(self):
        first = self.checkin("1")
        reset = self.store.reset_checkin(self.session, "1", "admin", now=self.today)
        self.assertEqual((reset["removed"], reset["day"], reset["total"]), (True, "2026-10-23", 11))
        self.assertIsNone(self.store.get_checkin(first["id"]))
        archived = self.store.history(self.session)[0]
        for key in ("id", "player", "points", "total", "created_at", "commands", "mc_state", "credited"):
            self.assertEqual(archived[key], first[key])
        self.assertEqual(archived["actor"], "admin")
        self.assertTrue(archived["reset_at"])
        second = self.checkin("1")
        self.assertGreater(second["id"], first["id"])
        self.assertEqual(second["total"], 22)
        self.assertEqual(len(self.store.history(self.session)), 2)
        self.assertEqual(self.store.history(self.session, 1)[0]["id"], second["id"])

    def test_reset_pending_stops_stale_worker_and_sending_is_rejected(self):
        self.mc_settings()
        first = self.checkin("1", "Alex")
        self.store.reset_checkin(self.session, "1", "admin", now=self.today)
        self.assertFalse(self.store.claim_mc(first["id"]))
        self.store.set_mc_state(first["id"], "sent")
        self.assertEqual(self.store.balance(self.session, "1"), 0)
        second = self.checkin("1", "Alex")
        self.store.claim_mc(second["id"])
        with self.assertRaisesRegex(ValueError, "正在执行"):
            self.store.reset_checkin(self.session, "1", "admin", now=self.today)
        self.assertEqual(self.store.get_checkin(second["id"])["mc_state"], "sending")
        self.store.set_mc_state(second["id"], "failed")
        self.assertTrue(self.store.reset_checkin(self.session, "1", "admin", now=self.today)["removed"])

    def test_reset_only_todays_target_user_in_current_session(self):
        self.checkin("1", now=datetime(2026, 10, 22))
        self.checkin("2")
        self.store.save_settings("other", self.settings)
        self.store.checkin("other", "1", now=self.today)
        result = self.store.reset_checkin(self.session, "1", "admin", now=self.today)
        self.assertFalse(result["removed"])
        self.assertEqual(len(self.store.history(self.session)), 2)
        self.assertEqual(len(self.store.history("other")), 1)

    def test_v4_migration_keeps_v3_balances_and_adds_reset_archive(self):
        self.checkin("1")
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE checkin_resets")
            db.execute("PRAGMA user_version=3")
        db.close()
        upgraded = Store(self.path)
        self.assertEqual(upgraded.balance(self.session, "1"), 11)
        self.assertTrue(upgraded.reset_checkin(self.session, "1", "admin", now=self.today)["removed"])
        self.assertEqual(upgraded.history(self.session)[0]["actor"], "admin")


class ShopTests(unittest.TestCase):
    session = "mybot:GroupMessage:123456789"
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "checkin.sqlite3")
        self.settings = {"enabled": True, "products": [
            {"id": "text", "name": "奖励链接", "price": 10, "reward_type": "text", "reward_text": "https://example.com/private"},
            {"id": "code", "name": "兑换码", "price": 30, "reward_type": "codes", "codes": ["secret-A", "secret-B"]},
        ]}
        self.store.save_settings(self.session, self.settings)
        self.store.adjust_balance(self.session, "1", "set", 100, "admin")
        self.store.adjust_balance(self.session, "2", "set", 100, "admin")

    def create(self, user="1", product="code"):
        return self.store.create_purchase(self.session, user, product, self.now)

    def confirm(self, user="1", now=None):
        return self.store.confirm_purchase(self.session, user, now or self.now)

    def test_pending_confirmation_snapshots_and_one_charge(self):
        pending = self.create()
        self.assertEqual((pending["status"], pending["total"], pending["price"]), ("pending", 100, 30))
        with ThreadPoolExecutor(max_workers=10) as pool:
            records = list(pool.map(lambda _: self.confirm(), range(20)))
        self.assertEqual({row["reward"] for row in records}, {"secret-A"})
        self.assertEqual({row["id"] for row in records}, {pending["id"]})
        self.assertEqual(self.store.balance(self.session, "1"), 70)
        self.assertEqual(self.store.product_catalog(self.session)[1]["stock"], 1)

    def test_delivery_claim_and_retry_same_reward_no_charge(self):
        self.create()
        paid = self.confirm()
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(self.store.claim_delivery, [paid["id"]] * 16))
        self.assertEqual(sum(claims), 1)
        self.store.finish_delivery(paid["id"], False, "failure secret-A")
        retry = self.confirm()
        self.assertEqual(retry["reward"], "secret-A")
        self.assertTrue(self.store.claim_delivery(retry["id"]))
        self.store.finish_delivery(retry["id"], True)
        self.assertEqual(self.confirm()["status"], "delivered")
        self.assertFalse(self.store.claim_delivery(retry["id"]))
        self.assertEqual(self.store.balance(self.session, "1"), 70)
        self.assertIsNone(self.store.active_purchase(self.session, "1"))

    def test_recovery_preserves_paid_reward(self):
        self.create()
        paid = self.confirm()
        self.store.claim_delivery(paid["id"])
        reopened = Store(self.store.path)
        reopened.recover_inflight()
        retry = reopened.confirm_purchase(self.session, "1", self.now)
        self.assertEqual((retry["status"], retry["reward"], retry["total"]), ("delivery_failed", "secret-A", 70))
        self.assertTrue(reopened.claim_delivery(paid["id"]))

    def test_expiry_and_replacement_do_not_charge(self):
        first = self.create()
        second = self.create(product="text")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.store.orders(self.session)[1]["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.confirm(now=self.now + timedelta(minutes=3))
        self.assertEqual(self.store.balance(self.session, "1"), 100)
        self.assertEqual(self.store.orders(self.session)[0]["status"], "expired")

    def test_balance_rechecked_at_confirmation(self):
        self.create()
        self.store.adjust_balance(self.session, "1", "set", 10, "admin")
        with self.assertRaises(ValueError):
            self.confirm()
        self.assertEqual(self.store.product_catalog(self.session)[1]["stock"], 2)
        self.assertEqual(self.store.balance(self.session, "1"), 10)

    def test_stock_competition_assigns_code_once(self):
        self.settings["products"][1]["codes"] = ["only-one"]
        self.store.save_settings(self.session, self.settings)
        self.create("1")
        self.create("2")
        def attempt(user):
            try:
                return self.confirm(user)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["1", "2"]))
        self.assertEqual(sum(row is not None for row in results), 1)
        self.assertEqual(sum(self.store.balance(self.session, user) for user in ["1", "2"]), 170)

    def test_used_codes_never_resurrect_when_settings_or_session_recreated(self):
        self.create()
        paid = self.confirm()
        self.store.claim_delivery(paid["id"])
        self.store.finish_delivery(paid["id"], True)
        self.store.save_settings(self.session, self.settings)
        self.assertEqual(self.store.product_catalog(self.session)[1]["stock"], 1)
        self.store.delete_session(self.session)
        self.store.save_settings(self.session, self.settings)
        self.create()
        self.assertEqual(self.confirm()["reward"], "secret-B")

    def test_delete_session_cancels_unpaid_but_keeps_paid_delivery(self):
        self.create("1")
        self.create("2")
        self.confirm("2")
        self.store.delete_session(self.session)
        with self.assertRaises(ValueError):
            self.confirm("1")
        self.assertEqual(self.store.balance(self.session, "1"), 100)
        self.assertEqual(self.confirm("2")["reward"], "secret-A")
        self.assertEqual(self.store.balance(self.session, "2"), 70)

    def test_changed_products_require_new_confirmation(self):
        self.create()
        self.settings["products"][1]["price"] = 40
        self.store.save_settings(self.session, self.settings)
        with self.assertRaises(ValueError):
            self.confirm()
        self.assertEqual(self.store.balance(self.session, "1"), 100)

    def test_public_order_and_catalog_never_expose_rewards(self):
        self.create()
        paid = self.confirm()
        self.store.claim_delivery(paid["id"])
        self.store.finish_delivery(paid["id"], False, "failed to send secret-A")
        public = self.store.orders(self.session)[0]
        self.assertNotIn("reward", public)
        self.assertNotIn("product_snapshot", public)
        self.assertNotIn("secret-A", str(public))
        self.assertNotIn("secret", str(self.store.product_catalog(self.session)))

    def test_unfinished_paid_order_blocks_another_purchase(self):
        self.create()
        self.confirm()
        with self.assertRaises(ValueError):
            self.create(product="text")
        self.assertEqual(len(self.store.orders(self.session)), 1)

    def delivered(self, order):
        self.assertTrue(self.store.claim_delivery(order["id"]))
        self.store.finish_delivery(order["id"], True)

    def test_allocated_codes_removed_from_storage_settings_and_sessions(self):
        self.create()
        paid = self.confirm()
        self.assertEqual(self.store.get_settings(self.session)["products"][1]["codes"], ["secret-B"])
        self.assertEqual(self.store.sessions()[0]["settings"]["products"][1]["codes"], ["secret-B"])
        self.store.claim_delivery(paid["id"])
        self.store.finish_delivery(paid["id"], False)
        cleaned = self.store.save_settings(self.session, self.settings)
        with sqlite3.connect(self.store.path) as db:
            raw = json.loads(db.execute("SELECT settings FROM session_settings WHERE session=?", (self.session,)).fetchone()[0])
        db.close()
        self.assertEqual(cleaned["products"][1]["codes"], ["secret-B"])
        self.assertEqual(raw["products"][1]["codes"], ["secret-B"])
        self.assertEqual(self.confirm()["reward"], "secret-A")

    def test_two_pending_users_can_buy_after_pool_changes(self):
        self.create("1")
        self.create("2")
        with ThreadPoolExecutor(max_workers=2) as pool:
            orders = list(pool.map(self.confirm, ["1", "2"]))
        self.assertEqual({row["reward"] for row in orders}, {"secret-A", "secret-B"})
        self.assertEqual(self.store.get_settings(self.session)["products"][1]["codes"], [])
        self.assertEqual([self.store.balance(self.session, user) for user in ["1", "2"]], [70, 70])

    def test_confirm_and_admin_add_code_do_not_lose_new_inventory(self):
        self.create()
        expanded = {**self.settings, "products": [dict(p) for p in self.settings["products"]]}
        expanded["products"][1]["codes"] = ["secret-A", "secret-B", "secret-C"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            buy = pool.submit(self.confirm)
            save = pool.submit(self.store.save_settings, self.session, expanded)
            buy.result()
            save.result()
        self.assertEqual(self.store.get_settings(self.session)["products"][1]["codes"], ["secret-B", "secret-C"])

    def test_purchase_limit_counts_paid_once_and_delivery_retries_do_not_count_again(self):
        self.settings["products"][0]["purchase_limit"] = 2
        self.store.save_settings(self.session, self.settings)
        self.create(product="text")
        first = self.confirm()
        for _ in range(3):
            self.store.claim_delivery(first["id"])
            self.store.finish_delivery(first["id"], False)
            self.assertEqual(self.confirm()["id"], first["id"])
        self.delivered(first)
        self.create(product="text")
        second = self.confirm()
        self.delivered(second)
        with self.assertRaisesRegex(ValueError, "限购"):
            self.create(product="text")
        self.assertEqual(self.store.balance(self.session, "1"), 80)
        self.assertEqual(self.store.product_catalog(self.session)[0]["purchase_limit"], 2)
        self.create("2", "text")
        self.assertEqual(self.confirm("2")["status"], "paid")

    def test_limit_rechecked_using_latest_settings_at_confirmation(self):
        self.create(product="text")
        self.delivered(self.confirm())
        self.create(product="text")
        self.settings["products"][0]["purchase_limit"] = 1
        self.store.save_settings(self.session, self.settings)
        with self.assertRaisesRegex(ValueError, "限购"):
            self.confirm()
        self.assertEqual(self.store.balance(self.session, "1"), 90)
        self.settings["products"][0]["purchase_limit"] = 2
        self.store.save_settings(self.session, self.settings)
        self.assertEqual(self.confirm()["status"], "paid")

    def test_expired_cancelled_pending_do_not_use_purchase_limit(self):
        self.settings["products"][0]["purchase_limit"] = 1
        self.store.save_settings(self.session, self.settings)
        self.create(product="text")
        self.create(product="text")
        with self.assertRaises(ValueError):
            self.confirm(now=self.now + timedelta(minutes=3))
        self.create(product="text")
        self.delivered(self.confirm())
        with self.assertRaisesRegex(ValueError, "限购"):
            self.create(product="text")

    def test_limit_and_code_usage_persist_across_delete_and_recreate(self):
        self.settings["products"][1]["purchase_limit"] = 1
        self.store.save_settings(self.session, self.settings)
        self.create()
        self.delivered(self.confirm())
        self.store.delete_session(self.session)
        self.store.save_settings(self.session, self.settings)
        self.assertEqual(self.store.get_settings(self.session)["products"][1]["codes"], ["secret-B"])
        with self.assertRaisesRegex(ValueError, "限购"):
            self.create()


if __name__ == "__main__":
    unittest.main()
