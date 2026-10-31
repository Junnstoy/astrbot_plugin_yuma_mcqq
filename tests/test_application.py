"""Composition tests use temporary storage and mocked transport only."""
import asyncio
import copy
import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "mcqq_application_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
module = importlib.import_module(PACKAGE + ".services.application")
Application = module.Application


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / "work").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "work")
        self.app = Application(self.temp.name, {
            "servers": [{"id": "paper"}],
            "routes": [{"umo": "bot:GroupMessage:1", "server_id": "paper"}],
            "commands": [{"id": "list", "name": "测试查询", "rcon": "list",
                          "require_binding": False, "parser": "text"}],
        })
        self.ctx = {"umo": "bot:GroupMessage:1", "user_id": "1", "is_admin": True, "message_id": "message-1"}

    async def asyncTearDown(self):
        await self.app.close()
        self.temp.cleanup()

    async def test_duplicate_message_and_original_umo(self):
        async def reply(*args):
            await asyncio.sleep(0.01)
            return "§aThere are 3 players"
        self.app.gateway.rcon = AsyncMock(side_effect=reply)
        first, second = await asyncio.gather(
            self.app.execute("custom:list", self.ctx), self.app.execute("custom:list", self.ctx))
        self.assertEqual(first, second)
        self.assertEqual(first["text"], "There are 3 players")
        self.assertEqual(self.app.gateway.rcon.await_count, 1)
        row = self.app._ops.execute("SELECT umo,status FROM runs").fetchone()
        self.assertEqual(tuple(row), (self.ctx["umo"], "SUCCESS"))

    async def test_unknown_is_persisted_and_not_replayed(self):
        self.app.gateway.rcon = AsyncMock(side_effect=module.OutcomeUnknown("test"))
        result = await self.app.execute("custom:list", self.ctx)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(await self.app.execute("custom:list", self.ctx), result)
        self.assertEqual(self.app.gateway.rcon.await_count, 1)

    async def test_reload_cancels_inflight_before_database_close(self):
        started = asyncio.Event()
        async def pending(*args):
            started.set()
            await asyncio.Event().wait()
        self.app.gateway.rcon = AsyncMock(side_effect=pending)
        task = asyncio.create_task(self.app.execute("custom:list", self.ctx))
        await started.wait()
        await self.app.close()
        self.assertTrue(task.cancelled())
        db = sqlite3.connect(Path(self.temp.name) / "operations.sqlite3")
        try:
            self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "UNKNOWN")
        finally:
            db.close()
        with self.assertRaises(RuntimeError):
            await self.app.execute("custom:list", self.ctx)

    async def test_configuration_rollback_and_stale_revision(self):
        previous = copy.deepcopy(self.app.settings.data)
        self.app.gateway.reload = AsyncMock()
        self.app.monitor.reload = AsyncMock(side_effect=[RuntimeError("synthetic port failure"), None])
        self.app.media.reload = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            await self.app.web("settings", {"settings": {"enabled": False}}, "tester")
        self.assertEqual(self.app.settings.data, previous)
        self.assertEqual(module.Settings(self.app.settings.path).data, previous)
        with self.assertRaisesRegex(ValueError, "配置已"):
            await self.app.web("settings", {"revision": "outdated", "settings": {}}, "tester")

    async def test_unknown_scope_and_nonadmin_never_send(self):
        self.app.gateway.rcon = AsyncMock()
        result = await self.app.execute("custom:list", {**self.ctx, "umo": "another:GroupMessage:1"})
        self.assertEqual(result["status"], "DISABLED")
        result = await self.app.execute("custom:list", {**self.ctx, "is_admin": False})
        self.assertEqual(result["status"], "DENIED")
        self.app.gateway.rcon.assert_not_awaited()

    async def test_preview_never_sends_command(self):
        self.app.gateway.rcon = AsyncMock()
        result = await self.app.web("preview", {"command": {"parser": "litesignin"}, "player": "rincho",
            "raw": "Rcon:§f[§3手记人偶§f] §c玩家 §6rincho §c今天已经签到了，请不要重复签到。"}, "tester")
        self.assertEqual(result["status"], "ALREADY_SIGNED")
        self.app.gateway.rcon.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
