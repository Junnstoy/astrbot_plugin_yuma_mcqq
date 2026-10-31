"""Offline regression checks for plain performance command defaults."""
from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, call


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "yuma_monitor_defaults"
if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
monitor_module = importlib.import_module(PACKAGE + ".services.monitor")


# Paper's built-in tps output, including legacy formatting from RCON bridges.
PAPER_TPS = "§6TPS from last 1m, 5m, 15m: §a20.0§r, §e17.6§r, §c14.2"
PAPER_MSPT = "Server tick times (avg/min/max) from last 5s, 10s, 1m:\n10.0/2.0/30.0, 12.0/3.0/40.0, 15.0/4.0/50.0"


class PlainMetricParserTests(unittest.TestCase):
    def test_paper_tps_selects_first_actual_value_and_preserves_windows(self):
        for raw in (PAPER_TPS, PAPER_TPS.replace("20.0", "*20.0")):
            with self.subTest(raw=raw):
                self.assertEqual(monitor_module.parse_tps(raw), {
                    "value": 20.0, "window": "1m",
                    "windows": {"1m": 20.0, "5m": 17.6, "15m": 14.2},
                })

    def test_numeric_ping_replies_with_milliseconds(self):
        # These are supported numeric reply shapes, not a claim that every
        # plugin providing /ping uses the same output or accepts console use.
        for raw in ("§aPing: §e42 ms", "Your ping is 42ms", "Steve has 42 ms ping.", "延迟：42 ms"):
            with self.subTest(raw=raw):
                self.assertEqual(monitor_module.parse_ping(raw), {
                    "value": 42.0, "statistic": "reported_player_ping",
                })

    def test_non_numeric_ping_is_missing_and_never_zero(self):
        for raw in ("Pong!", "§6Pong!", "Unknown command", "Usage: /ping [player]", "Only players may use this command."):
            with self.subTest(raw=raw):
                self.assertIsNone(monitor_module.parse_ping(raw)["value"])
        self.assertEqual(monitor_module.parse_ping("No players online."), {
            "value": None, "statistic": "no_players",
        })


class PlainCommandSamplingTests(unittest.IsolatedAsyncioTestCase):
    async def sample(self, server, replies):
        settings = types.SimpleNamespace(
            data={"servers": [server], "monitor": {"enabled": False, "retention_days": 7}},
            server=lambda server_id: server,
        )
        gateway = types.SimpleNamespace(rcon=AsyncMock(side_effect=replies))
        with tempfile.TemporaryDirectory() as directory:
            monitor = monitor_module.Monitor(None, settings, gateway, directory)
            try:
                result = await monitor.sample("one")
                saved = monitor.history("one")
            finally:
                await monitor.close()
        return gateway, result, saved

    async def test_default_commands_and_non_numeric_ping_persist_as_missing(self):
        gateway, result, saved = await self.sample(
            {"id": "one"}, [PAPER_TPS, PAPER_MSPT, "Pong!"],
        )
        self.assertEqual(gateway.rcon.await_args_list, [
            call("one", "tps"), call("one", "mspt"), call("one", "ping"),
        ])
        self.assertEqual((result["tps"], result["mspt"]), (20.0, 10.0))
        self.assertIsNone(result["player_ping_ms"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"], ["player_ping_ms:unavailable"])
        self.assertIsNone(saved[0]["player_ping_ms"])

    async def test_explicit_command_overrides_remain_unchanged(self):
        gateway, result, _ = await self.sample({
            "id": "one", "tps_command": "paper:tps", "mspt_command": "paper:mspt",
            "ping_command": "ping Steve",
        }, [PAPER_TPS, PAPER_MSPT, "Steve has 42 ms ping."])
        self.assertEqual(gateway.rcon.await_args_list, [
            call("one", "paper:tps"), call("one", "paper:mspt"), call("one", "ping Steve"),
        ])
        self.assertEqual(result["player_ping_ms"], 42.0)
        self.assertEqual(result["status"], "ok")
