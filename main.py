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

from .core import TEMPLATE_FIELDS, classify_mc_output, validate_settings
from .routing import COMMANDS, parse_command, render_reply
from .store import Store

NAME = "astrbot_plugin_checkin_points"
MC_NAME = "astrbot_plugin_minecraft_queqiao"


def session_id(value):
    value = str(value or "").strip()
    if len(value) > 255 or not re.fullmatch(r"[^\s:]+:[^\s:]+:[^\s]+", value):
        raise ValueError("请填写完整会话 ID，如 mybot:GroupMessage:123456789")
    return value


class PointsCommandFilter(filter.CustomFilter):
    """Do not wake normal MC/AI chat; the handler verifies the session prefix."""

    def filter(self, event, cfg):
        text = event.get_message_str().strip()
        if text == "兑换":
            return True
        if not event.is_at_or_wake_command:
            return False
        words = text.lower().split()
        candidates = set(COMMANDS) | {"checkin", "points"}
        return bool(words and (words[0] in candidates or
                               (len(words) > 1 and words[1] in candidates)))


class CheckinPointsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = Store(Path(StarTools.get_data_dir(NAME)) / "checkin.sqlite3")
        self._sync_lock = asyncio.Lock()
        self._worker = None
        for endpoint, method, handler in (
            ("state", "GET", self.web_state), ("settings", "POST", self.web_settings),
            ("sessions/toggle", "POST", self.web_toggle),
            ("sessions/delete", "POST", self.web_delete),
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
        state, detail = "unknown", "MC 返回结果未确认，未增加积分，请先检查游戏记录"
        try:
            settings = await asyncio.to_thread(self.store.get_settings, record["session"])
            if not self.config.get("enabled", True) or not settings["enabled"] or not settings["mc_enabled"]:
                state, detail = "skipped", "会话签到或 MC 联动已关闭，未执行指令、未增加积分"
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
                        outputs.append("MC 未返回可确认的响应，已停止后续指令，未增加积分")
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
            detail = "发送过程被中断，执行结果未知，未增加积分且不自动重试"
            raise
        except Exception as exc:
            logger.warning("[%s] MC 签到发送异常: %s", NAME, type(exc).__name__)
            detail = "发送发生异常，结果未知，未增加积分，请检查游戏记录"
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

    @filter.custom_filter(PointsCommandFilter, priority=10)
    async def on_command(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return
        session, user_id = event.unified_msg_origin, str(event.get_sender_id())
        settings = await asyncio.to_thread(self.store.get_settings, session)
        text = self.command_text(event)
        parsed = parse_command(text, settings["command_prefix"], event.is_at_or_wake_command)
        if parsed is None:
            return
        command, args = parsed
        active = None
        if command in {"兑换", "补领"}:
            active = await asyncio.to_thread(self.store.active_purchase, session, user_id)
            if text == "兑换" and not active:
                return
        if not settings["enabled"] and not (active and active["status"] in {"paid", "delivery_failed", "sending"}):
            return  # Closing a session must not strand an already paid reward.
        event.stop_event()
        await self.sync_bindings()
        values = dict(user_id=user_id, player=await self.display_player(event, user_id),
                      minutes=settings["confirmation_minutes"])
        try:
            answer = await self.execute_command(event, settings, command, args, values)
        except ValueError as exc:
            answer = str(exc)
        except Exception as exc:
            logger.warning("[%s] 积分指令失败: %s", NAME, type(exc).__name__)
            answer = "处理未完成，请管理员查看记录；请勿重复操作未确认的游戏奖励。"
        yield event.plain_result(answer)

    async def execute_command(self, event, settings, command, args, values):
        session, user_id = event.unified_msg_origin, str(event.get_sender_id())
        reply = lambda key, **more: render_reply(settings, key, **{**values, **more})
        is_admin = event.is_admin() or user_id in settings["admin_ids"]
        if command == "签到" and not args:
            player = await asyncio.to_thread(self.store.get_binding, f"{event.get_platform_id()}:User:{user_id}")
            record = await asyncio.to_thread(self.store.checkin, session, user_id, player)
            status = record["status"]
            if status == "success":
                await self.dispatch_mc(record)
                record = await asyncio.to_thread(self.store.get_checkin, record["id"])
                key = "checkin_success" if record["credited"] else (
                    "checkin_mc_failed" if record["mc_state"] == "failed" else "checkin_mc_unknown")
            elif status == "duplicate":
                key = "checkin_duplicate" if record["credited"] else (
                    "checkin_pending" if record["mc_state"] in {"pending", "sending"} else
                    "checkin_mc_failed" if record["mc_state"] == "failed" else "checkin_mc_unknown")
            else:
                key = "checkin_" + status
            output = reply(key, **record)
            if record.get("mc_detail"):
                output += "\nMC：" + record["mc_detail"]
            return output
        if command in {"积分", "查询"}:
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
            return reply("ranking", items="\n".join(items) or "暂无积分记录。")
        if (command == "商店列表" and not args) or (command == "商店" and args == ["列表"]):
            products = await asyncio.to_thread(self.store.product_catalog, session)
            items = [reply("shop_item", product_id=p["id"], product_name=p["name"], price=p["price"],
                           stock="不限" if p["stock"] is None else p["stock"]) for p in products]
            return reply("shop_list", items="\n".join(items) or "暂无在售商品。")
        if command == "商店" and len(args) == 2 and args[0] == "购买":
            if event.get_platform_name() != "aiocqhttp":
                return "当前平台无法确认主动私聊发货，请在 OneBot / NapCat 会话中购买；本次未扣款。"
            order = await asyncio.to_thread(self.store.create_purchase, session, user_id, args[1])
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
            await asyncio.to_thread(self.store.save_settings, session, settings)
            return json_response(dict(ok=True, settings=settings))
        except (ValueError, TypeError, KeyError) as exc:
            return json_response(dict(ok=False, error=str(exc)), status_code=400)

    async def web_toggle(self):
        try:
            body = await request.json()
            if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
                raise ValueError("请提供会话 ID 和布尔类型的 enabled")
            await asyncio.to_thread(self.store.toggle_session, session_id(body.get("session")), body["enabled"])
            return json_response(dict(ok=True))
        except (ValueError, TypeError, KeyError) as exc:
            return json_response(dict(ok=False, error=str(exc)), status_code=400)

    async def web_delete(self):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求必须是 JSON 对象")
            await asyncio.to_thread(self.store.delete_session, session_id(body.get("session")))
            return json_response(dict(ok=True))
        except (ValueError, TypeError, KeyError) as exc:
            return json_response(dict(ok=False, error=str(exc)), status_code=400)

    async def web_sync(self):
        return json_response(dict(ok=True, count=await self.sync_bindings()))
