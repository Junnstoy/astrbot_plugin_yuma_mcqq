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
    from ..core.text import SIGNIN_RULES, parse_rules, render_command, render_template, validate_player
except ImportError:  # Standalone offline tests.
    from core.store import DomainError, Store
    from core.text import SIGNIN_RULES, parse_rules, render_command, render_template, validate_player


DEFAULT_SIGNIN = "玩家：{player}\n游戏签到：{game_status_text}\n群签到：{group_status_text}\n本次获得：{points_delta} 点数\n当前点数：{balance}"


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

    async def execute(self, action, ctx, args=""):
        """Execute a verified AstrBot command without importing AstrBot itself."""
        args = str(args or "").strip()
        try:
            route = self._route(ctx)
            ctx = dict(ctx)
            ctx["scope"] = route.get("scope") or ctx.get("scope") or ctx["umo"]
            ctx["server_id"] = ctx.get("server_id") or route.get("server_id", "")
            user = str(ctx["user_id"])
            if action in {"binding", "rebind"}:
                player = validate_player(args)
                result = self.store.begin_binding(user, player, ctx["server_id"], ctx["umo"], rebind=action == "rebind")
                if result["already"]:
                    return self._result("当前已经绑定 " + result["binding"]["player"])
                return self._result(f"请使用 {player} 在对应游戏服务器发送：bind {result['code']}\n验证码 5 分钟内有效，只能使用一次。", "pending")
            if action == "binding_status":
                bound = self.store.binding(user)
                if not bound:
                    return self._result("尚未绑定 MC 账号。请使用绑定指令。")
                seconds = max(0, int(bound["cooldown_until"] - time.time()))
                return self._result(f"当前绑定：{bound['player']}\nUUID：{bound['mc_uuid']}\n变更冷却剩余：{seconds} 秒")
            if action == "unbind":
                old = self.store.unbind(user, self.settings.data.get("binding_cooldown_seconds", 86400))
                return self._result(f"已解绑 {old['player']}，点数和签到记录保留；再次绑定仍受冷却限制。")
            if action == "signin":
                return await self._signin(ctx, route, args)
            if action == "balance":
                target = user
                if args:
                    self._admin_only(ctx)
                    target = self.store.resolve_user(args)
                balance = self.store.balance(ctx["scope"], target)
                return self._result(f"点数：{balance['total']}\n可用：{balance['available']}\n冻结：{balance['frozen']}")
            if action == "rank":
                rows = self.store.rank(ctx["scope"])
                return self._result("点数排行榜\n" + ("\n".join(f"{i}. {r['qq_id']}：{r['total']}" for i, r in enumerate(rows, 1)) or "暂无记录"))
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
                return self._result(f"{target} 当前点数：{result['total']}（变动 {result['delta']:+d}）")
            if action == "shop":
                rows = [p for p in self.store.list_products(ctx["scope"]) if p["enabled"]]
                lines = [f"{p['id']} · {p['name']} · {p['price']} 点数 · 库存 {'不限' if p['stock'] < 0 else p['stock']}" for p in rows]
                return self._result("点数商店\n" + ("\n".join(lines) or "暂无在售商品"))
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
                return self._result(f"订单 {order['id']}\n商品：{order['snapshot']['name']}\n价格：{order['snapshot']['price']} 点数\n尚未扣点，请在 3 分钟内发送 {confirm_name} {order['id']}。", "pending")
            if action in {"confirm", "reclaim"}:
                return await self._confirm(ctx, args, reclaim=action == "reclaim")
            if action == "orders":
                rows = self.store.orders(ctx["scope"], user, 20)
                return self._result("兑换记录\n" + ("\n".join(f"{o['id']} · {o['snapshot']['name']} · {o['status']}" for o in rows) or "暂无记录"))
            raise DomainError("未知业务指令")
        except (DomainError, ValueError) as exc:
            return self._result(str(exc), "error")

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
                    return self._result("本次请求已处理或游戏结果待核对；未重复发送游戏命令。", previous["state"])
                if previous["binding"].get("mc_uuid") != bound["mc_uuid"] or previous["binding"].get("version") != bound["version"]:
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
                parsed = parse_rules(raw, route.get("signin_rules") or SIGNIN_RULES,
                                     bound["player"], route.get("signin_prefixes", ["[手记人偶]"]))
                game_status = str(parsed["status"]).upper()
                if game_status not in ("SIGNED", "ALREADY_SIGNED"):
                    state = "failed" if game_status in ("FAILED", "FAILURE", "ERROR") else "unknown"
                    self.store.set_signin_state(operation_id, state, game_status, parsed["cleaned"])
                    return self._result("游戏签到失败或结果无法确认，本次群签到未结算。", state)
                if self._day(route) != op["day"]:
                    self.store.set_signin_state(operation_id, "unknown", game_status, "请求跨越签到日期边界，需要核对实际游戏签到日期")
                    return self._result("本次请求跨越签到日期边界，群签到暂未结算，请核对后处理。", "unknown")
                self.store.set_signin_state(operation_id, "game_confirmed", game_status, parsed["cleaned"],
                                            {"game_continuous_days": parsed["fields"].get("days", "")})
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
                return self._result("签到执行未完成，游戏或群结算状态需要核对；没有重复加点。", "unknown")

    @staticmethod
    def _signin_message(result, binding, route):
        values = dict(result, player=binding["player"], player_name=binding["player"],
                      game_status_text="本次签到成功" if result["game_status"] == "SIGNED" else "今日已签到",
                      group_status_text="签到成功" if result["group_status"] == "SIGNED" else "今日已签到")
        template = route.get("templates", {}).get("signin", DEFAULT_SIGNIN)
        return dict(status="success", text=render_template(template, values), data=result)

    async def on_game_event(self, server_id, event):
        if event.get("event_name") not in ("PlayerChatEvent", "PlayerCommandEvent"):
            return False
        text = str(event.get("command") or event.get("message") or "").strip()
        match = re.fullmatch(r"/?(?:bind|绑定)\s+([A-Fa-f0-9]{10})", text)
        if not match:
            return False
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
                raise DomainError("当前接入不支持私聊交付，未扣点")
            if reclaim and kind == "rcon":
                raise DomainError("游戏交付不能自动补发，请由管理员核对订单")
            if order["status"] == "fulfilled" and not reclaim:
                return self._result("订单已完成，没有重复扣点。")
            if kind == "rcon" and order["status"] in ("awaiting_confirm", "reserved"):
                current = self._binding(ctx)
                if current["mc_uuid"] != order["binding"].get("mc_uuid") or current["version"] != order["binding"].get("version"):
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
                        return self._result(f"订单 {order_id} 已完成，奖励已私聊发送。点数只扣除一次。", data=final)
                    self.store.finish_order(order_id, "unknown", "未取得私聊消息标识")
                    return self._result("私聊交付未确认，点数与同一份奖励已保留；可使用补领重发同一份奖励。", "unknown")
                binding = delivery["binding"]
                command = render_command(delivery["snapshot"]["command"], {"player": binding["player"], "player_name": binding["player"],
                            "user_id": user, "points": delivery["snapshot"]["price"], "order_id": order_id})
                raw = await self.gateway.rcon(binding["server_id"], command)
                parsed = parse_rules(raw, delivery["snapshot"]["rules"], binding["player"])
                if parsed["status"].upper() in ("SUCCESS", "SIGNED", "OK"):
                    self.store.finish_order(order_id, "fulfilled", "已匹配配置的交付成功规则")
                    return self._result(f"订单 {order_id} 交付完成。")
                # An arbitrary command's error response does not prove absence of side effects.
                self.store.finish_order(order_id, "unknown", "游戏交付未得到成功证明：" + parsed["cleaned"][:1000])
                return self._result(f"订单 {order_id} 交付结果待管理员核对，点数仍处于冻结状态。", "unknown")
            except asyncio.CancelledError:
                self.store.finish_order(order_id, "unknown", "交付任务中断")
                raise
            except Exception as exc:
                if type(exc).__name__ == "GatewayUnavailable" and kind == "rcon":
                    self.store.finish_order(order_id, "cancelled", "连接不可用，命令未发送")
                    return self._result("游戏连接不可用，未发送交付命令；订单已取消并释放点数。", "error")
                self.store.finish_order(order_id, "unknown", "交付异常：" + type(exc).__name__)
                return self._result("交付结果待核对，未重复扣点或重新分配奖励。", "unknown")

    async def admin(self, action, payload, actor):
        """Called only through the authenticated GUI/administrator integration."""
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
                render_command(product.get("command", ""), {"player": "Example", "player_name": "Example", "user_id": "1", "points": 1, "order_id": "example"})
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
            return self.store.unbind(str(payload["user_id"]), self.settings.data.get("binding_cooldown_seconds", 86400), actor=actor, force=bool(payload.get("force", False)))
        if action == "import_preview":
            return self.store.preview_legacy(str(payload["path"]))
        if action == "import_apply":
            if payload.get("confirmed") is not True:
                raise DomainError("请先预览并明确确认数据导入")
            return self.store.import_legacy(str(payload["path"]), str(payload["digest"]), actor, payload.get("scope_map", {}))
        raise DomainError("未知管理操作")

    async def recover(self):
        return self.store.recover()

    async def close(self):
        # The owner first cancels in-flight application tasks, then closes storage.
        self._locks.clear()
