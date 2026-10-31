"""Binding, two-stage check-in and one-currency shop application services."""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from datetime import datetime
from uuid import uuid4
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

try:
    from ..core.store import DomainError, Store
    from ..core.text import SIGNIN_RULES, clean_text, parse_rules, render_command, render_template, validate_player
except ImportError:  # Standalone offline tests.
    from core.store import DomainError, Store
    from core.text import SIGNIN_RULES, clean_text, parse_rules, render_command, render_template, validate_player


DEFAULT_SIGNIN = "玩家: {player}\n{group_result}\n{game_result}"
DEFAULT_ALREADY_SIGNED = DEFAULT_SIGNIN


class Domain:
    def __init__(self, store: Store, settings, gateway):
        self.store, self.settings, self.gateway = store, settings, gateway
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self.notify = None  # Optional async callback (umo, text), supplied by Application.

    def _route(self, ctx):
        route = self.settings.route(ctx["umo"])
        if not route or not route.get("enabled", True):
            raise DomainError("当前会话未配置或已关闭")
        return route

    @staticmethod
    def _key(action, ctx, args=""):
        message_id = ctx.get("request_id") or ctx.get("message_id") or uuid4().hex
        raw = "\x1f".join(map(str, (action, ctx.get("umo", ""), ctx["user_id"], message_id, args)))
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _result(text, status="success", **fields):
        return dict(text=str(text), status=status, **fields)

    def _day(self, route):
        zone = ZoneInfo(route.get("timezone") or self.settings.data.get("timezone", "Asia/Shanghai"))
        return datetime.now(zone).date().isoformat()

    def _points_name(self):
        return str(self.settings.data.get("points_name") or "点数")

    @staticmethod
    def _binding_identity(binding):
        return binding.get("identity") or ("uuid:" + binding["mc_uuid"] if binding.get("mc_uuid") else "name:" + binding["player"].casefold())

    def _same_binding(self, left, right):
        return self._binding_identity(left) == self._binding_identity(right) and left.get("version") == right.get("version")

    @staticmethod
    def _target_qq(value):
        value = str(value).strip()
        if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            raise DomainError("目标 QQ 号必须是有效的数字 ID")
        return value

    def _binding_arguments(self, action, ctx, args):
        """Resolve admin targets while preserving members' valid numeric MC names."""
        tokens, user = args.split(), str(ctx["user_id"])
        if action == "unbind":
            if not tokens:
                return user, None
            self._admin_only(ctx)
            if len(tokens) != 1:
                raise DomainError("用法：解绑；管理员可使用：解绑 QQ号")
            return self._target_qq(tokens[0]), None
        command = "修改绑定" if action == "rebind" else "绑定"
        if len(tokens) == 1 and (not tokens[0].isdigit() or not ctx.get("is_admin", False)):
            return user, validate_player(tokens[0])
        if len(tokens) == 2:
            self._admin_only(ctx)
            return self._target_qq(tokens[0]), validate_player(tokens[1])
        raise DomainError(f"用法：{command} MC名；管理员代操作：{command} QQ号 MC名（QQ号后必须填写 MC名）")

    def _admin_binding(self, action, target, actor, *, player=None, server_id=None):
        """Shared trusted path for command admins and authenticated GUI admins."""
        target = self._target_qq(target)
        cooldown = self.settings.data.get("binding_cooldown_seconds", 86400)
        if action == "unbind":
            return self.store.unbind(target, cooldown, actor=str(actor), force=True)
        return self.store.bind_name(target, player, server_id, cooldown, rebind=action == "rebind",
                                    actor=str(actor), force=True)

    def _unbind_message(self, result, *, admin=False):
        if result.get("cancelled_pending"):
            return f"已取消 QQ {result['qq_id']} 的待验证绑定（{result['player']}），原验证码已作废。"
        target = f"QQ {result['qq_id']} 的 " if admin else ""
        return f"已解绑 {target}{result['player']}，{self._points_name()}和签到记录保留；普通用户再次绑定仍受冷却限制。"

    async def execute(self, action, ctx, args=""):
        """Execute a verified AstrBot command without importing AstrBot itself."""
        args = str(args or "").strip()
        try:
            route = self._route(ctx)
            ctx = dict(ctx)
            ctx["scope"] = route.get("scope") or ctx.get("scope") or ctx["umo"]
            ctx["server_id"] = ctx.get("server_id") or route.get("server_id", "")
            user = str(ctx["user_id"])
            points_name = self._points_name()
            if action in {"binding", "rebind"}:
                target, player = self._binding_arguments(action, ctx, args)
                if ctx.get("is_admin", False):
                    result = self._admin_binding(action, target, user, player=player, server_id=ctx["server_id"])
                    label = f"QQ {target} " if target != user else ""
                    return self._result(label + ("当前已经绑定 " if result["already"] else "绑定成功：") + result["binding"]["player"])
                if not self.settings.data.get("binding_verification_enabled", False):
                    result = self.store.bind_name(target, player, ctx["server_id"], self.settings.data.get("binding_cooldown_seconds", 86400), rebind=action == "rebind", actor=user)
                    return self._result(("当前已经绑定 " if result["already"] else "绑定成功：") + result["binding"]["player"])
                result = self.store.begin_binding(user, player, ctx["server_id"], ctx["umo"], rebind=action == "rebind")
                if result["already"]:
                    return self._result("当前已经绑定 " + result["binding"]["player"])
                return self._result(f"请使用 {player} 在对应游戏服务器发送：bind {result['code']}\n验证码 5 分钟内有效，只能使用一次。", "pending")
            if action == "binding_status":
                bound = self.store.binding(user)
                if not bound:
                    return self._result("尚未绑定 MC 账号。请使用绑定指令。")
                seconds = max(0, int(bound["cooldown_until"] - time.time()))
                identity_text = f"UUID：{bound['mc_uuid']}（已验证）" if bound["verified"] else "绑定方式：玩家名（未验证）"
                return self._result(f"当前绑定：{bound['player']}\n{identity_text}\n变更冷却剩余：{seconds} 秒")
            if action == "unbind":
                target, _ = self._binding_arguments(action, ctx, args)
                is_admin = bool(ctx.get("is_admin", False))
                old = self._admin_binding(action, target, user) if is_admin else self.store.unbind(target, self.settings.data.get("binding_cooldown_seconds", 86400), actor=user)
                return self._result(self._unbind_message(old, admin=is_admin))
            if action == "signin":
                return await self._signin(ctx, route, args)
            if action == "balance":
                target = user
                if args:
                    self._admin_only(ctx)
                    target = self.store.resolve_user(args)
                balance = self.store.balance(ctx["scope"], target)
                return self._result(f"{points_name}：{balance['total']}\n可用：{balance['available']}\n冻结：{balance['frozen']}")
            if action == "rank":
                rows = self.store.rank(ctx["scope"])
                return self._result(points_name + "排行榜\n" + ("\n".join(f"{i}. {r['qq_id']}：{r['total']}" for i, r in enumerate(rows, 1)) or "暂无记录"))
            if action in {"points_add", "points_sub", "points_set"}:
                self._admin_only(ctx)
                tokens = args.split(maxsplit=2)
                reason = "群管理员调整"
                if len(tokens) == 1:
                    target, amount = user, int(tokens[0])
                elif len(tokens) >= 2:
                    target, amount = self.store.resolve_user(tokens[0]), int(tokens[1])
                    if len(tokens) == 3:
                        reason = tokens[2].strip() or reason
                else:
                    raise DomainError("用法：指令 QQ ID 或绑定玩家名 数量 [原因]；只填数量则调整自己")
                result = self.store.adjust_points(ctx["scope"], target, action.removeprefix("points_"), amount, user, self._key(action, ctx, args), reason)
                return self._result(f"{target} 当前{points_name}：{result['total']}（变动 {result['delta']:+d}）")
            if action == "shop":
                rows = [p for p in self.store.list_products(ctx["scope"]) if p["enabled"]]
                lines = [f"{p['id']} · {p['name']} · {p['price']} {points_name} · 库存 {'不限' if p['stock'] < 0 else p['stock']}" for p in rows]
                return self._result(points_name + "商店\n" + ("\n".join(lines) or "暂无在售商品"))
            if action == "purchase":
                if not args:
                    raise DomainError("请填写商品编号")
                binding = self.store.binding(user)
                product = self.store.get_product(ctx["scope"], args)
                if product["kind"] == "rcon":
                    binding = self._binding(ctx)
                elif not callable(ctx.get("reply_private")):
                    raise DomainError("该商品需要当前 OneBot 接入支持私聊交付")
                order = self.store.create_order(ctx["scope"], user, args, self._key(action, ctx, args), ctx["umo"], binding)
                confirm_name = ctx.get("confirmation_command", "确认兑换")
                return self._result(f"订单 {order['id']}\n商品：{order['snapshot']['name']}\n价格：{order['snapshot']['price']} {points_name}\n尚未扣除{points_name}，请在 3 分钟内发送 {confirm_name} {order['id']}。", "pending")
            if action in {"confirm", "reclaim"}:
                return await self._confirm(ctx, args, reclaim=action == "reclaim")
            if action == "orders":
                rows = self.store.orders(ctx["scope"], user, 20)
                return self._result("兑换记录\n" + ("\n".join(f"{o['id']} · {o['snapshot']['name']} · {o['status']}" for o in rows) or "暂无记录"))
            raise DomainError("未知业务指令")
        except (DomainError, ValueError) as exc:
            return self._result(str(exc).replace("点数", self._points_name()), "error")

    @staticmethod
    def _admin_only(ctx):
        if not ctx.get("is_admin", False):
            raise DomainError("需要管理员权限")

    def _binding(self, ctx):
        bound = self.store.binding(ctx["user_id"])
        if not bound:
            raise DomainError("请先绑定 MC 账号")
        if bound["server_id"] != ctx["server_id"]:
            raise DomainError("当前绑定所属服务器与本会话目标服务器不同")
        validate_player(bound["player"])
        return bound

    async def _signin(self, ctx, route, args):
        if args:
            raise DomainError("签到指令不需要参数")
        user = str(ctx["user_id"])
        # Across scopes: one player cannot race two remote sign-in calls.
        lock = self._locks.setdefault("signin:" + user, asyncio.Lock())
        async with lock:
            bound = self._binding(ctx)
            operation_id = self._key("signin", ctx)
            previous = self.store.signin_operation(operation_id)
            if previous:
                if previous["state"] == "completed":
                    return self._signin_message(previous["result"], previous["binding"], route)
                if previous["state"] == "game_confirmed":
                    result = self.store.credit_signin(operation_id)
                    return self._signin_message(result, previous["binding"], route)
                if previous["state"] in ("sending", "unknown", "failed"):
                    if previous.get("result", {}) and previous["result"].get("reply"):
                        return self._result(previous["result"]["reply"], previous["state"])
                    return self._result("本次请求已处理或游戏结果待核对；未重复发送游戏命令。", previous["state"])
                if not self._same_binding(previous["binding"], bound):
                    self.store.set_signin_state(operation_id, "failed", detail="发送前绑定已变化，未执行旧请求")
                    return self._result("绑定已变更，本次旧签到请求未发送，请重新签到。", "error")
                if previous["day"] != self._day(route):
                    self.store.set_signin_state(operation_id, "failed", detail="未发送请求已跨日，不能替新一天执行签到")
                    return self._result("旧签到请求已跨日且未发送，请发起新的签到请求。", "error")
            day = self._day(route)
            minimum, maximum = int(route.get("points_min", 10)), int(route.get("points_max", 10))
            if not 0 <= minimum <= maximum <= 10**9:
                raise DomainError("签到点数范围配置不合法")
            amount = minimum + secrets.randbelow(maximum - minimum + 1)
            op = self.store.begin_signin(operation_id, ctx["scope"], user, day, amount, bound, ctx["umo"])
            command = render_command(route.get("signin_command", "signin click {player}"),
                                     {"player": bound["player"], "player_name": bound["player"], "user_id": user, "date": day})
            self.store.set_signin_state(operation_id, "sending")
            try:
                raw = await self.gateway.rcon(ctx["server_id"], command)
                displayed_rcon = clean_text(raw)
                parsed = parse_rules(raw, route.get("signin_rules") or SIGNIN_RULES,
                                     bound["player"], route.get("signin_prefixes", ["[手记人偶]"]))
                game_status = str(parsed["status"]).upper()
                if game_status not in ("SIGNED", "ALREADY_SIGNED"):
                    state = "failed" if game_status in ("FAILED", "FAILURE", "ERROR") else "unknown"
                    message = self._signin_unconfirmed_text(game_status, displayed_rcon)
                    self.store.set_signin_state(operation_id, state, game_status, displayed_rcon,
                                                {"rcon": displayed_rcon, "reply": message, "points_name": self._points_name()})
                    return self._result(message, state)
                if self._day(route) != op["day"]:
                    message = "签到失败❌️ 本次请求跨越签到日期边界，群签到暂未结算，请核对后处理。"
                    self.store.set_signin_state(operation_id, "unknown", game_status, "请求跨越签到日期边界，需要核对实际游戏签到日期",
                                                {"rcon": displayed_rcon, "reply": message, "points_name": self._points_name()})
                    return self._result(message, "unknown")
                self.store.set_signin_state(operation_id, "game_confirmed", game_status, displayed_rcon,
                                            {"game_continuous_days": parsed["fields"].get("days", ""),
                                             "rcon": displayed_rcon, "points_name": self._points_name()})
                result = self.store.credit_signin(operation_id)
                return self._signin_message(result, bound, route)
            except asyncio.CancelledError:
                current = self.store.signin_operation(operation_id)
                if current["state"] == "sending":
                    self.store.set_signin_state(operation_id, "unknown", detail="任务中断，游戏结果待核对")
                raise
            except Exception as exc:
                current = self.store.signin_operation(operation_id)
                # Preserve a confirmed remote result so recovery can finish the local transaction.
                if current["state"] == "sending":
                    state = "failed" if type(exc).__name__ == "GatewayUnavailable" else "unknown"
                    self.store.set_signin_state(operation_id, state, detail=type(exc).__name__)
                return self._result(f"签到失败❌️ 游戏或群结算状态需要核对；没有重复增加{self._points_name()}。", "unknown")

    @staticmethod
    def _signin_unconfirmed_text(game_status, rcon):
        reason = "游戏签到失败" if game_status in ("FAILED", "FAILURE", "ERROR") else "游戏签到结果无法确认"
        message = f"签到失败❌️ {reason}，本次群签到未结算。"
        return message + ("\n" + rcon if rcon else "")

    def _signin_values(self, result, binding):
        """Derive the two result lines from this operation, never from an older daily row."""
        game_status = str(result.get("game_status", "UNKNOWN")).upper()
        group_status = str(result.get("group_status", "NOT_SETTLED")).upper()
        raw_days = result.get("days", result.get("game_continuous_days", ""))
        days = str(raw_days) if raw_days is not None and re.fullmatch(r"[0-9]+", str(raw_days)) else ""
        values = dict(result, points_name=result.get("points_name") or self._points_name(),
                      rcon=result.get("rcon", ""), player=binding["player"], player_name=binding["player"],
                      days=days, game_continuous_days=days, game_status=game_status, group_status=group_status,
                      status=game_status)
        if group_status == "SIGNED":
            values["group_status_text"] = "签到成功"
            values["group_result"] = (f"群内签到成功✅️ 本次获得: {values.get('points_delta', 0)} "
                                      f"{values['points_name']} 当前{values['points_name']}:{values.get('balance', '')}")
        elif group_status == "ALREADY_SIGNED":
            values["group_status_text"] = "今日已签到"
            values["group_result"] = "群内签到失败❌️ 今日已签到 请不要重复签到"
        else:
            values["group_status_text"] = "未结算"
            values["group_result"] = "群内签到未结算：游戏结果尚未确认。"
        if game_status == "SIGNED":
            values["game_status_text"] = "本次签到成功"
            # Older stored results/custom rules may not contain continuous days.
            # A confirmed success remains a success without inventing a count.
            values["game_result"] = "游戏签到成功✅️" + (f" 已连续签到 {days} 天。" if days else "")
        elif game_status == "ALREADY_SIGNED":
            values["game_status_text"] = "今日已签到"
            values["game_result"] = "游戏签到失败❌️ 今日已签到 请不要重复签到"
        else:
            failed = game_status in ("FAILED", "FAILURE", "ERROR")
            values["game_status_text"] = "签到失败" if failed else "结果未知"
            values["game_result"] = "游戏签到失败❌️" if failed else "游戏签到结果未知，请核对服务器回包。"
        return values

    def _signin_message(self, result, binding, route):
        values = self._signin_values(result, binding)
        already_signed = values["group_status"] == "ALREADY_SIGNED"
        key, default = ("signin_already", DEFAULT_ALREADY_SIGNED) if already_signed else ("signin", DEFAULT_SIGNIN)
        template = route.get("templates", {}).get(key, default)
        return dict(status="success", text=render_template(template, values), data=values)

    def preview_signin(self, raw, player, route, group_signed=False, points_delta=1, balance=1):
        """Pure two-stage preview; no account reads, database writes or game calls."""
        player = validate_player(str(player))
        if not isinstance(group_signed, bool):
            raise ValueError("模拟群签到状态必须为布尔值")
        for value in (points_delta, balance):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
                raise ValueError("模拟奖励和余额必须为非负整数")
        parsed = parse_rules(raw, route.get("signin_rules") or SIGNIN_RULES,
                             player, route.get("signin_prefixes", ["[手记人偶]"]))
        game_status = str(parsed["status"]).upper()
        confirmed = game_status in ("SIGNED", "ALREADY_SIGNED")
        result = {"game_status": game_status,
                  "group_status": ("ALREADY_SIGNED" if group_signed else "SIGNED") if confirmed else "NOT_SETTLED",
                  "points_delta": points_delta if confirmed and not group_signed else 0,
                  "balance": balance, "day": "", "points_name": self._points_name(),
                  "game_continuous_days": parsed["fields"].get("days", ""), "rcon": clean_text(raw)}
        values = self._signin_values(result, {"player": player})
        preview = self._signin_message(result, {"player": player}, route)["text"] if confirmed else self._signin_unconfirmed_text(game_status, result["rcon"])
        return {**parsed, "preview": preview, "data": values}

    async def on_game_event(self, server_id, event):
        if event.get("event_name") not in ("PlayerChatEvent", "PlayerCommandEvent"):
            return False
        text = str(event.get("command") or event.get("message") or "").strip()
        match = re.fullmatch(r"/?(?:bind|绑定)\s+([A-Fa-f0-9]{10})", text)
        if not match:
            return False
        if not self.settings.data.get("binding_verification_enabled", False):
            return True  # Do not expose or apply a challenge after verification is disabled.
        player = event.get("player") or {}
        try:
            name = validate_player(str(player.get("nickname") or player.get("name") or ""))
            result = self.store.complete_binding(match[1], str(player.get("uuid", "")), name, server_id,
                                                 self.settings.data.get("binding_cooldown_seconds", 86400))
            if self.notify:
                try:
                    await self.notify(result["umo"], f"绑定成功：{name}。QQ 与 MC 账号均为唯一绑定。")
                except Exception:
                    self.store.audit("binding_notification_failed", "system", {"qq_id": result["binding"]["qq_id"]})
        except (ValueError, DomainError):
            # Never forward binding codes into QQ or audit their plaintext.
            self.store.audit("binding_verification_rejected", "game", {"server_id": server_id})
        return True

    async def _confirm(self, ctx, order_id, reclaim=False):
        user = str(ctx["user_id"])
        points_name = self._points_name()
        lock = self._locks.setdefault("order-user:" + ctx["scope"] + ":" + user, asyncio.Lock())
        async with lock:
            if not order_id:
                orders = self.store.orders(ctx["scope"], user, 100)
                candidates = [o for o in orders if o["status"] in (("unknown", "reserved", "fulfilled", "paid_pending") if reclaim else ("awaiting_confirm", "reserved", "unknown", "paid_pending"))]
                if not candidates:
                    raise DomainError("没有可处理的订单，请先选择商品")
                order_id = candidates[0]["id"]
            order = self.store.order(order_id, True)
            if not order or order["scope"] != ctx["scope"] or order["qq_id"] != user:
                raise DomainError("订单不存在或不属于当前用户和会话")
            kind = order["snapshot"]["kind"]
            if kind in ("text", "code") and not callable(ctx.get("reply_private")):
                raise DomainError(f"当前接入不支持私聊交付，未扣除{points_name}")
            if reclaim and kind == "rcon":
                raise DomainError("游戏交付不能自动补发，请由管理员核对订单")
            if order["status"] == "fulfilled" and not reclaim:
                return self._result(f"订单已完成，没有重复扣除{points_name}。")
            if kind == "rcon" and order["status"] in ("awaiting_confirm", "reserved"):
                current = self._binding(ctx)
                if not self._same_binding(current, order["binding"]):
                    raise DomainError("绑定已变更，请重新选择商品")
            if not reclaim:
                order = self.store.reserve_order(order_id, ctx["scope"], user)
            delivery = self.store.claim_delivery(order_id, reclaim=reclaim)
            if not delivery:
                return self._result("订单正在处理或结果待核对，没有重复执行交付。", "pending")
            try:
                if kind in ("text", "code"):
                    accepted = await ctx["reply_private"](user, f"订单 {order_id} · {delivery['snapshot']['name']}\n{delivery['reward']}")
                    if accepted:
                        final = self.store.finish_order(order_id, "fulfilled", "OneBot 已返回消息标识")
                        return self._result(f"订单 {order_id} 已完成，奖励已私聊发送。{points_name}只扣除一次。", data=final)
                    self.store.finish_order(order_id, "unknown", "未取得私聊消息标识")
                    return self._result(f"私聊交付未确认，{points_name}与同一份奖励已保留；可使用补领重发同一份奖励。", "unknown")
                binding = delivery["binding"]
                command = render_command(delivery["snapshot"]["command"], {"player": binding["player"], "player_name": binding["player"],
                            "user_id": user, "points": delivery["snapshot"]["price"], "points_name": points_name, "order_id": order_id})
                raw = await self.gateway.rcon(binding["server_id"], command)
                parsed = parse_rules(raw, delivery["snapshot"]["rules"], binding["player"])
                if parsed["status"].upper() in ("SUCCESS", "SIGNED", "OK"):
                    self.store.finish_order(order_id, "fulfilled", "已匹配配置的交付成功规则")
                    return self._result(f"订单 {order_id} 交付完成。")
                # An arbitrary command's error response does not prove absence of side effects.
                self.store.finish_order(order_id, "unknown", "游戏交付未得到成功证明：" + parsed["cleaned"][:1000])
                return self._result(f"订单 {order_id} 交付结果待管理员核对，{points_name}仍处于冻结状态。", "unknown")
            except asyncio.CancelledError:
                self.store.finish_order(order_id, "unknown", "交付任务中断")
                raise
            except Exception as exc:
                if type(exc).__name__ == "GatewayUnavailable" and kind == "rcon":
                    self.store.finish_order(order_id, "cancelled", "连接不可用，命令未发送")
                    return self._result(f"游戏连接不可用，未发送交付命令；订单已取消并释放{points_name}。", "error")
                self.store.finish_order(order_id, "unknown", "交付异常：" + type(exc).__name__)
                return self._result(f"交付结果待核对，未重复扣除{points_name}或重新分配奖励。", "unknown")

    async def admin(self, action, payload, actor):
        """Called only through the authenticated GUI/administrator integration."""
        try:
            return await self._admin(action, payload, actor)
        except DomainError as exc:
            raise DomainError(str(exc).replace("点数", self._points_name())) from exc

    async def _admin(self, action, payload, actor):
        scope = str(payload.get("scope", ""))
        if action == "product_save":
            if not scope:
                raise DomainError("必须指定点数作用域")
            product = payload.get("product", payload)
            if product.get("kind") == "rcon":
                import regex
                rules = product.get("rules", [])
                if not any(str(r.get("status", "")).upper() in ("SUCCESS", "OK", "SIGNED") for r in rules):
                    raise DomainError("游戏商品需要明确的交付成功识别规则")
                for rule in rules:
                    regex.compile(str(rule.get("pattern", "")))
                render_command(product.get("command", ""), {"player": "Example", "player_name": "Example", "user_id": "1", "points": 1, "points_name": self._points_name(), "order_id": "example"})
            return self.store.save_product(scope, product, actor)
        if action == "product_get":
            return self.store.get_product(scope, str(payload.get("product_id", payload.get("id", ""))), True)
        if action == "product_delete":
            return {"disabled": self.store.delete_product(scope, str(payload.get("product_id", payload.get("id", ""))), actor)}
        if action == "product_codes":
            return {"added": self.store.add_codes(scope, str(payload.get("product_id", payload.get("id", ""))), payload.get("codes", []), actor)}
        if action == "order_resolve":
            outcome = {"success": "fulfilled", "failed": "cancelled"}.get(payload.get("outcome"), payload.get("outcome"))
            if outcome not in ("fulfilled", "cancelled"):
                raise DomainError("核对结果必须为已交付或确认未交付")
            if not str(payload.get("reason", "")).strip():
                raise DomainError("请填写核对依据")
            order_id = str(payload.get("order_id", payload.get("id", "")))
            order = self.store.order(order_id)
            if not order or (scope and order["scope"] != scope):
                raise DomainError("订单不存在或作用域不匹配")
            return self.store.finish_order(order_id, outcome, str(payload["reason"]), actor)
        if action == "points_adjust":
            if not payload.get("operation_id"):
                raise DomainError("点数调整需要唯一 operation_id")
            return self.store.adjust_points(scope, str(payload["user_id"]), str(payload["mode"]), payload["amount"], actor, str(payload["operation_id"]), str(payload.get("reason", "")))
        if action == "binding_unbind":
            result = self._admin_binding("unbind", payload["user_id"], actor)
            return dict(result, text=self._unbind_message(result, admin=True))
        if action == "import_preview":
            return self.store.preview_legacy(str(payload["path"]), payload.get("scope_map", {}),
                                            **self._migration_binding_options(payload))
        if action == "import_apply":
            if payload.get("confirmed") is not True:
                raise DomainError("请先预览并明确确认数据导入")
            return self.store.import_legacy(str(payload["path"]), str(payload["digest"]), actor, payload.get("scope_map", {}),
                                            **self._migration_binding_options(payload),
                                            plan_digest=payload.get("plan_digest", ""))
        raise DomainError("未知管理操作")

    def _migration_binding_options(self, payload):
        server_id = payload.get("binding_server_id", "")
        server_map = payload.get("server_map", {})
        choices = payload.get("binding_choices")
        if not isinstance(server_id, str) or not isinstance(server_map, dict):
            raise DomainError("绑定目标服务器和服务器映射格式无效")
        known = {str(server["id"]) for server in self.settings.data.get("servers", [])}
        if server_id and server_id not in known:
            raise DomainError("绑定目标服务器不存在，请先添加服务器")
        if any(not isinstance(key, str) or not isinstance(value, str) or value not in known
               for key, value in server_map.items()):
            raise DomainError("服务器映射必须指向已配置的服务器 ID")
        if choices is not None and (not isinstance(choices, list) or not all(isinstance(key, str) for key in choices)):
            raise DomainError("请选择需要迁移的旧绑定记录")
        return {"binding_server_id": server_id, "server_map": server_map,
                "binding_choices": choices, "routes": self.settings.data.get("routes", [])}

    async def recover(self):
        return self.store.recover()

    async def close(self):
        # The owner first cancels in-flight application tasks, then closes storage.
        self._locks.clear()
