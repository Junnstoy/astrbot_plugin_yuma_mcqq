"""AstrBot entry points for the Yuma MCQQ plugin."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, json_response, request

from .adapters.astrbot_commands import (
    ADMIN_ACTIONS,
    CommandRegistryBridge,
    install_builtin_commands,
)
from .services.application import Application


PLUGIN_NAME = "astrbot_plugin_yuma_mcqq"
CONSUMED_KEY = "yuma_mcqq.consumed"
AI_TAKEOVER_KEYS = ("yuma_mcqq.ai_claimed", "ai_claimed", "provider_request")


@install_builtin_commands
@register(
    PLUGIN_NAME,
    "Junnstoy",
    "Paper / QueQiao 群服互通、绑定、签到点数商店与性能监控",
    "1.1.1",
    "https://github.com/Junnstoy/astrbot_plugin_yuma_mcqq",
)
class YumaMCQQPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.app = Application(self.data_dir, dict(config or {}))
        self.commands = CommandRegistryBridge(self)
        self._ready = False
        self._closing = False
        self._shutdown_task: asyncio.Task | None = None
        self._settings_lock = asyncio.Lock()
        self._register_web_api()

    async def initialize(self) -> None:
        try:
            await self.app.start(self._send_game_message)
            await self.commands.reconcile(self.app.settings.data.get("commands", []))
            self._ready = True
        except BaseException:
            self.commands.close()
            self._unregister_web_api()
            await self.app.close()
            raise

    async def terminate(self) -> None:
        if self._shutdown_task is None:
            self._closing = True
            self._ready = False
            self._unregister_web_api()
            self._shutdown_task = asyncio.create_task(self._shutdown(), name="yuma-mcqq-shutdown")
        # Cancelling the framework's unloading caller must not strand open
        # databases or tasks; subsequent calls wait for the same cleanup.
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        # A save may still be reconciling handlers after its application call
        # returned. Drain that complete transaction before closing the app.
        async with self._settings_lock:
            await self.commands.shutdown()
            await self.app.close()

    def _unregister_web_api(self) -> None:
        self.context.registered_web_apis[:] = [
            api for api in self.context.registered_web_apis
            if getattr(api[1], "__self__", None) is not self
        ]

    def _enabled(self) -> bool:
        return self._ready and not self._closing and bool(self.app.settings.data.get("enabled", True))

    def _context_for_event(self, event: AstrMessageEvent) -> dict:
        umo = str(event.unified_msg_origin)
        route = self.app.settings.route(umo)
        user_id = str(event.get_sender_id())
        admins = {str(value) for value in (route or {}).get("admins", [])}
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        bot = getattr(event, "bot", None)
        platform_name = event.get_platform_name()

        async def reply_private(target_user_id: str, text: str) -> bool:
            # Retain the original OneBot client, not a fabricated private UMO.
            if platform_name != "aiocqhttp" or bot is None:
                return False
            target = str(target_user_id)
            if not target.isdecimal():
                return False
            try:
                result = await bot.call_action(
                    "send_private_msg", user_id=int(target),
                    message=[{"type": "text", "data": {"text": str(text)}}],
                )
                if not isinstance(result, dict):
                    return False
                if result.get("status") == "failed" or result.get("retcode", 0) != 0:
                    return False
                payload = result.get("data", result)
                return isinstance(payload, dict) and payload.get("message_id") not in (None, "", 0, "0")
            except Exception:
                logger.warning("[%s] OneBot 私聊交付未确认。", PLUGIN_NAME)
                return False

        return {
            "umo": umo,
            "user_id": user_id,
            "scope": str((route or {}).get("scope") or umo),
            "server_id": str((route or {}).get("server_id", "")),
            "is_admin": bool(event.is_admin() or user_id in admins),
            "message_id": message_id,
            "group_id": str(event.get_group_id()),
            "platform_id": str(event.get_platform_id()),
            "reply_private": reply_private,
        }

    async def _execute_action(self, action: str, event: AstrMessageEvent, args: str) -> None:
        event.set_extra(CONSUMED_KEY, True)
        event.stop_event()
        ctx = self._context_for_event(event)
        if not self._enabled():
            await self._send_result(ctx["umo"], {"text": "MCQQ 插件尚未就绪或已关闭。"})
            return
        if action in ADMIN_ACTIONS and not ctx["is_admin"]:
            await self._send_result(ctx["umo"], {"text": "此操作需要管理员权限。"})
            return
        try:
            effective = await self.commands.effective_commands()
            confirmation = next((entry for entry in effective
                                 if entry.get("handler_name") == "cmd_confirm"), None)
            ctx["confirmation_command"] = self._command_hint(
                str((confirmation or {}).get("effective_command") or "确认兑换"), ctx["umo"],
            )
            if action == "help":
                result = {"text": self._help_text(ctx, effective), "status": "ok"}
            else:
                result = await self.app.execute(action, ctx, str(args or ""))
        except (ValueError, PermissionError) as exc:
            result = {"text": str(exc), "status": "error"}
        except Exception as exc:
            logger.error("[%s] 指令处理失败：%s (%s)", PLUGIN_NAME, action, type(exc).__name__)
            result = {"text": "本次操作未能完成，请查看插件运行记录。", "status": "error"}
        # Delivery failure must never replay the point mutation or RCON action.
        await self._send_result(ctx["umo"], result)

    def _command_hint(self, name: str, umo: str) -> str:
        plugin_prefix = str(self.app.settings.data.get("command_prefix", "") or "").strip()
        if plugin_prefix:
            return plugin_prefix + " " + name
        prefixes = self.context.get_config(umo).get("wake_prefix", [])
        if isinstance(prefixes, list) and prefixes:
            return str(prefixes[0]) + name
        # Without a text wake prefix the group user must mention the bot.
        return "@机器人 " + name

    def _help_text(self, ctx: dict, effective: list[dict]) -> str:
        disabled_custom = {
            self.commands.handler_name(str(item["id"]))
            for item in self.app.settings.data.get("commands", [])
            if not item.get("enabled", True) or (item.get("admin_only", True) and not ctx["is_admin"])
        }
        lines = ["MCQQ 指令（以当前管理行为配置为准）"]
        for entry in effective:
            handler = str(entry.get("handler_name", ""))
            if not entry.get("enabled", True) or handler in disabled_custom:
                continue
            if not ctx["is_admin"] and (
                handler.removeprefix("cmd_") in ADMIN_ACTIONS
                or str(entry.get("permission", "")).lower() == "admin"
            ):
                continue
            name = str(entry.get("effective_command") or "")
            if not name:
                continue
            description = str(entry.get("description") or "").strip()
            original = str(entry.get("original_command") or "")
            if original:
                description = description.replace(original, name)
            lines.append(self._command_hint(name, ctx["umo"]) + (" — " + description if description else ""))
        return "\n".join(lines)

    async def _send_result(self, umo: str, result: dict) -> bool:
        parts = []
        text = str(result.get("text", ""))
        if text:
            parts.append(Plain(text))
        image_path = result.get("image_path")
        if image_path:
            candidate = Path(str(image_path)).resolve()
            # Only files produced in this plugin's own data directory may be
            # sent as images. A configurable template cannot exfiltrate files.
            try:
                candidate.relative_to(self.data_dir.resolve())
            except ValueError:
                logger.warning("[%s] 拒绝发送数据目录以外的图片。", PLUGIN_NAME)
            else:
                if candidate.is_file():
                    parts.append(Image.fromFileSystem(str(candidate)))
        if not parts:
            return True
        try:
            found = await self.context.send_message(umo, MessageChain(chain=parts))
            if not found:
                logger.warning("[%s] 找不到 UMO 对应的平台，回复未发送。", PLUGIN_NAME)
            return bool(found)
        except Exception as exc:
            logger.error("[%s] UMO 回复发送失败；业务操作不会重复执行。(%s)", PLUGIN_NAME, type(exc).__name__)
            return False

    async def _send_game_message(self, umo: str, text: str) -> None:
        await self._send_result(umo, {"text": text})

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1000)
    async def ordinary_message(self, event: AstrMessageEvent) -> None:
        if not self._enabled() or event.is_stopped() or event.get_extra(CONSUMED_KEY, False):
            return
        if not event.get_group_id() or str(event.get_sender_id()) == str(event.get_self_id()):
            return
        # is_wake is also set by generic listeners and is not an AI signal.
        if event.is_at_or_wake_command or event.call_llm or getattr(event, "_has_send_oper", False):
            return
        if any(event.get_extra(key, False) for key in AI_TAKEOVER_KEYS):
            return
        route = self.app.settings.route(str(event.unified_msg_origin))
        if not route or not route.get("enabled", True) or not route.get("qq_to_mc", True):
            return
        try:
            await self.app.relay.from_qq(event, route)
        except Exception as exc:
            # Relay faults do not manufacture bot replies or consume AI events.
            logger.error("[%s] 普通群消息转发失败。(%s)", PLUGIN_NAME, type(exc).__name__)

    def _register_web_api(self) -> None:
        for suffix, handler, methods, description in (
            ("state", self.api_state, ["GET"], "插件状态与脱敏配置"),
            ("settings", self.api_settings, ["POST"], "保存插件配置"),
            ("action", self.api_action, ["POST"], "管理员业务操作"),
            ("metrics", self.api_metrics, ["GET"], "性能历史"),
        ):
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{suffix}", handler, methods, description,
            )

    def _web_actor(self) -> str:
        # AstrBot authenticates extension requests and supplies this identity.
        # Requiring it also protects accidental future anonymous registration.
        actor = request.username
        if not actor:
            raise PermissionError("需要登录 AstrBot 管理面板。")
        if not self._ready or self._closing:
            raise RuntimeError("插件尚未就绪。")
        return "dashboard:" + str(actor)

    async def api_state(self):
        try:
            self._web_actor()
            result = self.app.snapshot()
            result["effective_commands"] = await self.commands.effective_commands()
            return json_response(result)
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except RuntimeError as exc:
            return error_response(str(exc), status_code=503)

    async def api_settings(self):
        try:
            actor = self._web_actor()
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise ValueError("配置必须是对象。")
            async with self._settings_lock:
                self._web_actor()
                patch = payload.get("settings", payload)
                if not isinstance(patch, dict):
                    raise ValueError("settings 必须是对象。")
                if "commands" in patch:
                    self.commands.validate(patch["commands"])
                previous = copy.deepcopy(self.app.settings.data)
                try:
                    result = await self.app.web("settings", payload, actor)
                    await self.commands.reconcile(self.app.settings.data.get("commands", []))
                except BaseException:
                    if self.app.settings.data != previous:
                        await self._restore_settings(previous, actor)
                    raise
            return json_response(result)
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.error("[%s] 保存配置失败。(%s)", PLUGIN_NAME, type(exc).__name__)
            return error_response("保存配置失败，请查看插件日志。", status_code=500)

    async def _restore_settings(self, previous: dict, actor: str) -> None:
        try:
            await self.app.web("settings", {"settings": previous}, actor)
        except BaseException:
            # Shutdown rejects new app operations. The file still has to match
            # the rolled-back registry; connections will be closed or reloaded.
            self.app.settings.save(previous)
            logger.warning("[%s] 配置已回退；运行组件恢复未完成，请重载插件。", PLUGIN_NAME)

    async def api_action(self):
        try:
            actor = self._web_actor()
            body = await request.json(default={})
            if not isinstance(body, dict) or not isinstance(body.get("action"), str):
                raise ValueError("需要 action 与 payload。")
            payload = body.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError("payload 必须是对象。")
            if body["action"] == "settings":
                raise ValueError("请通过 settings 接口保存配置。")
            result = await self.app.web(body["action"], payload, actor)
            return json_response(result)
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.error("[%s] 管理操作失败。(%s)", PLUGIN_NAME, type(exc).__name__)
            return error_response("操作失败，请查看插件日志。", status_code=500)

    async def api_metrics(self):
        try:
            actor = self._web_actor()
            payload = {
                "server_id": request.query.get("server_id", ""),
                "hours": request.query.get("hours", "24"),
            }
            return json_response(await self.app.web("metrics", payload, actor))
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except RuntimeError as exc:
            return error_response(str(exc), status_code=503)
