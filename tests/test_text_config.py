import json
import os
import tempfile
import unittest
from pathlib import Path

from core.config import Settings
from core.text import clean_text, parse_rules, parse_signin, render_command, render_template


class TextTests(unittest.TestCase):
    def test_real_litesignin_samples(self):
        ok = 'Rcon:§f[§3手记人偶§f] §a成功给玩家 §6rincho §a签到， 当前该玩家已连续签到 §e1 §a天。'
        repeated = 'Rcon:§f[§3手记人偶§f] §c玩家 §6rincho §c今天已经签到了，请不要重复签到。'
        self.assertEqual(parse_signin(ok, 'rincho')['status'], 'SIGNED')
        self.assertEqual(parse_signin(ok, 'rincho')['fields']['days'], '1')
        self.assertEqual(parse_signin(repeated, 'rincho')['status'], 'ALREADY_SIGNED')
        self.assertNotIn('days', parse_signin(repeated, 'rincho')['fields'])
        self.assertEqual(parse_signin(ok, 'not_rincho')['status'], 'UNKNOWN')
        self.assertEqual(parse_signin(ok+'\n'+repeated, 'rincho')['status'], 'UNKNOWN')

    def test_normalization_and_regex_deadline(self):
        self.assertEqual(clean_text('\x1b[32m§x§f§f§0§0§a§aHello§r\x1b[0m'), 'Hello')
        self.assertEqual(parse_rules('a'*4096+'!', [{'status':'SUCCESS','pattern':'(a+)+$'}])['status'], 'UNKNOWN')
        self.assertEqual(parse_rules('ok', [{'status':'SUCCESS','pattern':'ok'}, {'status':'FAILED','pattern':'ok'}])['status'], 'UNKNOWN')
        self.assertEqual(parse_rules('anything', [{'status':'SUCCESS','pattern':'('}])['status'], 'UNKNOWN')

    def test_templates_cannot_access_objects_or_inject_commands(self):
        for template in ('{x.__class__}', '{x[0]}', '{x!r}', '{x:08d}'):
            with self.assertRaises(ValueError): render_template(template, {'x': 'value'})
        self.assertEqual(render_template('{{原文}} {player} {missing}', {'player':'rincho'}), '{原文} rincho ')
        with self.assertRaises(ValueError): render_command('give {player} stone', {'player':'rincho\nop other'})
        with self.assertRaises(ValueError): render_command('give {player} stone', {})
        self.assertEqual(render_command('/signin click {player}', {'player':'rincho'}), 'signin click rincho')


class ConfigTests(unittest.TestCase):
    def setUp(self):
        base=Path('work/config-tests');base.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base)
        self.settings=Settings(Path(self.tmp.name)/'settings.json')

    def tearDown(self): self.tmp.cleanup()

    def test_atomic_rejection_and_masked_secret_preservation(self):
        self.settings.save({'servers':[{'id':'survival','name':'Survival','token':'never-public'}]})
        public=self.settings.public()
        self.assertNotIn('never-public', json.dumps(public))
        self.settings.save(public)
        self.assertEqual(self.settings.server('survival')['token'], 'never-public')
        before=self.settings.path.read_bytes()
        with self.assertRaises(ValueError): self.settings.save({'routes':[{'umo':'123','server_id':'survival'}]})
        self.assertEqual(self.settings.path.read_bytes(),before)

    def test_duplicate_route_and_secret_url_rejected(self):
        with self.assertRaises(ValueError): self.settings.save({'servers':[{'id':'s','url':'ws://user:secret@example.com'}]})
        self.settings.save({'servers':[{'id':'s'}]})
        r={'umo':'bot:GroupMessage:1','server_id':'s'}
        with self.assertRaises(ValueError): self.settings.save({'routes':[r,r]})
        with self.assertRaises(ValueError): self.settings.save({'commands':[{'id':'a','name':'x','rcon':'list','rules':[{'status':'OK','pattern':'('}]}]})

    def test_invalid_amount_timezone_and_template_rejected_before_save(self):
        self.settings.save({'servers':[{'id':'s'}]})
        invalid = [
            {'routes':[{'umo':'b:GroupMessage:1','server_id':'s','points_min':1.5}]},
            {'timezone':'Not/A_Timezone'}, {'enabled':'false'}, {'commands':{}},
            {'commands':[{'id':'a','name':'x','rcon':'give {missing} stone'}]},
            {'commands':[{'id':'a','name':'x','rcon':'give {n} stone','parameters':[{'name':'n','type':'enum'}]}]},
            {'routes':[{'umo':'b:GroupMessage:1','server_id':'s','signin_rules':[{'status':'SIGNED','pattern':'ok'}]}]},
        ]
        before = self.settings.path.read_bytes()
        for patch in invalid:
            with self.subTest(patch=patch), self.assertRaises(ValueError): self.settings.save(patch)
        self.assertEqual(self.settings.path.read_bytes(),before)

    def test_shared_scope_must_keep_calendar_and_rewards_consistent(self):
        self.settings.save({'servers':[{'id':'s'}]})
        one = {'umo':'b:GroupMessage:1','scope':'shared','server_id':'s'}
        two = {'umo':'b:GroupMessage:2','scope':'shared','server_id':'s'}
        self.settings.save({'routes':[one,two]})
        for change in ({'timezone':'UTC'}, {'points_max':20}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.settings.save({'routes':[one,{**two,**change}]})


if __name__ == '__main__': unittest.main()
