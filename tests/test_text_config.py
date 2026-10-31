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

    def test_defaults_and_migration_preserve_explicit_face_off(self):
        self.assertFalse(self.settings.data['binding_verification_enabled'])
        self.assertEqual(self.settings.data['command_prefix'],'')
        old = {'servers':[{'id':'s'}], 'routes':[{'umo':'b:GroupMessage:1','server_id':'s','types':['text','image']}]}
        self.settings.path.write_text(json.dumps(old),encoding='utf-8')
        upgraded = Settings(self.settings.path)
        self.assertIn('face',upgraded.data['routes'][0]['types'])
        upgraded.data['routes'][0]['types'].remove('face')
        upgraded.save(upgraded.data)
        self.assertNotIn('face',Settings(self.settings.path).data['routes'][0]['types'])

    def test_reverse_requires_token_and_preserves_display_name(self):
        server={'id':'s','connection_mode':'reverse','url':'','display_name':'生存服'}
        with self.assertRaisesRegex(ValueError,'Token'): self.settings.save({'servers':[server]})
        self.settings.save({'servers':[{**server,'token':'test-only-secret'}]})
        self.assertEqual(self.settings.server('s')['display_name'],'生存服')
        self.assertEqual(self.settings.server('s')['reverse_path'],'/queqiao')
        with self.assertRaises(ValueError): self.settings.save({'command_prefix':'mc command'})
        with self.assertRaises(ValueError): self.settings.save({'points_name':'{balance}'})

    def test_performance_defaults_migrate_once_and_keep_custom_commands(self):
        self.settings.save({'servers':[{'id':'new'}]})
        self.assertEqual(self.settings.server('new')['tps_command'], 'tps')
        self.assertEqual(self.settings.server('new')['ping_command'], 'ping')
        old = {'schema_version':2, 'servers':[
            {'id':'old','tps_command':'spark tps','ping_command':'spark ping'},
            {'id':'custom','tps_command':'paper:tps','ping_command':'latency all'},
        ]}
        self.settings.path.write_text(json.dumps(old),encoding='utf-8')
        upgraded = Settings(self.settings.path)
        self.assertEqual(upgraded.data['schema_version'],3)
        self.assertEqual(upgraded.server('old')['tps_command'],'tps')
        self.assertEqual(upgraded.server('old')['ping_command'],'ping')
        self.assertEqual(upgraded.server('custom')['tps_command'],'paper:tps')
        self.assertEqual(upgraded.server('custom')['ping_command'],'latency all')
        upgraded.server('old')['tps_command']='spark tps'
        upgraded.server('old')['ping_command']='spark ping'
        upgraded.save({})
        reloaded = Settings(self.settings.path)
        self.assertEqual(reloaded.server('old')['tps_command'],'spark tps')
        self.assertEqual(reloaded.server('old')['ping_command'],'spark ping')


if __name__ == '__main__': unittest.main()
