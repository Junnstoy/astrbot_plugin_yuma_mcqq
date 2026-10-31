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
  if(body.action==='preview')return {status:'SIGNED',fields:{player:'rincho',days:'1'},cleaned:'测试回包',preview:'游戏签到成功'};
  if(body.action==='import_preview')return {digest:'synthetic-digest',balances:1};
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
        await page.get_by_role('button', name='签到回包解析示例').click()
        await page.get_by_role('button', name='只模拟回复', exact=True).click()
        await page.wait_for_selector('#output[open]')
        assert 'SIGNED' in await page.locator('#output-text').inner_text()
        await page.locator('#output-close').click()
        await page.locator('nav button[data-page="commands"]').click()
        await page.get_by_role('button', name='新增命令', exact=True).click()
        await page.get_by_label('初始触发名', exact=True).fill('测试查询')
        await page.get_by_label('RCON 命令模板', exact=True).fill('list')
        await page.get_by_role('button', name='保存', exact=True).click()
        await page.wait_for_selector('#editor:not([open])', state='attached')
        assert await page.get_by_text('测试查询', exact=True).count() >= 1
        await page.locator('nav button[data-page="routes"]').click()
        await page.get_by_role('button',name='编辑与开关',exact=True).click()
        await page.screenshot(path=str(screenshots/'route-editor.png'),full_page=True)
        await page.get_by_label('玩家死亡',exact=True).uncheck()
        await page.get_by_label('QQ 表情',exact=True).check()
        await page.get_by_label('QQ群显示名称',exact=True).fill('测试交流群')
        await page.get_by_role('button',name='保存',exact=True).click()
        await page.wait_for_selector('#editor:not([open])',state='attached')
        route_value=await page.evaluate('window.fixture.settings.routes[0]')
        assert 'PlayerDeathEvent' not in route_value['events'] and 'face' in route_value['types']
        await page.get_by_role('button',name='编辑',exact=True).first.click()
        await page.get_by_label('服务器显示名称',exact=True).fill('生存测试服')
        await page.get_by_label('连接方式',exact=True).select_option('reverse')
        assert await page.get_by_label('反向监听端口',exact=True).is_visible()
        assert not await page.get_by_label('QueQiao WebSocket 地址',exact=True).is_visible()
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
        await page.locator('nav button[data-page="shop"]').click()
        await page.locator('footer').evaluate("el=>el.textContent='本地 GUI 验证 · 仅合成数据'")
        await page.screenshot(path=str(screenshots/'shop.png'), full_page=True)
        await page.set_viewport_size({'width':390,'height':844})
        await page.locator('nav button[data-page="overview"]').click()
        await page.screenshot(path=str(screenshots/'mobile.png'), full_page=True)
        assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        assert not errors, errors
        print(json.dumps({'pages':7,'forms':['command_save','parser_preview','route_event_switches','reverse_connection','binding_prefix_currency'],'responsive':True,'console_errors':errors},ensure_ascii=False))
        await browser.close()


if __name__ == '__main__': asyncio.run(main())
