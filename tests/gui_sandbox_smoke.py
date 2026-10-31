"""Browser acceptance in AstrBot 4.29.1's actual plugin iframe sandbox.

Run with playwright/Edge. Only synthetic data and intercepted local URLs are
used; no production panel, credentials, QQ account or Minecraft server is used.
"""
import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
# Official 4.29.1 PluginViewPage-DVqXRTsa.js: intentionally no allow-modals.
SANDBOX = "allow-scripts allow-forms allow-downloads"
FIXTURE = {
    "revision": "sandbox-test-only",
    "settings": {
        "enabled": True, "timezone": "Asia/Shanghai", "binding_cooldown_seconds": 604800,
        "servers": [{"id": "paper-test", "name": "Synthetic Paper", "enabled": True}],
        "routes": [{"umo": "synthetic:GroupMessage:20001", "scope": "test-group", "server_id": "paper-test", "enabled": True}],
        "commands": [{"id": "test-command", "name": "测试查询", "rcon": "list", "parser": "text", "enabled": True}],
        "media": {"enabled": False}, "monitor": {"interval_seconds": 60, "retention_days": 7},
    },
    "connections": [{"id": "paper-test", "connected": True}],
    "domain": {
        "bindings": [{"qq_id": "100001", "player": "TestPlayer", "mc_uuid": None, "verified": False, "server_id": "paper-test", "updated": 1793390400}],
        "balances": [], "ledger": [], "signins": [], "products": [], "orders": [],
    },
    "monitor": {}, "runs": [], "audit": [], "effective_commands": [],
}


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=os.environ.get("MCQQ_BROWSER", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            headless=True, args=["--disable-gpu"],
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        page.set_default_timeout(15000)
        errors, blocked_modals, unexpected_dialogs = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: blocked_modals.append(message.text) if "Ignored call to" in message.text else None)

        async def unexpected_dialog(dialog):
            unexpected_dialogs.append(dialog.type)
            await dialog.dismiss()

        page.on("dialog", unexpected_dialog)

        async def serve(route):
            if route.request.url == "http://mcqq-sandbox.test/host.html":
                await route.fulfill(content_type="text/html", body=(
                    '<!doctype html><meta charset="utf-8"><title>Synthetic AstrBot host</title>'
                    f'<iframe src="/plugin/index.html" sandbox="{SANDBOX}" '
                    'style="width:1400px;height:1060px;border:0"></iframe>'
                ))
                return
            name = route.request.url.rsplit("/", 1)[-1]
            if name not in {"index.html", "app.js", "style.css"}:
                await route.fulfill(status=404, body="No external requests in this fixture")
                return
            await route.fulfill(
                body=(ROOT / "views" / "dashboard" / name).read_bytes(),
                content_type={"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name],
                headers={"Access-Control-Allow-Origin": "*"},
            )

        await page.route("**/*", serve)
        await page.add_init_script("window.fixture=" + json.dumps(FIXTURE, ensure_ascii=False) + ";" + """
window.calls=[];window.nativeConfirmCalls=0;window.successfulUnbinds=0;window.failNextUnbind=false;
const realConfirm=window.confirm;
window.confirm=(...args)=>{window.nativeConfirmCalls++;return realConfirm(...args)};
window.AstrBotPluginView={
 ready:async()=>({pluginName:'astrbot_plugin_yuma_mcqq'}),
 apiGet:async()=>structuredClone(window.fixture),
 apiPost:async(path,body)=>{
  window.calls.push({path,body:structuredClone(body)});
  if(path==='settings'){Object.assign(window.fixture.settings,body.settings);return {revision:'next'}};
  if(body.action==='dashboard_page'){
   const p=body.payload;let rows=window.fixture.domain[p.table]||window.fixture[p.table]||[];
   if(p.scope&&['balances','ledger','signins'].includes(p.table))rows=rows.filter(x=>x.scope===p.scope);
   const size=p.page_size||20,pages=Math.max(1,Math.ceil(rows.length/size)),current=Math.max(1,Math.min(p.page||1,pages));
   return {rows:structuredClone(rows.slice((current-1)*size,current*size)),total:rows.length,page:current,page_size:size,pages};
  }
  if(body.action==='binding_unbind'){
   await new Promise(resolve=>setTimeout(resolve,80));
   if(window.failNextUnbind){window.failNextUnbind=false;throw new Error('合成后端错误，请重试')}
   window.fixture.domain.bindings=window.fixture.domain.bindings.filter(x=>x.qq_id!==body.payload.user_id);
   window.successfulUnbinds++;
   return {status:'success',text:'已解绑 TestPlayer；管理员本次不受 CD 限制。'};
  }
  throw new Error('Unhandled synthetic action: '+body.action);
 }
};
""")
        try:
            await page.goto("http://mcqq-sandbox.test/host.html")
            frame = page.frame_locator("iframe")
            await frame.locator("#connection").filter(has_text="已连接").wait_for()
            child = page.frames[1]
            assert await page.locator("iframe").get_attribute("sandbox") == SANDBOX
            await frame.locator('nav button[data-page="bindings"]').click()
            await frame.get_by_role("button", name="管理员解绑", exact=True).click()
            await frame.locator("#editor[open]").wait_for()
            assert await frame.get_by_label("QQ ID", exact=True).input_value() == "100001"
            assert await frame.get_by_label("MC 玩家", exact=True).input_value() == "TestPlayer"
            assert await frame.get_by_label("QQ ID", exact=True).evaluate("el=>el.readOnly")
            assert await frame.get_by_label("MC 玩家", exact=True).evaluate("el=>el.readOnly")
            assert await child.evaluate("window.calls.filter(x=>x.body?.action==='binding_unbind').length") == 0
            screenshots = ROOT / "docs" / "screenshots"
            screenshots.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshots / "admin-unbind-sandbox.png"), full_page=True)
            await frame.locator("#cancel").click()
            await frame.locator("#editor[open]").wait_for(state="hidden")
            assert await child.evaluate("window.calls.filter(x=>x.body?.action==='binding_unbind').length") == 0

            # API errors remain visible in the DOM dialog; no silent false return.
            await child.evaluate("window.failNextUnbind=true")
            await frame.get_by_role("button", name="管理员解绑", exact=True).click()
            await frame.get_by_role("button", name="确认解绑", exact=True).click()
            await frame.locator("#dialog-error").filter(has_text="合成后端错误").wait_for()
            assert await frame.locator("#editor").is_visible()
            assert await child.evaluate("window.fixture.domain.bindings.length") == 1
            await frame.get_by_role("button", name="确认解绑", exact=True).click()
            await frame.locator("#output[open]").wait_for()
            assert "已解绑 TestPlayer" in await frame.locator("#output-text").inner_text()
            assert await child.evaluate("window.successfulUnbinds") == 1
            assert await child.evaluate("window.fixture.domain.bindings.length") == 0
            unbinds = await child.evaluate("window.calls.filter(x=>x.body?.action==='binding_unbind')")
            assert len(unbinds) == 2 and all(call["body"]["payload"] == {"user_id": "100001"} for call in unbinds)
            await frame.locator("#output-close").click()

            # Other destructive buttons use the shared DOM confirmation as well.
            await frame.locator('nav button[data-page="commands"]').click()
            await frame.get_by_role("button", name="删除", exact=True).click()
            confirm = frame.locator('dialog[aria-label="确认操作"]')
            await confirm.wait_for(state="visible")
            await confirm.get_by_role("button", name="取消", exact=True).click()
            assert await child.evaluate("window.calls.filter(x=>x.path==='settings').length") == 0
            await frame.get_by_role("button", name="删除", exact=True).click()
            await confirm.get_by_role("button", name="确认", exact=True).click()
            await child.wait_for_function("window.fixture.settings.commands.length===0")
            assert await child.evaluate("window.calls.filter(x=>x.path==='settings').length") == 1
            assert await child.evaluate("window.nativeConfirmCalls") == 0
            assert not errors, errors
            assert not blocked_modals, blocked_modals
            assert not unexpected_dialogs, unexpected_dialogs
            print(json.dumps({"sandbox": SANDBOX, "native_confirm_calls": 0, "unbind_cancel": True,
                              "backend_error_visible": True, "successful_unbinds": 1, "dom_delete_confirm": True,
                              "console_errors": errors}, ensure_ascii=False))
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
