"""Session points, confirmed Minecraft check-ins and private shop rewards."""
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
from astrbot.core.star.filter.command import GreedyStr

from .core import CHECKIN_COMMANDS, TEMPLATE_FIELDS, classify_mc_output, validate_settings
from .command_filters import install_command_filters
from .routing import render_reply
from .store import Store

NAME = "astrbot_plugin_checkin_points"
MC_NAME = "astrbot_plugin_minecraft_queqiao"


def session_id(value):
    value = str(value or "").strip()
    if len(value) > 255 or not re.fullmatch(r"[^\s:]+:[^\s:]+:[^\s]+", value):
        raise ValueError("请填写完整会话 ID，如 mybot:GroupMessage:123456789")
    return value


class CheckinPointsPlugin(Star):
    _operations = {
        "cmd_checkin": "签到", "cmd_points": "点数", "cmd_query": "查询",
        "cmd_set": "修改", "cmd_add": "增加", "cmd_subtract": "减少",
        "cmd_ranking": "排行榜", "cmd_shop_list": "商店列表", "cmd_buy": "购买",
        "cmd_exchange": "兑换", "cmd_collect": "补领", "cmd_help": "帮助",
        "cmd_reset": "重置签到", "cmd_makeup": "补签", "cmd_mc_ranking": "签到排行榜",
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = Store(Path(StarTools.get_data_dir(NAME)) / "checkin.sqlite3")
        self._sync_lock = asyncio.Lock()
        self._worker = None
        self._command_settings = {}
        self._command_filters = {}
        for endpoint, method, handler in (
            ("state", "GET", self.web_state), ("settings", "POST", self.web_settings),
            ("sessions/toggle", "POST", self.web_toggle),
            ("sessions/delete", "POST", self.web_delete),
            ("bindings/sync", "POST", self.web_sync),
        ):
            context.register_web_api(f"/{NAME}/{endpoint}", handler, [method], "签到点数管理")

    def mc_plugin(self):
        meta = self.context.get_registered_star(MC_NAME)
        return meta.star_cls if meta and meta.activated and meta.star_cls else None

    async def initialize(self):
        await asyncio.to_thread(self.store.recover_inflight)
        await self.sync_bindings()
        await self.refresh_command_settings()
        self._command_filters = install_command_filters(self, self._operations)
        self._worker = asyncio.create_task(self.resume_pending())

    async def refresh_command_settings(self):
        sessions = await asyncio.to_thread(self.store.sessions)
        self._command_settings = {row["session"]: {
            "enabled": row["settings"]["enabled"],
            "command_prefix": row["settings"]["command_prefix"],
            "checkin_prefix": row["settings"]["checkin_prefix"],
        } for row in sessions}

    def command_help(self, settings):
        groups = {"签到指令": [], "点数管理与商店": []}
        for canonical, entry in self._command_filters.items():
            if not entry.handler_md.enabled:
                continue
            is_checkin = canonical in CHECKIN_COMMANDS
            prefix = settings["checkin_prefix" if is_checkin else "command_prefix"]
            name = entry.command_name
            if prefix:
                name = prefix + " " + name
            groups["签到指令" if is_checkin else "点数管理与商店"].append(name)
        return "\n".join(label + "：" + "、".join(names) for label, names in groups.items() if names)

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
        state, detail = "unknown", "MC 返回结果未确认，未增加点数，请先检查游戏记录"
        try:
            settings = await asyncio.to_thread(self.store.get_settings, record["session"])
            if not self.config.get("enabled", True) or not settings["enabled"] or not settings["mc_enabled"]:
                state, detail = "skipped", "会话签到或 MC 联动已关闭，未执行指令、未增加点数"
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
                    current = await asyncio.to_thread(self.store.get_checkin, record["id"])
                    if not self.config.get("enabled", True) or not current or current["mc_state"] != "sending":
                        state = "skipped"
                        outputs.append("会话任务已取消，停止剩余 MC 指令")
                        break
                    await self.sync_bindings()
                    if await asyncio.to_thread(self.store.get_binding, account) != record["player"]:
                        state = "failed"
                        outputs.append("绑定已变更，停止剩余 MC 指令")
                        break
                    # Binding refresh may yield while the session is being removed.
                    current = await asyncio.to_thread(self.store.get_checkin, record["id"])
                    if not current or current["mc_state"] != "sending":
                        state = "skipped"
                        outputs.append("会话任务已取消，停止剩余 MC 指令")
                        break
                    output, channel = await instance.execute_command_with_channel(command)
                    if output is None or not channel:
                        outputs.append("MC 未返回可确认的响应，已停止后续指令，未增加点数")
                        break
                    result, clean = classify_mc_output(str(output), settings)
                    outputs.append(clean or "MC 返回空内容，无法确认签到成功")
                    if result != "sent":
                        state = result
                        break
                else:
                    state = "sent"
                detail = "\n".join(outputs)
        except asyncio.CancelledError:
            detail = "发送过程被中断，执行结果未知，未增加点数且不自动重试"
            raise
        except Exception as exc:
            logger.warning("[%s] MC 签到发送异常: %s", NAME, type(exc).__name__)
            detail = "发送发生异常，结果未知，未增加点数，请检查游戏记录"
        finally:
            await asyncio.shield(asyncio.to_thread(self.store.set_mc_state, record["id"], state, detail))
        return state

    @staticmethod
    def command_text(event):
        text = event.get_message_str().strip()
        for component in event.get_messages():
            if type(component).__name__ == "At":
                target = str(component.qq)
                name = str(getattr(component, "name", ""))
                text = text.replace(f"@{name}({target})", f"@{target}")
        return text

    async def resolve_user(self, event, target):
        if not target:
            return str(event.get_sender_id())
        target = target.lstrip("@")
        if re.fullmatch(r"\d{1,24}", target):
            return target
        matches = await asyncio.to_thread(self.store.lookup_users, event.get_platform_id(), target)
        if len(matches) != 1:
            raise ValueError("未找到唯一的绑定玩家，请使用 @用户 或账号 ID。")
        return str(matches[0])

    async def display_player(self, event, user_id):
        return await asyncio.to_thread(self.store.get_binding, f"{event.get_platform_id()}:User:{user_id}") or user_id

    @filter.command("签到 排行榜", priority=20)
    async def cmd_mc_ranking(self, event: AstrMessageEvent, args: GreedyStr):
        """查询 Minecraft 签到排行榜，使用本会话签到指令前缀。"""
        async for result in self.handle_command(event, "签到排行榜", args):
            yield result

    @filter.command("商店 购买", priority=20)
    async def cmd_buy(self, event: AstrMessageEvent, args: GreedyStr):
        """发起商品购买：商店 购买 商品序号。"""
        async for result in self.handle_command(event, "购买", args):
            yield result

    @filter.command("签到", alias={"checkin"}, priority=10)
    async def cmd_checkin(self, event: AstrMessageEvent, args: GreedyStr):
        """每日签到，使用本会话签到指令前缀。"""
        async for result in self.handle_command(event, "签到", args):
            yield result

    @filter.command("点数", alias={"points"}, priority=10)
    async def cmd_points(self, event: AstrMessageEvent, args: GreedyStr):
        """查询自己的点数，使用本会话签到指令前缀。"""
        async for result in self.handle_command(event, "点数", args):
            yield result

    @filter.command("查询", priority=10)
    async def cmd_query(self, event: AstrMessageEvent, args: GreedyStr):
        """查询余额；查询他人需要会话管理员或机器人管理员权限。"""
        async for result in self.handle_command(event, "查询", args):
            yield result

    @filter.command("修改", priority=10)
    async def cmd_set(self, event: AstrMessageEvent, args: GreedyStr):
        """管理员设定余额：修改 玩家 点数；省略玩家则修改自己。"""
        async for result in self.handle_command(event, "修改", args):
            yield result

    @filter.command("增加", priority=10)
    async def cmd_add(self, event: AstrMessageEvent, args: GreedyStr):
        """管理员增加余额：增加 玩家 点数。"""
        async for result in self.handle_command(event, "增加", args):
            yield result

    @filter.command("减少", priority=10)
    async def cmd_subtract(self, event: AstrMessageEvent, args: GreedyStr):
        """管理员减少余额：减少 玩家 点数。"""
        async for result in self.handle_command(event, "减少", args):
            yield result

    @filter.command("排行榜", priority=10)
    async def cmd_ranking(self, event: AstrMessageEvent, args: GreedyStr):
        """查询本会话点数排行榜。"""
        async for result in self.handle_command(event, "排行榜", args):
            yield result

    @filter.command("商店列表", alias={"商店", "商店 列表"}, priority=10)
    async def cmd_shop_list(self, event: AstrMessageEvent, args: GreedyStr):
        """查看商品序号、价格、可售库存与每人限购数量。"""
        async for result in self.handle_command(event, "商店列表", args):
            yield result

    @filter.command("兑换", priority=10)
    async def cmd_exchange(self, event: AstrMessageEvent, args: GreedyStr):
        """确认本人的购买或补发已付奖励；有订单时支持直接回复确认词。"""
        async for result in self.handle_command(event, "兑换", args):
            yield result

    @filter.command("补领", priority=10)
    async def cmd_collect(self, event: AstrMessageEvent, args: GreedyStr):
        """重发已付订单的同一奖励，不重复扣款。"""
        async for result in self.handle_command(event, "补领", args):
            yield result

    @filter.command("帮助", priority=10)
    async def cmd_help(self, event: AstrMessageEvent, args: GreedyStr):
        """显示当前可用指令（反映管理行为中的改名与停用）。"""
        async for result in self.handle_command(event, "帮助", args):
            yield result

    @filter.command("重置签到", priority=10)
    async def cmd_reset(self, event: AstrMessageEvent, args: GreedyStr):
        """管理员重置玩家今日插件签到状态，保留余额及历史，不清除 MC 记录。"""
        async for result in self.handle_command(event, "重置签到", args):
            yield result

    @filter.command("补签", priority=10)
    async def cmd_makeup(self, event: AstrMessageEvent, args: GreedyStr):
        """管理员补签：补签 玩家 YYYY-MM-DD；通过配置的游戏指令确认。"""
        async for result in self.handle_command(event, "补签", args):
            yield result

    async def handle_command(self, event: AstrMessageEvent, command: str, argument_text: str):
        """Run a canonical action already selected by its native command filter."""
        if not self.config.get("enabled", True):
            return
        session, user_id = event.unified_msg_origin, str(event.get_sender_id())
        settings = await asyncio.to_thread(self.store.get_settings, session)
        args = argument_text.split()
        active = None
        if command in {"兑换", "补领"}:
            active = await asyncio.to_thread(self.store.active_purchase, session, user_id)
        if not settings["enabled"] and not (active and active["status"] in {"paid", "delivery_failed", "sending"}):
            return  # Closing a session must not strand an already paid reward.
        event.stop_event()
        await self.sync_bindings()
        confirm_filter = self._command_filters.get("兑换")
        values = dict(user_id=user_id, player=await self.display_player(event, user_id),
                      minutes=settings["confirmation_minutes"], command_list=self.command_help(settings),
                      confirmation_command=confirm_filter.command_name if confirm_filter else "兑换")
        try:
            answer = await self.execute_command(event, settings, command, args, values)
        except ValueError as exc:
            answer = str(exc)
        except Exception as exc:
            logger.warning("[%s] 点数指令失败: %s", NAME, type(exc).__name__)
            answer = "处理未完成，请管理员查看记录；请勿重复操作未确认的游戏奖励。"
        yield event.plain_result(answer)

    async def execute_command(self, event, settings, command, args, values):
        session, user_id = event.unified_msg_origin, str(event.get_sender_id())
        reply = lambda key, **more: render_reply(settings, key, **{**values, **more})
        is_admin = event.is_admin() or user_id in settings["admin_ids"]
        if command == "重置签到":
            if not is_admin:
                return reply("no_permission")
            if len(args) != 1:
                return "用法：重置签到 玩家（可用 @用户、账号 ID 或绑定游戏名）。"
            target = await self.resolve_user(event, args[0])
            record = await asyncio.to_thread(self.store.reset_checkin, session, target, user_id)
            if not record["removed"]:
                return f"{record['day']} 没有该玩家的插件签到记录，余额未改变。"
            return reply("admin_reset", **{**record, "target_id": target,
                         "player": await self.display_player(event, target)})
        if command == "签到排行榜" and not args:
            return await self.mc_leaderboard(settings)
        if (command == "签到" and not args) or command == "补签":
            target = user_id
            if command == "补签":
                if not is_admin:
                    return reply("no_permission")
                if len(args) != 2:
                    return "用法：补签 玩家 YYYY-MM-DD，例如：补签 Steve 2026-10-23。"
                target = await self.resolve_user(event, args[0])
            player = await asyncio.to_thread(self.store.get_binding, f"{event.get_platform_id()}:User:{target}")
            if command == "补签":
                record = await asyncio.to_thread(self.store.makeup_checkin, session, target, player, args[1], user_id)
            else:
                record = await asyncio.to_thread(self.store.checkin, session, target, player)
            status = record["status"]
            if status == "success":
                await self.dispatch_mc(record)
                record = await asyncio.to_thread(self.store.get_checkin, record["id"])
                if record is None:
                    return "该次签到已被管理员重置，未继续执行。"
                key = ("makeup_success" if command == "补签" else "checkin_success") if record["credited"] else (
                    "checkin_mc_failed" if record["mc_state"] == "failed" else "checkin_mc_unknown")
            elif status == "duplicate":
                key = ("makeup_duplicate" if command == "补签" else "checkin_duplicate") if record["credited"] else (
                    "checkin_pending" if record["mc_state"] in {"pending", "sending"} else
                    "checkin_mc_failed" if record["mc_state"] == "failed" else "checkin_mc_unknown")
            else:
                key = "checkin_" + status
            output = reply(key, **{**record, "target_id": target, "player": player or target})
            if record.get("mc_detail"):
                output += "\nMC：" + record["mc_detail"]
            return output
        if command in {"点数", "查询"}:
            if len(args) > 1:
                return reply("usage")
            target = await self.resolve_user(event, args[0] if args else "")
            if target != user_id and not is_admin:
                return reply("no_permission")
            total = await asyncio.to_thread(self.store.balance, session, target)
            return reply("admin_query" if target != user_id else "balance", total=total,
                         target_id=target, player=await self.display_player(event, target))
        if command in {"修改", "增加", "减少"}:
            if not is_admin:
                return reply("no_permission")
            mentions = [str(c.qq) for c in event.get_messages() if type(c).__name__ == "At"
                        and str(c.qq) not in {str(event.get_self_id()), "all"}]
            if len(args) == 1:
                target, amount = (mentions[0] if len(mentions) == 1 else user_id), args[0]
            elif len(args) == 2:
                target, amount = await self.resolve_user(event, args[0]), args[1]
            else:
                return reply("usage")
            if not re.fullmatch(r"\d{1,19}", amount):
                raise ValueError("数量必须为非负整数。")
            result = await asyncio.to_thread(self.store.adjust_balance, session, target,
                {"修改": "set", "增加": "add", "减少": "subtract"}[command], int(amount), user_id)
            return reply("admin_adjust", **{**result, "target_id": target,
                         "player": await self.display_player(event, target), "points": int(amount)})
        if command == "排行榜" and not args:
            rows = await asyncio.to_thread(self.store.leaderboard, session, 10)
            items = []
            for rank, row in enumerate(rows, 1):
                items.append(reply("ranking_item", **{**row, "rank": rank,
                                   "player": await self.display_player(event, row["user_id"])}))
            return reply("ranking", items="\n".join(items) or "暂无点数记录。")
        if command == "商店列表" and not args:
            products = await asyncio.to_thread(self.store.product_catalog, session)
            items = [reply("shop_item", product_id=p["id"], product_name=p["name"], price=p["price"],
                           stock="不限" if p["stock"] is None else p["stock"])
                     + (f"；每人累计限购 {p['purchase_limit']} 件" if p["purchase_limit"] else "") for p in products]
            return reply("shop_list", items="\n".join(items) or "暂无在售商品。")
        if command == "购买" and len(args) == 1:
            if event.get_platform_name() != "aiocqhttp":
                return "当前平台无法确认主动私聊发货，请在 OneBot / NapCat 会话中购买；本次未扣款。"
            confirmation = self._command_filters.get("兑换")
            if confirmation is None or not confirmation.handler_md.enabled:
                return "购买确认指令已在管理行为中停用，请管理员启用后再购买；本次未扣款。"
            order = await asyncio.to_thread(self.store.create_purchase, session, user_id, args[0])
            return reply("purchase_pending", **{**order, "order_id": order["id"]})
        if command in {"兑换", "补领"} and not args:
            if event.get_platform_name() != "aiocqhttp":
                return "当前平台不支持该私聊发货方式，本次未扣款。"
            if command == "补领":
                active = await asyncio.to_thread(self.store.active_purchase, session, user_id)
                if not active or active["status"] not in {"paid", "delivery_failed", "sending"}:
                    return "没有需要补领的已付款订单。"
            order = await asyncio.to_thread(self.store.confirm_purchase, session, user_id)
            if order["status"] == "delivered":
                return "该订单已完成，奖励已私聊发送，未再次扣款。"
            if order["status"] == "sending":
                return "该订单正在私聊发货，请稍候；未再次扣款。"
            delivered = await self.deliver_purchase(event, settings, order, values)
            if delivered is None:
                return "该订单已由另一个请求处理，请查看私聊；未再次扣款。"
            return reply("purchase_delivered" if delivered else "purchase_failed",
                         **{**order, "order_id": order["id"]})
        return reply("usage")

    async def mc_leaderboard(self, settings):
        if not settings["mc_enabled"]:
            return "本会话尚未开启 MC 联动。"
        mc = self.mc_plugin()
        instance = mc.server_manager.get(settings["server_name"]) if mc else None
        if instance is None:
            return "鹊桥插件或目标服务器未加载，无法查询签到排行榜。"
        output, channel = await instance.execute_command_with_channel(settings["leaderboard_command"])
        if output is None or not channel:
            return "MC 签到排行榜查询未收到响应，请检查服务器连接。"
        _, clean = classify_mc_output(str(output), settings)
        return clean or "MC 未返回签到排行榜内容。"

    async def deliver_purchase(self, event, settings, order, values):
        if not await asyncio.to_thread(self.store.claim_delivery, order["id"]):
            return None
        success, detail = False, "私聊结果未确认，可在原会话使用补领重发同一份奖励，不重复扣分"
        try:
            text = render_reply(settings, "purchase_private", **{**values, **order, "order_id": order["id"]})
            result = await event.bot.send_private_msg(
                user_id=int(order["user_id"]), self_id=event.get_self_id(),
                message=[{"type": "text", "data": {"text": text}}])
            if isinstance(result, dict) and result.get("message_id") is not None:
                success, detail = True, f"平台已接受私聊，message_id={result['message_id']}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[%s] 订单 %s 私聊未确认: %s", NAME, order["id"], type(exc).__name__)
        finally:
            await asyncio.shield(asyncio.to_thread(self.store.finish_delivery, order["id"], success, detail))
        return success

    async def web_state(self):
        sessions = await asyncio.to_thread(self.store.sessions)
        session = request.query.get("session") or (sessions[0]["session"] if sessions else "")
        settings = await asyncio.to_thread(self.store.get_settings, session)
        history = await asyncio.to_thread(self.store.history, session) if session else []
        leaderboard = await asyncio.to_thread(self.store.leaderboard, session, 20) if session else []
        orders = await asyncio.to_thread(self.store.orders, session) if session else []
        mc = self.mc_plugin()
        servers = [{"name": x.server_name, "connected": x.connected} for x in mc.server_manager.all()] if mc else []
        count = await self.sync_bindings()
        return json_response(dict(ok=True, sessions=sessions, session=session, settings=settings,
                                  history=history, servers=servers, binding_count=count,
                                  leaderboard=leaderboard, orders=orders,
                                  template_fields=sorted(TEMPLATE_FIELDS)))

    async def web_settings(self):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求必须是 JSON 对象")
            session = session_id(body.get("session"))
            settings = validate_settings(body.get("settings", {}))
            settings = await asyncio.to_thread(self.store.save_settings, session, settings)
            await self.refresh_command_settings()
            return json_response(dict(ok=True, settings=settings))
        except (ValueError, TypeError, KeyError) as exc:
            return json_response(dict(ok=False, error=str(exc)), status_code=400)

    async def web_toggle(self):
        try:
            body = await request.json()
            if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
                raise ValueError("请提供会话 ID 和布尔类型的 enabled")
            await asyncio.to_thread(self.store.toggle_session, session_id(body.get("session")), body["enabled"])
            await self.refresh_command_settings()
            return json_response(dict(ok=True))
        except (ValueError, TypeError, KeyError) as exc:
            return json_response(dict(ok=False, error=str(exc)), status_code=400)

    async def web_delete(self):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求必须是 JSON 对象")
            await asyncio.to_thread(self.store.delete_session, session_id(body.get("session")))
            await self.refresh_command_settings()
            return json_response(dict(ok=True))
        except (ValueError, TypeError, KeyError) as exc:
            return json_response(dict(ok=False, error=str(exc)), status_code=400)

    async def web_sync(self):
        return json_response(dict(ok=True, count=await self.sync_bindings()))
