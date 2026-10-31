"""Signin policy GUI acceptance with real local config/API and mocked transport.

Runs in AstrBot's iframe sandbox against temporary SQLite files. No external
requests, live QQ account or Minecraft server are used.
"""
import asyncio
import copy
import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "mcqq_policy_gui"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
Application = importlib.import_module(PACKAGE + ".services.application").Application


async def main():
    (ROOT / "work").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "work", prefix="policy-gui-") as directory:
        app = Application(directory, {
            "servers": [{"id": "paper", "enabled": False}],
            "monitor": {"enabled": False}, "points_name": "星币",
            "routes": [{"umo": f"test:GroupMessage:{i}", "scope": "shared" if i < 3 else "other",
                        "server_id": "paper", "display_name": f"合成群 {i}", "points_min": 3, "points_max": 10}
                       for i in (1, 2, 3)],
        })
        for component in (app.gateway, app.monitor, app.media):
            component.reload = AsyncMock()
        app.gateway.rcon = AsyncMock(side_effect=AssertionError("GUI preview cannot send RCON"))
        untouched = copy.deepcopy(app.settings.data["routes"][2])
        errors, calls = [], []

        async def api(path, body=None):
            calls.append((path, copy.deepcopy(body)))
            try:
                if path == "state":
                    return app.snapshot()
                if path == "settings":
                    return await app.web("settings", body, "dashboard:synthetic")
                return await app.web(body["action"], body["payload"], "dashboard:synthetic")
            except (ValueError, RuntimeError) as exc:
                return {"status": "error", "message": str(exc)}

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    executable_path=os.environ.get("MCQQ_BROWSER", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                    headless=True, args=["--disable-gpu"],
                )
                page = await browser.new_page(viewport={"width": 1440, "height": 1100})
                page.set_default_timeout(15000)
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                await page.expose_function("policyApi", api)

                async def serve(route):
                    if route.request.url.endswith("/host.html"):
                        await route.fulfill(content_type="text/html", body=(
                            '<!doctype html><meta charset="utf-8"><iframe src="/index.html" '
                            'sandbox="allow-scripts allow-forms allow-downloads" '
                            'style="width:1400px;height:1060px;border:0"></iframe>'
                        ))
                        return
                    name = route.request.url.rsplit("/", 1)[-1]
                    if name not in {"index.html", "app.js", "style.css"}:
                        await route.fulfill(status=404, body="External requests disabled")
                        return
                    await route.fulfill(body=(ROOT / "views/dashboard" / name).read_bytes(),
                        content_type={"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name],
                        headers={"Access-Control-Allow-Origin": "*"})

                await page.route("**/*", serve)
                await page.add_init_script("""window.AstrBotPluginView={ready:async()=>({}),
                    apiGet:path=>window.policyApi(path),apiPost:(path,body)=>window.policyApi(path,body)};""")
                await page.goto("http://policy.test/host.html")
                frame = page.frame_locator("iframe")
                await frame.locator('nav button[data-page="routes"]').click()
                await frame.get_by_role("button", name="签到时段与活动", exact=True).first.click()
                policy = frame.get_by_role("dialog", name="签到时段与奖励活动", exact=True)
                await policy.get_by_text("作用域 shared 共 2 条路由", exact=False).wait_for()
                await policy.get_by_role("button", name="编辑每日时段", exact=True).click()
                assert not await frame.get_by_label("限制整条签到指令", exact=True).is_checked()
                await frame.get_by_label("限制整条签到指令", exact=True).check()
                await frame.get_by_label("每日开始时间", exact=True).fill("08:00")
                await frame.get_by_label("每日结束时间", exact=True).fill("09:00")

                async def save_editor():
                    await frame.locator("#save").click()
                    await frame.locator("#editor[open]").wait_for(state="hidden")

                await save_editor()
                assert all(r["signin_time"] == {"enabled": True, "start": "08:00", "end": "09:00"}
                           for r in app.settings.data["routes"][:2])
                assert app.settings.data["routes"][2] == untouched
                await policy.get_by_role("button", name="新增奖励活动", exact=True).click()
                await frame.get_by_label("活动名称", exact=True).fill("周末倍率")
                await frame.get_by_label("开始日期", exact=True).fill("2026-10-31")
                await frame.get_by_label("结束日期", exact=True).fill("2026-11-01")
                await frame.get_by_label("奖励倍率", exact=True).fill("1.5")
                await save_editor()
                assert app.settings.data["routes"][0]["reward_events"] == app.settings.data["routes"][1]["reward_events"]

                async def preview(at, sample=""):
                    before = copy.deepcopy(app.settings.data)
                    audit_count = app._ops.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
                    await policy.get_by_role("button", name="预览签到策略", exact=True).click()
                    await frame.get_by_label("预览日期时间（留空使用当前时间）", exact=True).fill(at)
                    await frame.get_by_label("模拟抽样点数（可选）", exact=True).fill(sample)
                    await save_editor()
                    await frame.locator("#output[open]").wait_for()
                    text = await frame.locator("#output-text").inner_text()
                    await frame.locator("#output-close").click()
                    assert app.settings.data == before
                    assert app._ops.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == audit_count
                    return text

                text = await preview("2026-10-31T08:30", "3")
                assert "允许执行签到指令" in text and "3 × 1.5 → 4 星币" in text
                assert "4–15 星币" in text
                text = await preview("2026-10-31T12:00")
                assert "不会执行游戏签到" in text and "不代表可领取" in text
                text = await preview("2026-11-02T08:30", "3")
                assert "无，使用基础奖励" in text and "3 × 1 → 3 星币" in text

                await policy.get_by_role("button", name="新增奖励活动", exact=True).click()
                await frame.get_by_label("活动名称", exact=True).fill("节日范围")
                await frame.get_by_label("开始日期", exact=True).fill("2026-10-31")
                await frame.get_by_label("结束日期", exact=True).fill("2026-10-31")
                await frame.get_by_label("奖励方式", exact=True).select_option("range")
                assert not await frame.get_by_label("奖励倍率", exact=True).is_visible()
                await frame.get_by_label("活动奖励下限", exact=True).fill("20")
                await frame.get_by_label("活动奖励上限", exact=True).fill("30")
                await frame.locator("#save").click()
                await frame.locator("#dialog-error").filter(has_text="优先级相同").wait_for()
                assert len(app.settings.data["routes"][0]["reward_events"]) == 1
                await frame.get_by_label("优先级", exact=True).fill("10")
                await save_editor()
                text = await preview("2026-10-31T08:30", "21")
                assert "节日范围（优先级 10）" in text and "21 × 1 → 21 星币" in text
                row = policy.locator("tbody tr").filter(has_text="节日范围")
                await row.get_by_role("button", name="关闭活动", exact=True).click()
                await row.get_by_role("button", name="启用活动", exact=True).wait_for()
                assert "周末倍率" in await preview("2026-10-31T08:30", "3")
                await row.get_by_role("button", name="启用活动", exact=True).click()
                await row.get_by_role("button", name="关闭活动", exact=True).wait_for()
                await row.get_by_role("button", name="编辑", exact=True).click()
                await frame.get_by_label("活动名称", exact=True).fill("节日特别奖励")
                await save_editor()
                await policy.get_by_text("节日特别奖励", exact=True).wait_for()
                screenshots = ROOT / "docs/screenshots"
                await page.screenshot(path=str(screenshots / "signin-policy.png"), full_page=True)
                row = policy.locator("tbody tr").filter(has_text="节日特别奖励")
                await row.get_by_role("button", name="删除", exact=True).click()
                confirm = frame.get_by_role("dialog", name="确认操作", exact=True)
                await confirm.get_by_role("button", name="取消", exact=True).click()
                assert len(app.settings.data["routes"][0]["reward_events"]) == 2
                await row.get_by_role("button", name="删除", exact=True).click()
                await confirm.get_by_role("button", name="确认", exact=True).click()
                await policy.get_by_text("节日特别奖励", exact=True).wait_for(state="hidden")
                assert len(app.settings.data["routes"][0]["reward_events"]) == 1
                # Hidden invalid controls must not block native form submission.
                await policy.get_by_role("button", name="新增奖励活动", exact=True).click()
                await frame.get_by_label("活动名称", exact=True).fill("关闭的备用活动")
                await frame.get_by_label("开始日期", exact=True).fill("2026-10-31")
                await frame.get_by_label("结束日期", exact=True).fill("2026-11-01")
                await frame.get_by_label("启用活动", exact=True).uncheck()
                await frame.get_by_label("奖励方式", exact=True).select_option("range")
                await frame.get_by_label("活动奖励下限", exact=True).fill("-1")
                await frame.get_by_label("奖励方式", exact=True).select_option("multiplier")
                await save_editor()
                backup_row = policy.locator("tbody tr").filter(has_text="关闭的备用活动")
                await backup_row.get_by_role("button", name="启用活动", exact=True).click()
                await policy.get_by_role("alert").filter(has_text="优先级相同").wait_for()
                assert not app.settings.data["routes"][0]["reward_events"][-1]["enabled"]
                await backup_row.get_by_role("button", name="编辑", exact=True).click()
                await frame.get_by_label("奖励倍率", exact=True).fill("not-a-number")
                await frame.get_by_label("奖励方式", exact=True).select_option("range")
                await frame.get_by_label("活动奖励下限", exact=True).fill("20")
                await frame.get_by_label("活动奖励上限", exact=True).fill("30")
                await save_editor()
                assert app.settings.data["routes"][0]["reward_events"][-1]["mode"] == "range"
                await policy.get_by_role("button", name="编辑每日时段", exact=True).click()
                await frame.get_by_label("每日开始时间", exact=True).fill("22:00")
                await frame.get_by_label("每日结束时间", exact=True).fill("02:00")
                await save_editor()
                assert "允许执行签到指令" in await preview("2026-11-01T01:59")
                await policy.get_by_role("button", name="编辑每日时段", exact=True).click()
                await frame.get_by_label("每日开始时间", exact=True).fill("")
                await frame.get_by_label("每日结束时间", exact=True).fill("")
                await frame.get_by_label("限制整条签到指令", exact=True).uncheck()
                await save_editor()
                assert app.settings.data["routes"][0]["signin_time"] == {"enabled": False, "start": "22:00", "end": "02:00"}
                assert "允许执行签到指令" in await preview("2026-10-31T12:00")
                app.gateway.rcon.assert_not_awaited()
                assert app.settings.data["routes"][2] == untouched
                # New routes and routes moved into a scope inherit its policies.
                await policy.get_by_role("button", name="关闭", exact=True).click()
                await frame.get_by_role("button", name="添加路由", exact=True).click()
                await frame.get_by_label("原始 UMO", exact=True).fill("test:GroupMessage:4")
                await frame.get_by_label("点数作用域", exact=True).fill("shared")
                await frame.get_by_label("签到奖励下限", exact=True).fill("3")
                await frame.get_by_label("签到奖励上限", exact=True).fill("10")
                await save_editor()
                source = app.settings.route("test:GroupMessage:1")
                added = app.settings.route("test:GroupMessage:4")
                assert added["signin_time"] == source["signin_time"]
                assert added["reward_events"] == source["reward_events"]
                other_row = frame.locator("tbody tr").filter(has_text="test:GroupMessage:3")
                await other_row.get_by_role("button", name="编辑与开关", exact=True).click()
                await frame.get_by_label("点数作用域", exact=True).fill("shared")
                await save_editor()
                moved = app.settings.route("test:GroupMessage:3")
                assert moved["signin_time"] == source["signin_time"]
                assert moved["reward_events"] == source["reward_events"]
                assert moved["display_name"] == untouched["display_name"]
                assert not errors, errors
                print(json.dumps({"signin_policy_flows": 16, "real_config_api": True,
                    "iframe_sandbox": True, "no_rcon": True, "console_errors": errors}, ensure_ascii=False))
                await browser.close()
        finally:
            await app.close()


if __name__ == "__main__":
    asyncio.run(main())
