"""Composition root shared by AstrBot commands and the authenticated GUI."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import shlex
import sqlite3
import time
import uuid
import weakref
from datetime import datetime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.config import Settings
from ..core.store import Store
from ..core.text import clean_text, parse_rules, parse_signin, render_command, render_template, validate_player
from .domain import Domain
from .gateway import Gateway, GatewayError, GatewayUnavailable, OutcomeUnknown
from .media import MediaService
from .monitor import Monitor
from .relay import Relay

log = logging.getLogger("astrbot_plugin_yuma_mcqq")


def tracked(method):
    """Drain live operations before closing SQLite on plugin reload."""
    @wraps(method)
    async def call(self, *args, **kwargs):
        if self._closing:
            raise RuntimeError("插件正在停止，请稍后重试")
        task = asyncio.current_task()
        self._active[task] = self._active.get(task, 0) + 1
        try:
            return await method(self, *args, **kwargs)
        finally:
            count = self._active.get(task, 1) - 1
            if count:
                self._active[task] = count
            else:
                self._active.pop(task, None)
    return call


class Application:
    def __init__(self, data_dir: str | Path, bootstrap: dict | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(self.data_dir / "settings.json", bootstrap)
        self.store = Store(self.data_dir / "domain.sqlite3")
        self.gateway = Gateway(self.settings, self._on_event)
        self.media = MediaService(self.data_dir, self.settings)
        self.relay = Relay(self.settings, self.gateway, self.media)
        self.domain = Domain(self.store, self.settings, self.gateway)
        self.monitor = Monitor(self.store, self.settings, self.gateway, self.data_dir)
        self._send = None
        self._locks = weakref.WeakValueDictionary()
        self._cooldowns: dict[tuple, float] = {}
        self._settings_lock = asyncio.Lock()
        self._closed = False
        self._closing = False
        self._active: dict[asyncio.Task, int] = {}
        self._ops = sqlite3.connect(self.data_dir / "operations.sqlite3")
        self._ops.row_factory = sqlite3.Row
        self._ops.execute("PRAGMA journal_mode=WAL")
        self._ops.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, umo TEXT, action TEXT, server_id TEXT, status TEXT, text TEXT, created_at REAL, updated_at REAL)")
        self._ops.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, actor TEXT, action TEXT, detail TEXT, created_at REAL)")
        self._ops.execute("UPDATE runs SET status='UNKNOWN',text='进程中断，命令执行结果待核对。' WHERE status='SENDING'")
        self._ops.commit()

    async def start(self, on_game_message_callback):
        self._send = on_game_message_callback
        self.domain.notify = on_game_message_callback
        await self.domain.recover()
        await self.media.start()
        await self.gateway.start()
        await self.monitor.start()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._closing = True
        pending = [t for t in self._active if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for component in (self.monitor, self.relay, self.gateway, self.media, self.domain):
            try:
                await component.close()
            except Exception as exc:
                log.error("[Yuma MCQQ] 后台组件关闭失败 (%s)", type(exc).__name__)
        self._ops.close()
        self.store.close()

    @tracked
    async def _on_event(self, server_id: str, payload: dict):
        if not self.settings.data["enabled"]:
            return
        if await self.domain.on_game_event(server_id, payload):
            return
        for umo, message in await self.relay.from_game(server_id, payload):
            if self._send:
                await self._send(umo, message)

    def revision(self) -> str:
        return hashlib.sha256(json.dumps(self.settings.data, sort_keys=True).encode()).hexdigest()[:16]

    def snapshot(self) -> dict:
        return {"settings": self.settings.public(), "revision": self.revision(),
                "connections": self.gateway.status(), "domain": self.store.snapshot(),
                "monitor": self.monitor.status(), "media": self.media.status(),
                "runs": [dict(r) for r in self._ops.execute("SELECT id,action,server_id,status,created_at,updated_at FROM runs ORDER BY created_at DESC LIMIT 100")],
                "audit": [dict(r) for r in self._ops.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100")]}

    @tracked
    async def execute(self, action: str, ctx: dict, args: str = "") -> dict:
        if not self.settings.data["enabled"]:
            return {"status": "DISABLED", "text": "Yuma MCQQ 当前已停用。"}
        route = self.settings.route(ctx.get("umo", ""))
        if not route or not route.get("enabled", True):
            return {"status": "DISABLED", "text": "当前会话尚未启用，请管理员在插件 GUI 配置群服路由。"}
        ctx = {**ctx, "scope": route.get("scope", ctx["umo"]), "server_id": ctx.get("server_id") or route["server_id"]}
        if action == "rcon":
            # The raw administrator action always addresses this conversation's
            # configured server, never a caller-supplied context override.
            return await self._raw_rcon({**ctx, "server_id": route["server_id"]}, args)
        if action.startswith("custom:"):
            return await self._custom(action[7:], ctx, args)
        if action == "status":
            if args.strip():
                raise ValueError("服务器状态不需要参数")
            status = next((item for item in self.gateway.status() if item.get("id", item.get("server_id")) == ctx["server_id"]), {})
            return {"status": "SUCCESS", "text": "在线" if status.get("connected") else "离线"}
        if action in {"tps", "mspt", "ping"}:
            if args.strip():
                raise ValueError("此命令不需要参数；历史图片请使用性能图指令")
            server = self.settings.server(ctx["server_id"])
            command = render_command(server[action + "_command"], {})
            try:
                raw = await self.gateway.rcon(ctx["server_id"], command)
            except GatewayUnavailable:
                return {"status": "FAILED", "text": "服务器离线或 RCON 不可用。"}
            except OutcomeUnknown:
                return {"status": "UNKNOWN", "text": "查询已发送，但未收到确定的 RCON 回包，请稍后再试。"}
            return {"status": "SUCCESS", "text": clean_text(raw) or "服务器返回了空内容。"}
        if action == "performance":
            parts = shlex.split(args)
            server_id = ctx["server_id"]
            hours = 24
            if parts:
                if parts[0].isdigit():
                    hours = max(1, min(168, int(parts[0])))
                elif ctx.get("is_admin"):
                    server_id = parts[0]
                else:
                    raise ValueError("普通成员只能查询当前群绑定的服务器")
            server = self.settings.server(server_id)
            history = self.monitor.history(server_id, hours)
            if not history:
                await self.monitor.sample(server_id)
            image_path = await self.monitor.render(server_id, hours)
            name = server.get("display_name") or server["name"]
            return {"status": "SUCCESS", "text": f"{name} · 最近 {hours} 小时性能记录（玩家延迟与机器人连接延迟分别标注）", "image_path": str(image_path)}
        if action == "order_reconcile":
            if not ctx.get("is_admin"):
                return {"status": "DENIED", "text": "需要管理员权限。"}
            parts = shlex.split(args)
            if len(parts) < 3 or parts[1] not in {"success", "failed"}:
                raise ValueError("用法：订单核对 <订单ID> <success|failed> <核对依据>；请先核实是否交付")
            result = await self.domain.admin("order_resolve", {"order_id": parts[0], "outcome": parts[1], "reason": " ".join(parts[2:]), "scope": ctx["scope"]}, ctx["user_id"])
            return {"status": "SUCCESS", "text": result.get("text", "订单核对已记录。")}
        return await self.domain.execute(action, ctx, args)

    async def _raw_rcon(self, ctx: dict, args: str) -> dict:
        if not ctx.get("is_admin"):
            return {"status": "DENIED", "text": "这条命令需要管理员权限。"}
        message_id = ctx.get("request_id") or ctx.get("message_id")
        if message_id in (None, "", 0, "0") or not str(message_id).strip():
            return {"status": "DENIED", "text": "缺少稳定的消息标识，未执行游戏命令。"}
        # This is intentionally not a template: JSON braces, quotes and literal
        # escapes belong to the console command and must remain untouched.
        command = str(args or "").strip().lstrip("/")
        if not command or any(char in command for char in ("\n", "\r", "\x00")):
            raise ValueError("每次只能执行一条非空 RCON 指令；用法：cmd 游戏命令")
        try:
            byte_count = len(command.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("RCON 指令包含无效的 Unicode 字符") from exc
        if byte_count > 1446:
            raise ValueError("RCON 指令超过 1446 字节")
        action_key = "builtin:rcon"
        run_id = hashlib.sha256(f"{ctx['umo']}|{message_id}|{action_key}".encode()).hexdigest()

        def received(raw):
            return "RECEIVED", clean_text(raw) or "服务器返回了空内容；命令执行结果请在游戏内核对。"

        return await self._run_rcon_once(ctx, run_id, action_key, ctx["server_id"], command, received,
                                         audit_action="rcon")

    async def _custom(self, command_id: str, ctx: dict, args: str) -> dict:
        command = next((c for c in self.settings.data["commands"] if c["id"] == command_id), None)
        if not command or not command.get("enabled", True):
            return {"status": "DISABLED", "text": "该动作已停用。"}
        if command.get("admin_only", True) and not ctx.get("is_admin"):
            return {"status": "DENIED", "text": "这条命令需要管理员权限。"}
        server_id = command.get("server_id") or ctx["server_id"]
        binding = self.store.binding(ctx["user_id"])
        if command.get("require_binding", True) and not binding:
            return {"status": "DENIED", "text": "请先绑定 Minecraft 账号。"}
        if command.get("require_binding", True) and binding["server_id"] != server_id:
            return {"status": "DENIED", "text": "此命令的目标服务器与绑定服务器不同，请检查配置。"}
        player = (binding or {}).get("player_name", (binding or {}).get("name", (binding or {}).get("player", "")))
        values = {"user_id": ctx["user_id"], "player": player, "player_name": player,
                  "points_name": self.settings.data.get("points_name", "点数"),
                  "date": datetime.now(ZoneInfo(self.settings.data["timezone"])).date().isoformat()}
        supplied = shlex.split(args)
        params = command.get("parameters", [])
        if len(supplied) > len(params):
            raise ValueError("参数数量过多")
        for index, param in enumerate(params):
            value = supplied[index] if index < len(supplied) else param.get("default")
            if value is None:
                raise ValueError("缺少参数：" + param["name"])
            kind = param.get("type", "word")
            if kind == "integer":
                value = int(value)
                if not param.get("min", -1000000000) <= value <= param.get("max", 1000000000):
                    raise ValueError("参数超出范围：" + param["name"])
            elif kind == "player":
                value = validate_player(str(value))
            elif kind == "enum":
                if value not in param.get("choices", []):
                    raise ValueError("参数不在允许选项中：" + param["name"])
            elif not re.fullmatch(r"[\w.:/-]{1,128}", str(value)):
                raise ValueError("参数只能包含文字、数字、下划线及 . : / -")
            values[param["name"]] = value
        rendered = render_command(command["rcon"], values)
        # Keep the v1 custom-command key exactly: old events must remain deduped
        # after upgrading to the shared executor.
        message_id = ctx.get("message_id") or uuid.uuid4().hex
        run_id = hashlib.sha256(f"{ctx['umo']}|{message_id}|{command_id}".encode()).hexdigest()

        def render_result(raw):
            parser = command.get("parser", "rules")
            if parser == "litesignin":
                result = parse_signin(raw, player)
            elif parser == "text":
                result = {"status": "SUCCESS", "fields": {}, "cleaned": clean_text(raw, command.get("cleanup_prefixes", []))}
            else:
                result = parse_rules(raw, command.get("rules", []), player or None, command.get("cleanup_prefixes", []))
            status = result["status"]
            fields = {**values, **result.get("fields", {}), "cleaned": result["cleaned"], "rcon": clean_text(raw), "status": status}
            template = command.get("templates", {}).get(status, "服务器结果未识别，请管理员检查解析规则。")
            return status, render_template(template, fields)

        return await self._run_rcon_once(ctx, run_id, command_id, server_id, rendered, render_result,
                                         cooldown_seconds=command.get("cooldown_seconds", 3))

    async def _run_rcon_once(self, ctx: dict, run_id: str, action: str, server_id: str,
                             command: str, render_result, *, cooldown_seconds=0, audit_action=None) -> dict:
        """Persist before sending and cache every terminal result, including unknowns."""
        if len(self._cooldowns) > 1024:
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > time.monotonic()}
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            previous = self._ops.execute("SELECT status,text FROM runs WHERE id=?", (run_id,)).fetchone()
            if previous:
                return {"status": previous["status"], "text": previous["text"] or "该请求正在处理或等待核对，请勿重复提交。"}
            key = (action, ctx["umo"], ctx["user_id"])
            remaining = self._cooldowns.get(key, 0) - time.monotonic()
            if remaining > 0:
                return {"status": "COOLDOWN", "text": f"请等待 {int(remaining) + 1} 秒后再使用。"}
            self._cooldowns[key] = time.monotonic() + cooldown_seconds
            now = time.time()
            with self._ops:
                self._ops.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)", (run_id, ctx["umo"], action, server_id, "SENDING", "", now, now))
                if audit_action:
                    self._ops.execute("INSERT INTO audit(actor,action,detail,created_at) VALUES (?,?,?,?)",
                                      (str(ctx["user_id"]), audit_action, json.dumps({"server_id": server_id, "run_id": run_id}), now))
            try:
                raw = await self.gateway.rcon(server_id, command)
                status, text = render_result(raw)
            except GatewayUnavailable:
                status, text = "FAILED", "服务器未连接，命令未发送。"
            except OutcomeUnknown:
                status, text = "UNKNOWN", "命令已发出但结果未确认，请管理员核对；不会自动重发。"
            except GatewayError:
                status, text = "UNKNOWN", "服务器接口返回错误，命令执行结果待核对；不会自动重发。"
            except asyncio.CancelledError:
                self._finish_run(run_id, "UNKNOWN", "插件重载或停止，命令执行结果待核对。")
                raise
            except Exception as exc:
                log.error("[Yuma MCQQ] 命令处理失败，action=%s (%s)", action, type(exc).__name__)
                status, text = "UNKNOWN", "命令结果处理失败，请管理员核对。"
            self._finish_run(run_id, status, text)
            return {"status": status, "text": text}

    def _finish_run(self, run_id: str, status: str, text: str):
        self._ops.execute("UPDATE runs SET status=?,text=?,updated_at=? WHERE id=?", (status, text, time.time(), run_id))
        self._ops.commit()

    @tracked
    async def web(self, action: str, payload: dict, actor: str) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("请求数据必须是对象")
        # These endpoints are only exposed through AstrBot's authenticated plugin API.
        if action == "settings":
            async with self._settings_lock:
                expected = payload.get("revision")
                if expected is not None and expected != self.revision():
                    raise ValueError("配置已由其他页面修改，请刷新后再编辑")
                patch = payload.get("settings", payload)
                previous = copy.deepcopy(self.settings.data)
                result = self.settings.save(patch)
                try:
                    await self.gateway.reload()
                    await self.monitor.reload()
                    await self.media.reload()
                except BaseException:
                    self.settings.save(previous)
                    if not self._closing:
                        for component in (self.gateway, self.monitor, self.media):
                            try:
                                await component.reload()
                            except Exception as exc:
                                log.error("[Yuma MCQQ] 恢复旧配置连接失败 (%s)", type(exc).__name__)
                    raise
                self._audit(actor, "settings", "配置已更新")
                return {"settings": result, "revision": self.revision()}
        if action == "preview":
            command = payload.get("command", {})
            result = parse_rules(payload.get("raw", ""), command.get("rules", []), payload.get("player") or None, command.get("cleanup_prefixes", []))
            if command.get("parser") == "litesignin":
                result = parse_signin(payload.get("raw", ""), payload.get("player", ""))
            elif command.get("parser") == "text":
                result = {"status": "SUCCESS", "fields": {}, "cleaned": clean_text(payload.get("raw", ""), command.get("cleanup_prefixes", []))}
            values = {"points_name": self.settings.data.get("points_name", "点数"), "player": payload.get("player", ""),
                      **payload.get("values", {}), **result.get("fields", {}),
                      "rcon": clean_text(payload.get("raw", "")), "cleaned": result["cleaned"], "status": result["status"]}
            result["preview"] = render_template(command.get("templates", {}).get(result["status"], "{cleaned}"), values)
            return result
        if action == "metrics":
            sid = payload.get("server_id", "")
            self.settings.server(sid)
            return {"samples": self.monitor.history(sid, max(1, min(168, int(payload.get("hours", 24)))))}
        if action == "sample":
            result = await self.monitor.sample(str(payload["server_id"]))
            return {"sample": result}
        if action == "rcon":
            command = render_command(str(payload.get("command", "")), {})
            server_id = str(payload["server_id"])
            self._audit(actor, "rcon", f"server={server_id}")
            try:
                raw = await self.gateway.rcon(server_id, command)
                return {"status": "RECEIVED", "raw": raw, "cleaned": clean_text(raw)}
            except OutcomeUnknown:
                return {"status": "UNKNOWN", "text": "结果未知，不会自动重试。"}
        result = await self.domain.admin(action, payload, actor)
        self._audit(actor, action, "后台操作完成")
        return result

    def _audit(self, actor: str, action: str, detail: str):
        self._ops.execute("INSERT INTO audit(actor,action,detail,created_at) VALUES (?,?,?,?)", (actor, action, detail, time.time()))
        self._ops.commit()
