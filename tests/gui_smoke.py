"""Optional browser acceptance check with synthetic, non-production fixtures.

Run with playwright installed; uses local Edge, no server or production API.
"""
import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = {
    'revision': 'test-only',
    'settings': {
        'enabled': True, 'timezone': 'Asia/Shanghai', 'binding_cooldown_seconds': 604800,
        'servers': [{'id': 'paper-test', 'name': 'Paper 测试服', 'url': 'ws://127.0.0.1:8080', 'token': '********', 'enabled': True}],
        'routes': [{'umo': 'testbot:GroupMessage:10001', 'scope': 'test-group', 'server_id': 'paper-test', 'points_min': 10, 'points_max': 10, 'enabled': True}],
        'commands': [], 'media': {'enabled': False}, 'monitor': {'interval_seconds': 60, 'retention_days': 7},
    },
    'connections': [{'id': 'paper-test', 'name': 'Paper 测试服', 'connected': True}],
    'domain': {
        'bindings': [{'qq_id': '100001', 'player': 'TestPlayer', 'mc_uuid': '00000000-0000-0000-0000-000000000001', 'server_id': 'paper-test', 'updated': 1793390400}],
        'balances': [{'scope': 'test-group', 'qq_id': '100001', 'total': 120, 'frozen': 20}],
        'ledger': [], 'signins': [],
        'products': [{'scope': 'test-group', 'id': 'gift', 'name': '测试礼物', 'price': 20, 'kind': 'text', 'stock': -1, 'enabled': True}],
        'orders': [{'scope': 'test-group', 'id': 'test-order', 'qq_id': '100001', 'product_id': 'gift', 'status': 'unknown', 'created': 1793390400}],
    },
    'monitor': {}, 'runs': [], 'audit': [],
    'effective_commands': [{'action_id': 'signin', 'name': '签到', 'aliases': [], 'enabled': True}],
}


async def main():
    async with async_playwright() as pw:
        executable = os.environ.get('MCQQ_BROWSER', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        browser = await pw.chromium.launch(executable_path=executable, headless=True, args=['--disable-gpu'])
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        async def serve(route):
            name = route.request.url.split('/')[-1] or 'index.html'
            path = ROOT / 'views/dashboard' / name
            if name not in {'index.html', 'app.js', 'style.css'}:
                await route.fulfill(status=404, body='Not found'); return
            mime = {'html':'text/html', 'js':'text/javascript', 'css':'text/css'}[name.split('.')[-1]]
            await route.fulfill(status=200, body=path.read_bytes(), content_type=mime)
        await page.route('http://mcqq.test/**', serve)
        script = 'window.fixture = ' + json.dumps(FIXTURE, ensure_ascii=False) + ';' + '''
window.calls=[];
window.AstrBotPluginView={
 ready:async()=>({pluginName:'astrbot_plugin_yuma_mcqq'}),
 apiGet:async()=>structuredClone(window.fixture),
 apiPost:async(path,body)=>{
  window.calls.push({path,body});
  if(path==='settings'){Object.assign(window.fixture.settings,body.settings);return {revision:'test-only'};}
  if(body.action==='metrics')return {samples:[{timestamp:1793390400,tps:20,mspt:8,player_ping_ms:35,slp_rtt_ms:4},{timestamp:1793390460,tps:null,mspt:null,player_ping_ms:null,slp_rtt_ms:null},{timestamp:1793390520,tps:19.8,mspt:12,player_ping_ms:42,slp_rtt_ms:5}]};
  if(body.action==='preview')return {status:'SUCCESS',fields:{},cleaned:body.payload.raw,preview:body.payload.raw};
  if(body.action==='preview_signin'){
    const p=body.payload;const repeated=p.raw.includes('今天已经签到了');
    return {status:repeated?'ALREADY_SIGNED':'SIGNED',fields:{player:p.player,days:'1'},
      data:{group_status:p.group_signed?'ALREADY_SIGNED':'SIGNED'},preview:`玩家: ${p.player}\n群内签到${p.group_signed?'失败❌️ 今日已签到 请不要重复签到':'成功✅️ 本次获得: 1 点数 当前点数:1'}\n游戏签到${repeated?'失败❌️ 今日已签到 请不要重复签到':'成功✅️ 已连续签到 1 天。'}`};
  }
  if(body.action==='import_preview'){
    const all=[{account_key:'old-a:User:100001',qq_id:'100001',player:'TestPlayer'},
      {account_key:'old-b:User:100001',qq_id:'100001',player:'OtherPlayer'}];
    const selected=body.payload.binding_choices??all.map(x=>x.account_key);const blocked=selected.length>1;
    return {digest:'synthetic-digest',plan_digest:'plan-'+selected.length,already_imported:true,counts:{bindings:2},
      binding_plan:{can_apply:!blocked,counts:{source:2,selected:selected.length,ready:blocked?0:selected.length,existing:0,duplicates:0,blocked:blocked?2:0},
        rows:all.map(x=>({...x,server_id:body.payload.binding_server_id,status:selected.includes(x.account_key)?(blocked?'blocked':'ready'):'excluded',reason:blocked?'同一QQ对应不同玩家':''})),
        conflicts:blocked?[{reason:'同一QQ对应不同玩家'}]:[]}};
  }
  if(body.action==='import_apply')return {already_imported:true,bindings_imported:1};
  if(body.action==='binding_unbind'){
    window.fixture.domain.bindings=window.fixture.domain.bindings.filter(x=>x.qq_id!==body.payload.user_id);
    return {status:'success',text:'已解绑 TestPlayer，本次管理员操作不受 CD 限制。'};
  }
  return {status:'success'};
 }
};'''
        await page.add_init_script(script)
        await page.goto('http://mcqq.test/index.html')
        await page.wait_for_function("document.querySelector('#connection').textContent.includes('已连接')")
        await page.locator('footer').evaluate("el=>el.textContent='本地 GUI 验证 · 仅合成数据，不连接真实群或服务器'")
        screenshots = ROOT / 'docs/screenshots'; screenshots.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshots/'dashboard.png'), full_page=True)
        for name in ['routes','commands','bindings','shop','monitor','diagnostics']:
            await page.locator(f'nav button[data-page="{name}"]').click()
            await page.wait_for_timeout(80)
            assert not await page.locator('#banner').is_visible(), name
        for group_signed in (False,True):
            for game_signed in (False,True):
                await page.get_by_role('button', name='签到回包解析示例').click()
                await page.get_by_label('模拟：群内今日已经签到',exact=True).set_checked(group_signed)
                if game_signed:
                    await page.get_by_label('粘贴游戏签到原始回包',exact=True).fill('[手记人偶] 玩家 rincho 今天已经签到了，请不要重复签到。')
                await page.get_by_role('button', name='只模拟回复', exact=True).click()
                await page.wait_for_selector('#output[open]')
                text=await page.locator('#output-text').inner_text()
                assert ('群内签到失败❌️' if group_signed else '群内签到成功✅️') in text
                assert ('游戏签到失败❌️' if game_signed else '游戏签到成功✅️') in text
                await page.locator('#output-close').click()
        await page.locator('nav button[data-page="commands"]').click()
        await page.get_by_role('button', name='新增命令', exact=True).click()
        await page.get_by_label('初始触发名', exact=True).fill('测试查询')
        await page.get_by_label('RCON 命令模板', exact=True).fill('list')
        await page.get_by_role('button', name='保存', exact=True).click()
        await page.wait_for_selector('#editor:not([open])', state='attached')
        assert await page.get_by_text('测试查询', exact=True).count() >= 1
        await page.get_by_role('button',name='模拟回复（不执行）',exact=True).click()
        await page.get_by_label('粘贴服务器原始回复',exact=True).fill('There are 1 players online')
        await page.get_by_role('button',name='只模拟回复',exact=True).click()
        await page.wait_for_selector('#output[open]')
        assert 'There are 1 players online' in await page.locator('#output-text').inner_text()
        await page.locator('#output-close').click()
        await page.locator('nav button[data-page="routes"]').click()
        await page.get_by_role('button',name='编辑与开关',exact=True).click()
        await page.screenshot(path=str(screenshots/'route-editor.png'),full_page=True)
        await page.get_by_label('玩家死亡',exact=True).uncheck()
        await page.get_by_label('QQ 表情',exact=True).check()
        await page.get_by_label('QQ群显示名称',exact=True).fill('测试交流群')
        await page.get_by_label('QQ → MC 显示前缀',exact=True).fill('&e[{group}]&e{sender}{reply}&8:')
        await page.get_by_label('链接标签颜色',exact=True).evaluate("el=>el.value='#ffaa00'")
        await page.get_by_label('图片标签颜色',exact=True).evaluate("el=>el.value='#55ffff'")
        await page.get_by_label('引用悬浮内容（留空用默认）',exact=True).fill('{sender}\n{message}\nDate: {time}')
        await page.get_by_label('引用悬浮内容（留空用默认）',exact=True).scroll_into_view_if_needed()
        await page.screenshot(path=str(screenshots/'route-style-editor.png'),full_page=True)
        await page.get_by_role('button',name='保存',exact=True).click()
        await page.wait_for_selector('#editor:not([open])',state='attached')
        route_value=await page.evaluate('window.fixture.settings.routes[0]')
        assert 'PlayerDeathEvent' not in route_value['events'] and 'face' in route_value['types']
        assert route_value['prefix']=='&e[{group}]&e{sender}{reply}&8:'
        assert route_value['message_colors']['link']=='#ffaa00' and route_value['message_colors']['image']=='#55ffff'
        assert route_value['templates']['reply_hover']=='{sender}\n{message}\nDate: {time}'
        await page.get_by_role('button',name='编辑',exact=True).first.click()
        await page.get_by_label('服务器显示名称',exact=True).fill('生存测试服')
        await page.get_by_label('连接方式',exact=True).select_option('reverse')
        assert await page.get_by_label('反向监听端口',exact=True).is_visible()
        assert not await page.get_by_label('QueQiao WebSocket 地址',exact=True).is_visible()
        assert await page.get_by_label('TPS 指令',exact=True).input_value()=='tps'
        assert await page.get_by_label('玩家延迟指令',exact=True).input_value()=='ping'
        await page.screenshot(path=str(screenshots/'reverse-editor.png'),full_page=True)
        await page.get_by_role('button',name='保存',exact=True).click()
        await page.wait_for_selector('#editor:not([open])',state='attached')
        assert await page.evaluate('window.fixture.settings.servers[0].connection_mode')=='reverse'
        await page.screenshot(path=str(screenshots/'routes.png'),full_page=True)
        await page.get_by_role('button',name='全局设置',exact=True).click()
        assert not await page.get_by_label('绑定时要求游戏验证码',exact=True).is_checked()
        await page.get_by_label('点数显示名称',exact=True).fill('星币')
        await page.get_by_label('插件唤醒前缀（可选）',exact=True).fill('mc')
        await page.get_by_role('button',name='保存',exact=True).click()
        await page.wait_for_selector('#editor:not([open])',state='attached')
        assert await page.evaluate('window.fixture.settings.points_name')=='星币'
        await page.locator('nav button[data-page="bindings"]').click()
        assert await page.locator('#title').inner_text()=='绑定与星币'
        page.once('dialog',lambda dialog:dialog.accept())
        await page.get_by_role('button',name='管理员解绑',exact=True).click()
        await page.wait_for_selector('#output[open]')
        assert '已解绑 TestPlayer' in await page.locator('#output-text').inner_text()
        assert await page.evaluate('window.fixture.domain.bindings.length')==0
        unbind_call=await page.evaluate("window.calls.find(x=>x.body?.action==='binding_unbind')")
        assert unbind_call['body']['payload']=={'user_id':'100001'}
        await page.locator('#output-close').click()
        await page.locator('nav button[data-page="diagnostics"]').click()
        await page.get_by_role('button',name='预览迁移',exact=True).click()
        await page.get_by_label('旧 SQLite 文件绝对路径',exact=True).fill('/offline/synthetic.sqlite')
        await page.get_by_role('button',name='只读预览',exact=True).click()
        await page.wait_for_selector('#output[open]')
        await page.locator('#output-close').click()
        await page.get_by_role('button',name='导入备份',exact=True).click()
        assert '冲突' in await page.locator('#banner').inner_text()
        assert not await page.evaluate("window.calls.some(x=>x.body?.action==='import_apply')")
        await page.get_by_role('button',name='选择绑定 / 重新核对',exact=True).click()
        await page.locator('#fields input[type="checkbox"]').nth(1).uncheck()
        await page.get_by_role('button',name='重新核对选择',exact=True).click()
        await page.wait_for_selector('#output[open]')
        await page.locator('#output-close').click()
        await page.screenshot(path=str(screenshots/'migration-plan.png'),full_page=True)
        await page.get_by_role('button',name='导入备份',exact=True).click()
        await page.get_by_label('我已核对选择并备份当前数据',exact=True).check()
        await page.get_by_role('button',name='确认导入',exact=True).click()
        await page.wait_for_selector('#output[open]')
        applied=await page.evaluate("window.calls.find(x=>x.body?.action==='import_apply').body.payload")
        assert applied['binding_choices']==['old-a:User:100001']
        assert applied['plan_digest']=='plan-1' and applied['binding_server_id']=='paper-test'
        await page.locator('#output-close').click()
        await page.locator('nav button[data-page="shop"]').click()
        await page.locator('footer').evaluate("el=>el.textContent='本地 GUI 验证 · 仅合成数据'")
        await page.screenshot(path=str(screenshots/'shop.png'), full_page=True)
        await page.set_viewport_size({'width':390,'height':844})
        await page.locator('nav button[data-page="overview"]').click()
        await page.screenshot(path=str(screenshots/'mobile.png'), full_page=True)
        assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        assert not errors, errors
        print(json.dumps({'pages':7,'forms':['command_save','parser_preview','signin_combinations','route_event_switches','reply_colors_hover','reverse_connection','binding_prefix_currency','admin_unbind','migration_binding_plan'],'responsive':True,'console_errors':errors},ensure_ascii=False))
        await browser.close()


if __name__ == '__main__': asyncio.run(main())
