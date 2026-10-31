"""Raw administrator commands: unchanged console text and durable at-most-once sends."""
import asyncio
import hashlib
import importlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "mcqq_raw_rcon_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
module = importlib.import_module(PACKAGE + ".services.application")
Application = module.Application


class RawRconTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / "work").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "work", prefix="raw-rcon-")
        self.bootstrap = {
            "servers": [{"id": "paper"}, {"id": "other"}],
            "routes": [{"umo": "bot:GroupMessage:1", "server_id": "paper"}],
            "commands": [{"id": "rcon", "name": "旧自定义命令", "rcon": "list",
                          "require_binding": False, "parser": "text", "cooldown_seconds": 0}],
        }
        self.app = Application(self.temp.name, self.bootstrap)
        self.ctx = {"umo": "bot:GroupMessage:1", "user_id": "1", "is_admin": True, "message_id": "one"}
        self.app.gateway.rcon = AsyncMock(return_value="§aDone")

    async def asyncTearDown(self):
        await self.app.close()
        self.temp.cleanup()

    def rows(self):
        return self.app._ops.execute("SELECT * FROM runs ORDER BY created_at,id").fetchall()

    async def reopen(self):
        await self.app.close()
        self.app = Application(self.temp.name, self.bootstrap)
        self.app.gateway.rcon = AsyncMock(return_value="must not send")

    async def test_permission_route_and_stable_id_are_required_before_recording(self):
        cases = [dict(self.ctx, is_admin=False), dict(self.ctx, umo="unknown:GroupMessage:1")]
        cases.extend(dict(self.ctx, message_id=value) for value in (None, "", 0, "0", "  "))
        for ctx in cases:
            with self.subTest(ctx=ctx):
                result = await self.app.execute("rcon", ctx, "give Player diamond 1")
                self.assertIn(result["status"], ("DENIED", "DISABLED"))
        self.app.settings.data["routes"][0]["enabled"] = False
        self.assertEqual((await self.app.execute("rcon", self.ctx, "say hello"))["status"], "DISABLED")
        self.app.gateway.rcon.assert_not_awaited()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.app._ops.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)

    async def test_json_braces_quotes_escapes_and_target_route_are_preserved(self):
        command = r'tellraw @a {"text":"{player}", "extra":[{"text":"SECRET_RAW_TOKEN\n a b"}]}'
        result = await self.app.execute("rcon", dict(self.ctx, server_id="other"), "  /" + command + "  ")
        self.app.gateway.rcon.assert_awaited_once_with("paper", command)
        self.assertEqual(result, {"status": "RECEIVED", "text": "Done"})
        audit = self.app._ops.execute("SELECT actor,action,detail FROM audit").fetchone()
        self.assertEqual((audit["actor"], audit["action"]), ("1", "rcon"))
        detail = json.loads(audit["detail"])
        self.assertEqual(set(detail), {"server_id", "run_id"})
        self.assertEqual(detail["server_id"], "paper")
        self.assertNotIn("SECRET_RAW_TOKEN", json.dumps(self.app.snapshot()))

    async def test_invalid_commands_fail_before_any_run_or_gateway_call(self):
        invalid = ["", " /// ", "say one\nsay two", "say one\rsay two", "say\x00x", "x" * 1447,
                   "say " + "中" * 481, "say \ud800"]
        for command in invalid:
            with self.subTest(command=repr(command[:30])):
                with self.assertRaises(ValueError):
                    await self.app.execute("rcon", self.ctx, command)
        self.app.gateway.rcon.assert_not_awaited()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.app._ops.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)

    async def test_utf8_1446_byte_boundary_is_allowed(self):
        command = "say " + "中" * 480 + "xx"
        self.assertEqual(len(command.encode("utf-8")), 1446)
        await self.app.execute("rcon", self.ctx, command)
        self.app.gateway.rcon.assert_awaited_once_with("paper", command)

    async def test_sending_is_committed_before_network_and_concurrent_request_sends_once(self):
        async def remote(server, command):
            with closing(sqlite3.connect(Path(self.temp.name) / "operations.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "SENDING")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 1)
            await asyncio.sleep(0.01)
            return "§aGranted"
        self.app.gateway.rcon = AsyncMock(side_effect=remote)
        results = await asyncio.gather(*(self.app.execute("rcon", self.ctx, "give Player diamond 1") for _ in range(6)))
        self.assertTrue(all(result == results[0] for result in results))
        self.app.gateway.rcon.assert_awaited_once()
        self.assertEqual(self.rows()[0]["status"], "RECEIVED")

    async def test_retry_after_route_or_text_change_returns_previous_result(self):
        first = await self.app.execute("rcon", self.ctx, "give Player diamond 1")
        self.app.settings.data["routes"][0]["server_id"] = "other"
        duplicate = await self.app.execute("rcon", self.ctx, "give Player diamond 99")
        self.assertEqual(duplicate, first)
        self.app.gateway.rcon.assert_awaited_once_with("paper", "give Player diamond 1")
        await self.app.execute("rcon", dict(self.ctx, message_id="two"), "list")
        self.app.gateway.rcon.assert_awaited_with("other", "list")

    async def test_request_id_can_provide_stable_identity(self):
        ctx = dict(self.ctx, message_id="", request_id="durable-request")
        first = await self.app.execute("rcon", ctx, "list")
        second = await self.app.execute("rcon", dict(ctx, message_id="changed"), "list")
        self.assertEqual(first, second)
        self.app.gateway.rcon.assert_awaited_once()

    async def test_gateway_errors_and_post_send_cleaning_errors_never_replay(self):
        cases = [(module.GatewayUnavailable("offline"), "FAILED"),
                 (module.OutcomeUnknown("timeout"), "UNKNOWN"),
                 (module.GatewayError("unclassified interface error"), "UNKNOWN")]
        for index, (error, expected) in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                ctx = dict(self.ctx, message_id=f"error-{index}")
                remote = AsyncMock(side_effect=error)
                self.app.gateway.rcon = remote
                first = await self.app.execute("rcon", ctx, "give Player diamond 1")
                self.assertEqual(first["status"], expected)
                self.assertEqual(await self.app.execute("rcon", ctx, "give Player diamond 1"), first)
                remote.assert_awaited_once()
        self.app.gateway.rcon = AsyncMock(return_value="x" * 65537)
        ctx = dict(self.ctx, message_id="bad-clean")
        with self.assertLogs("astrbot_plugin_yuma_mcqq", level="ERROR"):
            first = await self.app.execute("rcon", ctx, "give Player diamond 1")
        self.assertEqual(first["status"], "UNKNOWN")
        self.assertEqual(await self.app.execute("rcon", ctx, "give Player diamond 1"), first)
        self.app.gateway.rcon.assert_awaited_once()

    async def test_completed_result_survives_restart_without_sending(self):
        first = await self.app.execute("rcon", self.ctx, "give Player diamond 1")
        await self.reopen()
        self.assertEqual(await self.app.execute("rcon", self.ctx, "give Player diamond 1"), first)
        self.app.gateway.rcon.assert_not_awaited()

    async def test_cancelled_operation_is_unknown_before_database_close_and_after_restart(self):
        started = asyncio.Event()
        async def remote(*args):
            started.set()
            await asyncio.Event().wait()
        self.app.gateway.rcon = AsyncMock(side_effect=remote)
        task = asyncio.create_task(self.app.execute("rcon", self.ctx, "give Player diamond 1"))
        await started.wait()
        await self.app.close()
        self.assertTrue(task.cancelled())
        with closing(sqlite3.connect(Path(self.temp.name) / "operations.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "UNKNOWN")
        await self.reopen()
        result = await self.app.execute("rcon", self.ctx, "give Player diamond 1")
        self.assertEqual(result["status"], "UNKNOWN")
        self.app.gateway.rcon.assert_not_awaited()

    async def test_crashed_sending_record_recovers_as_unknown(self):
        run_id = hashlib.sha256(f"{self.ctx['umo']}|one|builtin:rcon".encode()).hexdigest()
        self.app._ops.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                              (run_id, self.ctx["umo"], "builtin:rcon", "paper", "SENDING", "", 1, 1))
        self.app._ops.commit()
        await self.reopen()
        result = await self.app.execute("rcon", self.ctx, "give Player diamond 1")
        self.assertEqual(result["status"], "UNKNOWN")
        self.app.gateway.rcon.assert_not_awaited()

    async def test_raw_namespace_is_distinct_and_legacy_custom_key_is_unchanged(self):
        legacy_id = hashlib.sha256(f"{self.ctx['umo']}|one|rcon".encode()).hexdigest()
        self.app._ops.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                              (legacy_id, self.ctx["umo"], "rcon", "paper", "SUCCESS", "old cached reply", 1, 1))
        self.app._ops.commit()
        cached = await self.app.execute("custom:rcon", self.ctx)
        self.assertEqual(cached, {"status": "SUCCESS", "text": "old cached reply"})
        self.app.gateway.rcon.assert_not_awaited()
        raw = await self.app.execute("rcon", self.ctx, "list")
        self.assertEqual(raw["status"], "RECEIVED")
        self.app.gateway.rcon.assert_awaited_once()
        self.assertEqual(len(self.rows()), 2)

    async def test_empty_return_is_reported_without_claiming_game_success(self):
        self.app.gateway.rcon = AsyncMock(return_value="§a \n ")
        result = await self.app.execute("rcon", self.ctx, "somecommand")
        self.assertEqual(result["status"], "RECEIVED")
        self.assertIn("空内容", result["text"])
        self.assertIn("核对", result["text"])


if __name__ == "__main__":
    unittest.main()
