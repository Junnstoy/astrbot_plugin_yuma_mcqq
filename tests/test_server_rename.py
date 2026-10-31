"""Server IDs migrate isolated local databases, never actual game delivery."""
import asyncio
import copy
import importlib
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "mcqq_server_rename_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
module = importlib.import_module(PACKAGE + ".services.application")
Application = module.Application


class ServerRenameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / "work").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="rename-", dir=ROOT / "work")
        self.bootstrap = {
            "servers": [{"id": "paper", "name": "auth-name", "token": "synthetic-secret"}, {"id": "other"}],
            "routes": [{"umo": "bot:GroupMessage:1", "server_id": "paper", "scope": "scope"}],
            "commands": [{"id": "list", "name": "旧查询", "rcon": "list", "server_id": "paper",
                          "require_binding": False, "parser": "text", "cooldown_seconds": 0}],
            "monitor": {"enabled": False},
        }
        self.app = Application(self.temp.name, self.bootstrap)
        self.mock_transport()
        self.ctx = {"umo": "bot:GroupMessage:1", "user_id": "1", "is_admin": True, "message_id": "original"}

    def mock_transport(self):
        self.app.gateway.start = AsyncMock()
        self.app.gateway.close = AsyncMock()
        self.app.gateway.rcon = AsyncMock(return_value="Done")

    async def asyncTearDown(self):
        await self.app.close()
        self.temp.cleanup()

    def payload(self, **extra):
        return {"server_id": "paper", "new_server_id": "survival", "revision": self.app.revision(), **extra}

    def seed(self):
        store = self.app.store
        store.bind_name("1", "Player", "paper", 604800)
        binding = store.binding("1")
        store.begin_binding("2", "Second", "paper", self.ctx["umo"])
        store.adjust_points("scope", "1", "add", 100, "setup", "seed-points", "fixture")
        store.begin_signin("signin", "scope", "1", "2026-10-31", 8, binding, self.ctx["umo"])
        store.set_signin_state("signin", "game_confirmed", "SIGNED", fields={"game_continuous_days": "9", "rcon": "paper text"})
        store.save_product("scope", {"id": "gift", "name": "Gift", "kind": "text", "price": 10, "reward": "paper secret"}, "setup")
        order = store.create_order("scope", "1", "gift", "purchase", self.ctx["umo"], binding)
        store.reserve_order(order["id"], "scope", "1")
        with store.connection(True) as db:
            db.execute("INSERT INTO legacy_records VALUES(?,?,?,?)", ("source", "bindings", "1", json.dumps({"server_id": "paper"})))
        now = time.time()
        self.app._ops.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)", ("run", self.ctx["umo"], "builtin:rcon", "paper", "UNKNOWN", "paper output", now, now))
        self.app._ops.commit()
        self.app._audit("setup", "rcon", json.dumps({"server_id": "paper", "run_id": "run"}))
        self.app._audit("setup", "rcon", "server=paper")
        sample = {"server_id": "paper", "timestamp": now, "tps": 20, "mspt": 50, "player_ping_ms": None}
        self.app.monitor._db.execute("INSERT INTO samples(server_id,timestamp,payload) VALUES(?,?,?)", ("paper", now, json.dumps(sample)))
        self.app.monitor._db.commit()
        self.app.monitor._last["paper"] = sample
        return binding, order

    async def test_migrates_config_bindings_pending_work_audit_and_history(self):
        binding, order = self.seed()
        account = self.app.store.balance("scope", "1")
        result = await self.app.web("server_rename", self.payload(), "dashboard:admin")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertFalse(self.app._rename_path.exists())
        self.assertFalse(self.app._maintenance)
        new = self.app.settings.server("survival")
        self.assertEqual((new["name"], new["token"]), ("auth-name", "synthetic-secret"))
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertEqual(self.app.settings.data["routes"][0]["server_id"], "survival")
        self.assertEqual(self.app.settings.data["commands"][0]["server_id"], "survival")
        moved = self.app.store.binding("1")
        self.assertEqual(moved, {**binding, "server_id": "survival"})
        self.assertEqual(self.app.store.balance("scope", "1"), account)
        self.assertEqual(self.app.store.signin_operation("signin")["binding"]["server_id"], "survival")
        stored_order = self.app.store.order(order["id"], True)
        self.assertEqual(stored_order["binding"]["server_id"], "survival")
        self.assertEqual((stored_order["status"], stored_order["reward"]), ("reserved", "paper secret"))
        with self.app.store.connection() as db:
            self.assertEqual(db.execute("SELECT server_id FROM binding_challenges").fetchone()[0], "survival")
            history = json.loads(db.execute("SELECT new_binding FROM binding_history WHERE qq_id='1'").fetchone()[0])
            self.assertEqual(history["server_id"], "survival")
            self.assertEqual(json.loads(db.execute("SELECT record FROM legacy_records").fetchone()[0])["server_id"], "paper")
        self.assertEqual(self.app.store.server_references("paper"), 0)
        self.assertEqual(tuple(self.app._ops.execute("SELECT server_id,status,text FROM runs").fetchone()), ("survival", "UNKNOWN", "paper output"))
        details = [r[0] for r in self.app._ops.execute("SELECT detail FROM audit WHERE action='rcon'")]
        self.assertEqual(json.loads(details[0])["server_id"], "survival")
        self.assertEqual(details[1], "server=survival")
        self.assertEqual(self.app.monitor.history("paper"), [])
        self.assertEqual(self.app.monitor.history("survival")[0]["mspt"], 50)
        self.assertEqual(self.app.monitor.status()["latest"]["survival"]["server_id"], "survival")
        self.app.gateway.rcon.assert_not_awaited()

    async def test_invalid_conflicting_or_stale_request_does_not_pause_or_mutate(self):
        before = copy.deepcopy(self.app.settings.data)
        for change in ({"new_server_id": "bad id"}, {"new_server_id": "other"}, {"new_server_id": "paper"},
                       {"revision": "old"}, {"revision": None}, {"server_id": "missing"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                await self.app.web("server_rename", self.payload(**change), "admin")
            self.assertEqual(self.app.settings.data, before)
            self.assertFalse(self.app._rename_path.exists())
            self.assertFalse(self.app._maintenance)
        self.app.gateway.close.assert_not_awaited()

    async def test_refuses_destination_orphan_history_and_direct_id_replacement(self):
        self.seed()
        now = time.time()
        self.app._ops.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)", ("orphan", "umo", "list", "survival", "SUCCESS", "", now, now))
        self.app._ops.commit()
        with self.assertRaisesRegex(ValueError, "关联历史"):
            await self.app.web("server_rename", self.payload(), "admin")
        changed = copy.deepcopy(self.app.settings.data)
        changed["servers"][0]["id"] = "brand_new"
        changed["routes"][0]["server_id"] = "brand_new"
        changed["commands"][0]["server_id"] = "brand_new"
        with self.assertRaisesRegex(ValueError, "专门"):
            await self.app.web("settings", {"settings": changed}, "admin")
        self.assertEqual(self.app.store.binding("1")["server_id"], "paper")

    async def test_partial_failure_blocks_operations_and_retries_idempotently(self):
        _, order = self.seed()
        payload = self.payload()
        with patch.object(self.app.monitor, "rename_server", side_effect=RuntimeError("synthetic disk failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                await self.app.web("server_rename", payload, "admin")
        self.assertTrue(self.app._maintenance)
        self.assertTrue(self.app._rename_path.exists())
        self.assertNotIn("synthetic-secret", self.app._rename_path.read_text())
        with self.assertRaisesRegex(RuntimeError, "正在迁移"):
            await self.app.execute("rcon", self.ctx, "give Player diamond")
        with self.assertRaisesRegex(ValueError, "已有"):
            await self.app.web("server_rename", {**payload, "new_server_id": "different"}, "admin")
        result = await self.app.web("server_rename", payload, "admin")
        self.assertEqual(result["renamed"]["counts"]["runs"], 1)
        self.assertEqual(self.app.store.order(order["id"])["status"], "reserved")
        with self.app.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='server_rename'").fetchone()[0], 1)
        self.assertEqual(self.app._ops.execute("SELECT COUNT(*) FROM audit WHERE action='server_rename'").fetchone()[0], 1)
        self.app.gateway.rcon.assert_not_awaited()

    async def test_restart_finishes_pending_journal_before_any_recovery_delivery(self):
        self.seed()
        payload = self.payload()
        original_save = self.app.settings.save
        calls = 0
        def fail_final_save(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic write failure")
            return original_save(value)
        with patch.object(self.app.settings, "save", side_effect=fail_final_save):
            with self.assertRaises(OSError):
                await self.app.web("server_rename", payload, "admin")
        await self.app.close()
        self.app = Application(self.temp.name)
        self.mock_transport()
        self.assertEqual(self.app.settings.data["routes"][0]["server_id"], "survival")
        self.assertEqual(self.app.store.binding("1")["server_id"], "survival")
        self.assertFalse(self.app._rename_path.exists())
        self.assertEqual(self.app.store.signin_operation("signin")["state"], "game_confirmed")
        self.app.gateway.rcon.assert_not_awaited()

    async def test_inflight_raw_command_cancelled_unknown_and_replay_never_resends(self):
        started = asyncio.Event()
        async def pending(*args):
            started.set()
            await asyncio.Event().wait()
        self.app.gateway.rcon = AsyncMock(side_effect=pending)
        task = asyncio.create_task(self.app.execute("rcon", self.ctx, "give Player diamond"))
        await started.wait()
        self.app._rename_drain_seconds = 0
        await self.app.web("server_rename", self.payload(), "admin")
        self.assertTrue(task.cancelled())
        row = self.app._ops.execute("SELECT status,server_id FROM runs").fetchone()
        self.assertEqual(tuple(row), ("UNKNOWN", "survival"))
        result = await self.app.execute("rcon", self.ctx, "give Player diamond")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.app.gateway.rcon.await_count, 1)

    async def test_inflight_operation_can_finish_before_migration(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def pending(*args):
            started.set()
            await finish.wait()
            return "Done"
        self.app.gateway.rcon = AsyncMock(side_effect=pending)
        task = asyncio.create_task(self.app.execute("rcon", self.ctx, "list"))
        await started.wait()
        rename = asyncio.create_task(self.app.web("server_rename", self.payload(), "admin"))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "正在迁移"):
            await self.app.execute("status", self.ctx)
        finish.set()
        self.assertEqual((await task)["status"], "RECEIVED")
        await rename
        self.assertEqual(self.app._ops.execute("SELECT server_id FROM runs").fetchone()[0], "survival")

    async def test_pending_game_order_uses_migrated_snapshot_without_changing_identity(self):
        store = self.app.store
        store.bind_name("1", "Player", "paper", 604800)
        binding = store.binding("1")
        store.adjust_points("scope", "1", "add", 100, "setup", "seed")
        store.save_product("scope", {"id": "game", "name": "Game", "kind": "rcon", "price": 10,
            "command": "give {player} diamond", "rules": [{"pattern": "OK", "status": "SUCCESS"}]}, "setup")
        order = store.create_order("scope", "1", "game", "buy", self.ctx["umo"], binding)
        await self.app.web("server_rename", self.payload(), "admin")
        self.app.gateway.rcon.assert_not_awaited()
        self.app.gateway.rcon.return_value = "OK"
        await self.app.execute("confirm", self.ctx, order["id"])
        self.app.gateway.rcon.assert_awaited_once_with("survival", "give Player diamond")
        self.assertEqual(store.order(order["id"])["status"], "fulfilled")
        self.assertEqual(store.binding("1")["version"], binding["version"])
        self.assertEqual(store.balance("scope", "1")["total"], 90)

    async def test_interrupted_signin_remains_unknown_after_migration(self):
        self.app.store.bind_name("1", "Player", "paper", 604800)
        started = asyncio.Event()
        async def pending(*args):
            started.set()
            await asyncio.Event().wait()
        self.app.gateway.rcon = AsyncMock(side_effect=pending)
        task = asyncio.create_task(self.app.execute("signin", self.ctx))
        await started.wait()
        self.app._rename_drain_seconds = 0
        await self.app.web("server_rename", self.payload(), "admin")
        self.assertTrue(task.cancelled())
        with self.app.store.connection() as db:
            row = db.execute("SELECT state,binding_json FROM signin_operations").fetchone()
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(json.loads(row["binding_json"])["server_id"], "survival")
        self.assertEqual(self.app.store.balance("scope", "1")["total"], 0)
        await self.app.execute("signin", self.ctx)
        self.assertEqual(self.app.gateway.rcon.await_count, 1)

    async def test_connection_restart_failure_can_resume_already_saved_config(self):
        self.seed()
        payload = self.payload()
        self.app.gateway.start = AsyncMock(side_effect=OSError("synthetic listener failure"))
        with self.assertRaises(OSError):
            await self.app.web("server_rename", payload, "admin")
        self.assertEqual(self.app.settings.data["routes"][0]["server_id"], "survival")
        self.assertTrue(self.app._maintenance)
        self.app.gateway.start = AsyncMock()
        result = await self.app.web("server_rename", payload, "admin")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(self.app._ops.execute("SELECT COUNT(*) FROM audit WHERE action='server_rename'").fetchone()[0], 1)

    async def test_external_config_change_during_recovery_is_rejected(self):
        self.seed()
        payload = self.payload()
        with patch.object(self.app.monitor, "rename_server", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                await self.app.web("server_rename", payload, "admin")
        self.app.settings.save({"points_name": "externally_changed"})
        with self.assertRaisesRegex(ValueError, "配置被其他方式修改"):
            await self.app.web("server_rename", payload, "admin")
        self.assertTrue(self.app._maintenance)
        self.assertTrue(self.app._rename_path.exists())

    async def test_relay_wrapper_refreshes_stale_route_and_obeys_maintenance(self):
        old_route = self.app.settings.route(self.ctx["umo"])
        await self.app.web("server_rename", self.payload(), "admin")
        self.app.relay.from_qq = AsyncMock()
        event = types.SimpleNamespace(unified_msg_origin=self.ctx["umo"])
        await self.app.relay_from_qq(event, old_route)
        self.assertEqual(self.app.relay.from_qq.await_args.args[1]["server_id"], "survival")
        self.app._maintenance = True
        with self.assertRaises(RuntimeError):
            await self.app.relay_from_qq(event, old_route)

    async def test_pre_rename_context_cannot_write_an_old_id_back_into_bindings(self):
        stale_context = {**self.ctx, "server_id": "paper", "scope": "stale-scope"}
        await self.app.web("server_rename", self.payload(), "admin")
        await self.app.execute("binding", stale_context, "Player")
        self.assertEqual(self.app.store.binding("1")["server_id"], "survival")
        self.assertEqual(self.app.store.server_references("paper"), 0)

    async def test_pre_rename_context_queries_and_inherited_custom_target_use_current_route(self):
        commands = copy.deepcopy(self.app.settings.data["commands"])
        commands[0]["server_id"] = ""
        self.app.settings.save({"commands": commands})
        stale_context = {**self.ctx, "server_id": "paper"}
        await self.app.web("server_rename", self.payload(), "admin")
        await self.app.execute("tps", stale_context)
        self.app.gateway.rcon.assert_awaited_with("survival", "tps")
        await self.app.execute("custom:list", stale_context)
        self.app.gateway.rcon.assert_awaited_with("survival", "list")
        self.assertEqual(self.app._ops.execute("SELECT server_id FROM runs").fetchone()[0], "survival")

    async def test_explicit_custom_and_administrator_performance_targets_are_preserved(self):
        commands = copy.deepcopy(self.app.settings.data["commands"])
        commands[0]["server_id"] = "other"
        self.app.settings.save({"commands": commands})
        stale_context = {**self.ctx, "server_id": "paper"}
        await self.app.web("server_rename", self.payload(), "admin")
        await self.app.execute("custom:list", stale_context)
        self.app.gateway.rcon.assert_awaited_with("other", "list")
        with patch.object(self.app.monitor, "history", return_value=[{}]), patch.object(self.app.monitor, "render", new=AsyncMock(return_value="synthetic.png")):
            await self.app.execute("performance", stale_context, "other")
            self.app.monitor.render.assert_awaited_once_with("other", 24)

    async def test_png_mspt_uses_ticks_but_history_stays_milliseconds(self):
        self.seed()
        figure = Mock()
        axes = [Mock() for _ in range(4)]
        figure.add_subplot.side_effect = axes
        rows = self.app.monitor.history("paper")
        with patch("matplotlib.figure.Figure", return_value=figure), patch("matplotlib.backends.backend_agg.FigureCanvasAgg"):
            self.app.monitor._render_chart("paper", rows, Path(self.temp.name) / "fake.png")
        self.assertEqual(axes[1].plot.call_args.args[1], [1])
        self.assertIn("1 tick = 50 ms", axes[1].set_ylabel.call_args.args[0])
        self.assertEqual(self.app.monitor.history("paper")[0]["mspt"], 50)


if __name__ == "__main__":
    unittest.main()
