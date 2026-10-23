"""按会话独立的每日签到积分与鹊桥签到联动。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import request, json_response

from .core import validate_settings
from .store import Store

NAME = "astrbot_plugin_checkin_points"
MC_NAME = "astrbot_plugin_minecraft_queqiao"


def session_id(value):
    value = str(value or "").strip()
    if len(value) > 255 or not re.fullmatch(r"[^\s:]+:[^\s:]+:[^\s]+", value):
        raise ValueError("请填写完整会话 ID，如 mybot:GroupMessage:123456789")
    return value


class CheckinPointsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = Store(Path(StarTools.get_data_dir(NAME)) / "checkin.sqlite3")
        self._sync_lock = asyncio.Lock()
        self._worker = None
        for endpoint, method, handler in (
            ("state", "GET", self.web_state),
            ("settings", "POST", self.web_settings),
            ("bindings/sync", "POST", self.web_sync),
        ):
            context.register_web_api(f"/{NAME}/{endpoint}", handler, [method], "签到积分管理")

    def mc_plugin(self):
        meta = self.context.get_registered_star(MC_NAME)
        return meta.star_cls if meta and meta.activated and meta.star_cls else None

    async def initialize(self):
        await asyncio.to_thread(self.store.recover_inflight)
        await self.sync_bindings()
        self._worker = asyncio.create_task(self.resume_pending())

    async def resume_pending(self):
        while True:
            await asyncio.sleep(15)
            if not self.config.get("enabled", True) or not self.mc_plugin():
                continue
            try:
                for record in await asyncio.to_thread(self.store.pending_mc):
                    await self.dispatch_mc(record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[%s] 恢复待发送签到失败: %s", NAME, type(exc).__name__)

    async def terminate(self):
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    async def sync_bindings(self):
        async with self._sync_lock:
            plugin = self.mc_plugin()
            if plugin:
                bindings = plugin.binding_service.all_bindings()
            else:
                path = Path(StarTools.get_data_dir(MC_NAME)) / "bindings.json"
                try:
                    bindings = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8")) if path.exists() else {}
                except (OSError, ValueError):
                    logger.warning("[%s] 无法读取鹊桥绑定，暂停使用绑定快照", NAME)
                    bindings = {}
            if not isinstance(bindings, dict):
                bindings = {}
            bindings = {str(k): v for k, v in bindings.items()
                        if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_]{1,16}", v)}
            return await asyncio.to_thread(self.store.sync_bindings, bindings)

    async def dispatch_mc(self, record):
        if record.get("mc_state") != "pending":
            return record.get("mc_state", "skipped")
        if not await asyncio.to_thread(self.store.claim_mc, record["id"]):
            return "unknown"
        state, detail = "unknown", "MC 返回结果未确认，请先检查游戏记录，勿重复发奖"
        try:
            settings = await asyncio.to_thread(self.store.get_settings, record["session"])
            if not settings["enabled"] or not settings["mc_enabled"]:
                state, detail = "skipped", "会话签到或 MC 联动已关闭，跳过待发送指令"
                return state
            await self.sync_bindings()
            account = f"{record['session'].split(':', 1)[0]}:User:{record['user_id']}"
            current_player = await asyncio.to_thread(self.store.get_binding, account)
            if current_player != record["player"]:
                state, detail = "failed", "绑定已变更或已解绑，未发送旧玩家的签到指令"
                return state
            mc = self.mc_plugin()
            instance = mc.server_manager.get(record["server_name"]) if mc else None
            if instance is None:
                state, detail = "failed", "鹊桥插件或目标服务器未加载，未发送指令"
            else:
                outputs = []
                for command in record["commands"]:
                    output, channel = await instance.execute_command_with_channel(command)
                    if output is None or not channel:
                        detail = f"已有 {len(outputs)} 条指令收到响应；后续结果未知，停止发送且不自动重试"
                        break
                    outputs.append(str(output)[:1000])
                else:
                    state, detail = "sent", "\n".join(outputs) or "MC 已返回响应；是否签到成功以游戏插件结果为准"
        except asyncio.CancelledError:
            detail = "发送过程被中断，执行结果未知，不自动重试"
            raise
        except Exception as exc:
            logger.warning("[%s] MC 签到发送异常: %s", NAME, type(exc).__name__)
            detail = "发送发生异常，结果未知，请检查游戏记录"
        finally:
            await asyncio.shield(asyncio.to_thread(self.store.set_mc_state, record["id"], state, detail))
        return state

    @filter.command("签到", alias={"checkin"})
    async def checkin(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return
        session = event.unified_msg_origin
        await self.sync_bindings()
        player = await asyncio.to_thread(self.store.get_binding, f"{event.get_platform_id()}:User:{event.get_sender_id()}")
        record = await asyncio.to_thread(self.store.checkin, session, event.get_sender_id(), player)
        status = record["status"]
        messages = {
            "disabled": "本会话尚未开启签到，请管理员在签到积分面板配置。",
            "outside_window": "当前不在本会话允许的签到时间内。",
            "unbound": "请先绑定 Minecraft 游戏名，再进行签到。",
        }
        if status == "duplicate":
            text = f"今天已经签到了，本次不会重复发放积分或执行 MC 指令。当前积分：{record['total']}"
        elif status != "success":
            text = messages.get(status, "签到未完成，请联系管理员。")
        else:
            state = await self.dispatch_mc(record)
            text = f"签到成功！获得 {record['points']} 积分，累计 {record['total']} 积分。"
            if state == "sent":
                text += f"\n已向 MC 发送 {player} 的签到指令并收到响应。"
            elif state in {"failed", "unknown"}:
                text += "\n积分已保留，MC 同步未确认，请管理员查看签到记录。"
        yield event.plain_result(text)
        event.stop_event()

    @filter.command("积分", alias={"points"})
    async def points(self, event: AstrMessageEvent):
        total = await asyncio.to_thread(self.store.balance, event.unified_msg_origin, event.get_sender_id())
        yield event.plain_result(f"你在本会话的积分：{total}")
        event.stop_event()

    async def web_state(self):
        sessions = await asyncio.to_thread(self.store.sessions)
        session = request.query.get("session") or (sessions[0]["session"] if sessions else "")
        settings = await asyncio.to_thread(self.store.get_settings, session)
        history = await asyncio.to_thread(self.store.history, session) if session else []
        mc = self.mc_plugin()
        servers = [{"name": x.server_name, "connected": x.connected} for x in mc.server_manager.all()] if mc else []
        count = await self.sync_bindings()
        return json_response(dict(ok=True, sessions=sessions, session=session, settings=settings,
                                  history=history, servers=servers, binding_count=count))

    async def web_settings(self):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求必须是 JSON 对象")
            session = session_id(body.get("session"))
            settings = validate_settings(body.get("settings", {}))
            await asyncio.to_thread(self.store.save_settings, session, settings)
            return json_response(dict(ok=True, settings=settings))
        except (ValueError, TypeError, KeyError) as exc:
            return json_response(dict(ok=False, error=str(exc)), status_code=400)

    async def web_sync(self):
        return json_response(dict(ok=True, count=await self.sync_bindings()))
