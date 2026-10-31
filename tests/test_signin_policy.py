"""Daily windows and date-based rewards with synthetic clocks and databases."""
import copy
import importlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from core.config import Settings as ConfigSettings
from core.signin_policy import normalize_signin_policy, preview_signin_policy
from core.store import MAX_POINTS, Store
from services.domain import Domain
from tests.test_domain_revision import Gateway, Settings, SIGNED


WORK = Path(__file__).resolve().parents[1] / "work"
WORK.mkdir(exist_ok=True)


def event(**change):
    return {"id": "festival", "name": "活动", "start_date": "2026-10-30", "end_date": "2026-10-31",
            "mode": "multiplier", "multiplier": "2", "priority": 0, "enabled": True, **change}


class PolicyPreviewTests(unittest.TestCase):
    def test_default_does_not_mutate_or_draw(self):
        route = {"points_min": 3, "points_max": 9}
        original = copy.deepcopy(route)
        with patch("services.domain.secrets.randbelow", side_effect=AssertionError("must not draw")):
            result = preview_signin_policy(route, "2026-10-31T12:00")
        self.assertEqual(route, original)
        self.assertTrue(result["allowed"])
        self.assertIsNone(result["event"])
        self.assertIsNone(result["final_points"])
        self.assertEqual(result["effective_range"], {"min": 3, "max": 9})

    def test_minute_inclusive_bounds(self):
        route = {"signin_time": {"enabled": True, "start": "08:30", "end": "09:00"}}
        for moment, allowed in (("08:29:59.999999", False), ("08:30:00", True), ("09:00:59.999999", True), ("09:01:00", False)):
            with self.subTest(moment=moment):
                self.assertEqual(preview_signin_policy(route, "2026-10-31T" + moment)["allowed"], allowed)

    def test_overnight_equal_minutes_and_disabled_window(self):
        route = {"signin_time": {"enabled": True, "start": "22:00", "end": "02:00"}}
        for hour, allowed in (("21:59", False), ("22:00", True), ("23:59", True), ("00:00", True), ("02:00:59", True), ("02:01", False)):
            self.assertEqual(preview_signin_policy(route, "2026-10-31T" + hour)["allowed"], allowed)
        route["signin_time"].update(start="12:00", end="12:00")
        self.assertTrue(preview_signin_policy(route, "2026-10-31T12:00:59")["allowed"])
        self.assertFalse(preview_signin_policy(route, "2026-10-31T12:01")["allowed"])
        route["signin_time"]["enabled"] = False
        self.assertTrue(preview_signin_policy(route, "2026-10-31T12:01")["allowed"])

    def test_local_date_timezone_and_dst_gap(self):
        route = {"timezone": "Asia/Shanghai", "reward_events": [event()]}
        self.assertIsNotNone(preview_signin_policy(route, "2026-10-31T15:59:59Z")["event"])
        after = preview_signin_policy(route, "2026-10-31T16:00:00Z")
        self.assertEqual(after["day"], "2026-11-01")
        self.assertIsNone(after["event"])
        with self.assertRaisesRegex(ValueError, "夏令时"):
            preview_signin_policy({"timezone": "America/New_York"}, "2026-03-08T02:30")

    def test_date_endpoints_and_event_priority_do_not_stack(self):
        low = event()
        high = event(id="bonus", start_date="2026-10-31", mode="range", points_min=31, points_max=35, priority=10)
        route = {"points_min": 10, "points_max": 20, "reward_events": [low, high]}
        before = preview_signin_policy(route, "2026-10-29T23:59")
        first = preview_signin_policy(route, "2026-10-30T00:00")
        overlap = preview_signin_policy(route, "2026-10-31T23:59:59")
        after = preview_signin_policy(route, "2026-11-01T00:00")
        self.assertIsNone(before["event"])
        self.assertEqual(first["effective_range"], {"min": 20, "max": 40})
        self.assertEqual(overlap["event"]["id"], "bonus")
        self.assertEqual(overlap["effective_range"], {"min": 31, "max": 35})
        self.assertEqual(after["effective_range"], {"min": 10, "max": 20})

    def test_decimal_floor_and_range_draw_validation(self):
        route = {"points_min": 3, "points_max": 100, "reward_events": [event(multiplier="1.13")]}
        self.assertEqual(preview_signin_policy(route, "2026-10-31T12:00", 100)["final_points"], 113)
        self.assertEqual(preview_signin_policy(route, "2026-10-31T12:00", 3)["final_points"], 3)
        route["reward_events"] = [event(mode="range", points_min=0, points_max=0, multiplier="1000")]
        self.assertEqual(preview_signin_policy(route, "2026-10-31T12:00", 0)["final_points"], 0)
        for sample in (-1, 1, True, 0.1):
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                preview_signin_policy(route, "2026-10-31T12:00", sample)

    def test_disabled_event_and_maximum_supported_reward(self):
        route = {"points_min": 1_000_000, "points_max": 1_000_000, "reward_events": [event(multiplier="1000")]}
        self.assertEqual(preview_signin_policy(route, "2026-10-31", 1_000_000)["final_points"], 1_000_000_000)
        route["reward_events"][0]["enabled"] = False
        self.assertEqual(preview_signin_policy(route, "2026-10-31", 1_000_000)["final_points"], 1_000_000)

    def test_domain_preview_has_no_store_gateway_or_random_side_effect(self):
        store, gateway = Mock(), Mock()
        domain = Domain(store, Settings(), gateway)
        with patch("services.domain.secrets.randbelow", side_effect=AssertionError("must not draw")):
            result = domain.preview_signin_policy({"reward_events": [event()]}, "2026-10-31T12:00", 10)
        self.assertEqual(result["final_points"], 20)
        self.assertEqual(store.mock_calls, [])
        self.assertEqual(gateway.mock_calls, [])

    def test_preview_rejects_invalid_date_time_timezone_and_route_with_value_errors(self):
        for route, moment in (([], "2026-10-31"), ({"timezone": "Not/A_Zone"}, "2026-10-31"),
                              ({}, "not a date"), ({}, 123), ({}, "2026-10-31T24:00")):
            with self.subTest(route=route, moment=moment), self.assertRaises(ValueError):
                preview_signin_policy(route, moment)


class PolicyConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="policy-config-")
        self.settings = ConfigSettings(Path(self.temp.name) / "settings.json", {"servers": [{"id": "paper"}]})
        self.route = {"umo": "bot:GroupMessage:1", "server_id": "paper"}
        self.settings.save({"routes": [self.route]})

    def tearDown(self):
        self.temp.cleanup()

    def test_additive_defaults_and_decimal_canonicalization(self):
        normalized = self.settings.route(self.route["umo"])
        self.assertEqual(normalized["signin_time"], {"enabled": False, "start": "00:00", "end": "23:59"})
        self.assertEqual(normalized["reward_events"], [])
        self.settings.save({"routes": [{**self.route, "reward_events": [event(multiplier=2.0)]}]})
        self.assertEqual(self.settings.route(self.route["umo"])["reward_events"][0]["multiplier"], "2")
        self.assertEqual(ConfigSettings(self.settings.path).data["schema_version"], 4)

    def test_invalid_policy_rejected_before_saving(self):
        bad_windows = [[], {"enabled": "false"}, {"start": "8:00"}, {"end": "24:00"}, {"start": "12:60"}, {"oops": True}]
        bad_events = [event(start_date="2026-1-01"), event(end_date="2026-02-30"), event(start_date="2027-01-01"),
            event(multiplier="NaN"), event(multiplier="Infinity"), event(multiplier="0.9"), event(multiplier="1001"),
            event(multiplier="1.1234567"), event(multiplier=True), event(priority=True), event(priority=1.5),
            event(mode="stack"), event(points_min=-1), event(points_max=1_000_001), event(enabled=1), event(name="\n"),
            event(id="bad id"), event(extra="unknown")]
        changes = [{"signin_time": value} for value in bad_windows] + [{"reward_events": [value]} for value in bad_events]
        changes += [{"reward_events": [event(), event()]}, {"reward_events": {}}, {"reward_events": [event(id=str(i)) for i in range(201)]}]
        original = self.settings.path.read_bytes()
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.settings.save({"routes": [{**self.route, **change}]})
        self.assertEqual(self.settings.path.read_bytes(), original)

    def test_overlapping_same_priority_rejected_at_shared_endpoint(self):
        one = event()
        two = event(id="other", start_date="2026-10-31", end_date="2026-11-01")
        with self.assertRaisesRegex(ValueError, "优先级相同"):
            normalize_signin_policy({"reward_events": [one, two]})
        normalize_signin_policy({"reward_events": [one, {**two, "priority": 1}]})
        normalize_signin_policy({"reward_events": [one, {**two, "enabled": False}]})
        normalize_signin_policy({"reward_events": [one, {**two, "start_date": "2026-11-01"}]})

    def test_shared_scope_is_consistent_but_list_order_is_irrelevant(self):
        one = {**self.route, "scope": "shared", "reward_events": [event(), event(id="later", start_date="2026-11-01", end_date="2026-11-02")]}
        two = {**copy.deepcopy(one), "umo": "bot:GroupMessage:2"}
        two["reward_events"].reverse()
        self.settings.save({"routes": [one, two]})
        for change in ({"signin_time": {"enabled": True, "start": "08:00", "end": "09:00"}}, {"reward_events": []}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "共用点数作用域"):
                self.settings.save({"routes": [one, {**two, **change}]})
        self.settings.save({"routes": [one, {**two, "scope": "independent", "reward_events": []}]})


class PolicySigninTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="policy-signin-")
        self.store = Store(Path(self.temp.name) / "domain.sqlite3")
        self.settings, self.gateway = Settings(), Gateway()
        self.route = self.settings.route_data
        self.route.update(timezone="Asia/Shanghai", points_min=10, points_max=20,
                          signin_time={"enabled": True, "start": "08:00", "end": "09:00"}, reward_events=[event(multiplier="1.5")])
        self.domain = Domain(self.store, self.settings, self.gateway)
        self.ctx = {"umo": self.route["umo"], "scope": "group1", "server_id": "paper", "user_id": "1", "message_id": "one"}
        self.store.bind_name("1", "rincho", "paper", 0)
        self.clock = patch.object(self.domain, "_now", return_value=datetime(2026, 10, 31, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))).start()

    async def asyncTearDown(self):
        patch.stopall()
        await self.domain.close()
        self.store.close()
        self.temp.cleanup()

    def at(self, text):
        self.clock.return_value = datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("Asia/Shanghai"))

    def operation_count(self):
        with self.store.connection() as db:
            return db.execute("SELECT COUNT(*) FROM signin_operations").fetchone()[0]

    async def test_outside_window_never_sends_or_creates_an_operation(self):
        self.at("2026-10-31T09:01")
        with patch("services.domain.secrets.randbelow", side_effect=AssertionError("must not draw")):
            result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["status"], "time_closed")
        self.assertIn("08:00–09:00", result["text"])
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.operation_count(), 0)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)

    async def test_fresh_attempt_after_block_can_sign_and_persists_draw(self):
        self.at("2026-10-31T07:59")
        await self.domain.execute("signin", self.ctx)
        self.at("2026-10-31T08:00")
        with patch("services.domain.secrets.randbelow", return_value=1) as rng:
            result = await self.domain.execute("signin", self.ctx)
        rng.assert_called_once_with(11)
        self.assertEqual(result["data"]["points_delta"], 16)
        policy = result["data"]["reward_policy"]
        self.assertEqual((policy["base_points"], policy["final_points"], policy["event"]["id"]), (11, 16, "festival"))
        self.assertEqual(self.store.signin_operation(self.domain._key("signin", self.ctx))["result"]["reward_policy"], policy)

    async def test_completed_replay_outside_window_ignores_new_rewards_and_missing_binding(self):
        with patch("services.domain.secrets.randbelow", return_value=0):
            original = await self.domain.execute("signin", self.ctx)
        self.at("2026-11-01T12:00")
        self.route["reward_events"] = []
        self.store.unbind("1", 0)
        with patch("services.domain.secrets.randbelow", side_effect=AssertionError("must not redraw")):
            replay = await self.domain.execute("signin", self.ctx)
        self.assertEqual(replay, original)
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_pending_retry_keeps_original_reward_and_snapshot(self):
        key = self.domain._key("signin", self.ctx)
        original = self.domain.preview_signin_policy(self.route, "2026-10-31T08:00", 11)
        self.store.begin_signin(key, "group1", "1", "2026-10-31", 16, self.store.binding("1"), self.ctx["umo"], reward_policy=original)
        self.route["reward_events"] = [event(mode="range", points_min=99, points_max=99)]
        with patch("services.domain.secrets.randbelow", side_effect=AssertionError("must not redraw")):
            result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["data"]["points_delta"], 16)
        self.assertEqual(result["data"]["reward_policy"], original)

    async def test_confirmed_recovery_outside_window_keeps_reward_without_rcon(self):
        with patch("services.domain.secrets.randbelow", return_value=1), patch.object(self.store, "credit_signin", side_effect=sqlite3.OperationalError("synthetic")):
            result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["status"], "unknown")
        self.at("2026-11-01T12:00")
        self.route["reward_events"] = []
        await self.domain.recover()
        replay = await self.domain.execute("signin", self.ctx)
        self.assertEqual(replay["data"]["points_delta"], 16)
        self.assertEqual(replay["data"]["reward_policy"]["event"]["id"], "festival")
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_overnight_window_uses_actual_calendar_day_and_does_not_leak_events(self):
        self.route["signin_time"].update(start="22:00", end="02:00")
        self.at("2026-10-31T23:59")
        with patch("services.domain.secrets.randbelow", return_value=0):
            first = await self.domain.execute("signin", self.ctx)
            self.at("2026-11-01T00:00")
            second = await self.domain.execute("signin", {**self.ctx, "message_id": "next-day"})
        self.assertEqual((first["data"]["points_delta"], second["data"]["points_delta"]), (15, 10))
        self.assertEqual((first["data"]["day"], second["data"]["day"]), ("2026-10-31", "2026-11-01"))
        self.assertIsNone(second["data"]["reward_policy"]["event"])

    async def test_unknown_game_keeps_policy_but_does_not_credit(self):
        self.gateway.replies = ["unknown"]
        result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["status"], "unknown")
        op = self.store.signin_operation(self.domain._key("signin", self.ctx))
        self.assertIn("reward_policy", op["result"])
        self.assertEqual(self.store.balance("group1", "1")["total"], 0)
        self.at("2026-10-31T12:00")
        self.assertEqual(await self.domain.execute("signin", self.ctx), result)
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_balance_limit_rolls_back_local_credit_and_keeps_confirmation(self):
        self.store.adjust_points("group1", "1", "set", MAX_POINTS - 1, "test", "opening")
        result = await self.domain.execute("signin", self.ctx)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.store.balance("group1", "1")["total"], MAX_POINTS - 1)
        self.assertEqual(self.store.signin_operation(self.domain._key("signin", self.ctx))["state"], "game_confirmed")

    async def test_separate_scope_uses_its_own_event_policy(self):
        other = {**copy.deepcopy(self.route), "scope": "other", "umo": "bot:GroupMessage:2", "reward_events": []}
        original_route = self.settings.route
        self.settings.route = lambda umo: other if umo == other["umo"] else original_route(umo)
        with patch("services.domain.secrets.randbelow", return_value=0):
            one = await self.domain.execute("signin", self.ctx)
            two = await self.domain.execute("signin", {**self.ctx, "umo": other["umo"], "scope": "other", "message_id": "other"})
        self.assertEqual((one["data"]["points_delta"], two["data"]["points_delta"]), (15, 10))
        self.assertEqual(self.store.balance("other", "1")["total"], 10)


class PolicyApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(__file__).resolve().parents[1]
        package_name = "mcqq_policy_application_tests"
        if package_name not in sys.modules:
            package = types.ModuleType(package_name)
            package.__path__ = [str(root)]
            sys.modules[package_name] = package
        self.module = importlib.import_module(package_name + ".services.application")
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="policy-api-")
        self.umo = "bot:GroupMessage:1"
        self.app = self.module.Application(self.temp.name, {
            "servers": [{"id": "paper"}], "monitor": {"enabled": False},
            "routes": [{"umo": self.umo, "server_id": "paper", "points_min": 3, "points_max": 100,
                "signin_time": {"enabled": True, "start": "08:00", "end": "09:00"}, "reward_events": [event(multiplier="1.13")]}]})
        self.app.gateway.rcon = AsyncMock(side_effect=AssertionError("preview cannot send"))

    async def asyncTearDown(self):
        await self.app.close()
        self.temp.cleanup()

    async def test_saved_route_preview_does_not_access_store_or_change_config(self):
        before = copy.deepcopy(self.app.settings.data)
        with patch.object(self.app.store, "connection", side_effect=AssertionError("preview cannot read/write accounts")), \
             patch.object(self.app, "_audit", side_effect=AssertionError("preview cannot write an audit record")):
            result = await self.app.web("preview_signin_policy", {"route_umo": self.umo, "at": "2026-10-31T08:00", "base_points": 100}, "dashboard:test")
        self.assertTrue(result["allowed"])
        self.assertEqual((result["final_points"], result["event"]["id"]), (113, "festival"))
        self.assertEqual(self.app.settings.data, before)
        self.assertEqual(self.app._ops.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)
        self.app.gateway.rcon.assert_not_awaited()

    async def test_rejects_unsaved_routes_and_bad_input(self):
        invalid = [{}, {"route_umo": ""}, {"route_umo": "bot:GroupMessage:missing"}, {"route_umo": []},
                   {"route_umo": self.umo, "at": 123}, {"route_umo": self.umo, "at": "invalid"},
                   {"route_umo": self.umo, "base_points": True}, {"route_umo": self.umo, "base_points": 101},
                   {"route_umo": self.umo, "base_points": None}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await self.app.web("preview_signin_policy", payload, "dashboard:test")
        self.app.gateway.rcon.assert_not_awaited()

    async def test_optional_time_uses_current_route_clock_and_never_draws(self):
        fixed = datetime(2026, 10, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch.object(self.app.domain, "_now", return_value=fixed), \
             patch(self.module.Domain.__module__ + ".secrets.randbelow", side_effect=AssertionError("preview cannot draw")):
            result = await self.app.web("preview_signin_policy", {"route_umo": self.umo}, "dashboard:test")
        self.assertFalse(result["allowed"])
        self.assertIsNone(result["base_points"])
        self.assertIsNone(result["final_points"])
        self.app.gateway.rcon.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
