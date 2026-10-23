"""Exercise the actual entry's dispatch code with fake MC transport, no AstrBot install."""
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
import importlib.util
import sys

spec = importlib.util.spec_from_file_location('dispatch_core', Path(__file__).resolve().parents[1] / 'core.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

tree = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8'))
cls = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == 'CheckinPointsPlugin')
method = next(x for x in cls.body if isinstance(x, ast.AsyncFunctionDef) and x.name == 'dispatch_mc')
namespace = {'asyncio': asyncio, 'logger': logging.getLogger('dispatch-test'), 'NAME': 'test', 'classify_mc_output': core.classify_mc_output}
exec(compile(ast.Module(body=[method], type_ignores=[]), 'dispatch-under-test', 'exec'), namespace)
dispatch = namespace['dispatch_mc']


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, results):
        self.commands, self.states = [], []
        self.claimed = False
        def claim(_):
            if self.claimed:
                return False
            self.claimed = True
            return True
        async def execute(command):
            self.commands.append(command)
            result = results[len(self.commands) - 1]
            if isinstance(result, BaseException):
                raise result
            return result
        server = SimpleNamespace(execute_command_with_channel=execute)
        store = SimpleNamespace(claim_mc=claim, set_mc_state=lambda *args: self.states.append(args), get_binding=lambda _: 'Steve', get_settings=lambda _: dict(enabled=True, mc_enabled=True))
        store.get_checkin = lambda _: dict(mc_state='sending')
        async def sync():
            return 1
        return SimpleNamespace(config={'enabled': True}, store=store, sync_bindings=sync, mc_plugin=lambda: SimpleNamespace(server_manager=SimpleNamespace(get=lambda _: server)))

    def record(self):
        return dict(id=1, mc_state='pending', commands=['sign Steve', 'reward Steve'], server_name='test', session='bot:GroupMessage:1', user_id='123', player='Steve')

    async def test_empty_response_is_unknown_and_no_replay(self):
        plugin = self.make_plugin([('', 'queqiao'), ('ok', 'direct')])
        self.assertEqual(await dispatch(plugin, self.record()), 'unknown')
        await dispatch(plugin, self.record())
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.states[-1][1], 'unknown')

    async def test_business_success_is_required_for_every_command(self):
        plugin = self.make_plugin([('§a签到成功', 'queqiao'), ('Successfully signed in', 'direct')])
        self.assertEqual(await dispatch(plugin, self.record()), 'sent')
        self.assertNotIn('§', self.states[-1][2])

    async def test_business_failure_stops_group_and_returns_actual_output(self):
        plugin = self.make_plugin([('§c玩家今天已经签到了', 'queqiao'), ('签到成功', 'direct')])
        self.assertEqual(await dispatch(plugin, self.record()), 'failed')
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.states[-1][2], '玩家今天已经签到了')

    async def test_unknown_stops_remaining_commands(self):
        plugin = self.make_plugin([(None, ''), ('must not run', 'direct')])
        self.assertEqual(await dispatch(plugin, self.record()), 'unknown')
        self.assertEqual(self.commands, ['sign Steve'])

    async def test_session_deleted_during_first_command_stops_remaining(self):
        plugin = self.make_plugin([('签到成功', 'queqiao'), ('must not run', 'direct')])
        plugin.store.get_checkin = lambda _: dict(mc_state='skipped' if self.commands else 'sending')
        self.assertEqual(await dispatch(plugin, self.record()), 'skipped')
        self.assertEqual(self.commands, ['sign Steve'])

    async def test_exception_does_not_retry(self):
        plugin = self.make_plugin([TimeoutError()])
        self.assertEqual(await dispatch(plugin, self.record()), 'unknown')
        self.assertEqual(len(self.commands), 1)

    async def test_unloaded_server_is_failed_without_execution(self):
        plugin = self.make_plugin([])
        plugin.mc_plugin = lambda: None
        self.assertEqual(await dispatch(plugin, self.record()), 'failed')
        self.assertEqual(self.commands, [])

    async def test_cancel_records_unknown(self):
        plugin = self.make_plugin([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await dispatch(plugin, self.record())
        self.assertEqual(self.states[-1][1], 'unknown')

    async def test_binding_changed_before_dispatch(self):
        plugin = self.make_plugin([])
        plugin.store.get_binding = lambda _: 'NewPlayer'
        self.assertEqual(await dispatch(plugin, self.record()), 'failed')
        self.assertEqual(self.commands, [])

    async def test_mc_disabled_before_dispatch(self):
        plugin = self.make_plugin([])
        plugin.store.get_settings = lambda _: dict(enabled=True, mc_enabled=False)
        self.assertEqual(await dispatch(plugin, self.record()), 'skipped')
        self.assertEqual(self.commands, [])
