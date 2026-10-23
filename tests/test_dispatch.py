"""Exercise the actual entry's dispatch code with fake MC transport, no AstrBot install."""
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

tree = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8'))
cls = next(x for x in tree.body if isinstance(x, ast.ClassDef))
method = next(x for x in cls.body if isinstance(x, ast.AsyncFunctionDef) and x.name == 'dispatch_mc')
namespace = {'asyncio': asyncio, 'logger': logging.getLogger('dispatch-test'), 'NAME': 'test'}
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
        async def sync():
            return 1
        return SimpleNamespace(store=store, sync_bindings=sync, mc_plugin=lambda: SimpleNamespace(server_manager=SimpleNamespace(get=lambda _: server)))

    def record(self):
        return dict(id=1, mc_state='pending', commands=['sign Steve', 'reward Steve'], server_name='test', session='bot:GroupMessage:1', user_id='123', player='Steve')

    async def test_empty_response_is_transport_success_and_no_replay(self):
        plugin = self.make_plugin([('', 'queqiao'), ('ok', 'direct')])
        self.assertEqual(await dispatch(plugin, self.record()), 'sent')
        await dispatch(plugin, self.record())
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(self.states[-1][1], 'sent')

    async def test_unknown_stops_remaining_commands(self):
        plugin = self.make_plugin([(None, ''), ('must not run', 'direct')])
        self.assertEqual(await dispatch(plugin, self.record()), 'unknown')
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
