"""AstrBot 4.29 command registry integration.

Only this module touches AstrBot's internal command registry. Business modules
do not import AstrBot. Stable handler names preserve management overrides when
an action is renamed, reordered, or the plugin is reloaded.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import re
from types import SimpleNamespace


BUILTINS = (
    ("signin", "签到", "先完成游戏签到，再结算本群点数签到。"),
    ("binding", "绑定", "绑定 MC 账号：绑定 玩家名；管理员可用：绑定 QQ号 MC名称（免冷却）。"),
    ("binding_status", "我的绑定", "查看当前绑定和换绑冷却。"),
    ("rebind", "修改绑定", "验证并修改绑定：修改绑定 新玩家名；管理员可用：修改绑定 QQ号 MC名称（免冷却）。"),
    ("unbind", "解绑", "解除绑定，保留绑定历史和冷却；管理员可用：解绑 QQ号（免冷却）。"),
    ("balance", "点数", "查看本群可用点数与冻结点数。"),
    ("rank", "点数排行", "查看本群点数排行榜。"),
    ("shop", "点数商店", "浏览本群点数商店。"),
    ("purchase", "兑换", "兑换一件商品：兑换 商品ID。"),
    ("confirm", "确认兑换", "确认待处理的兑换：确认兑换 订单ID。"),
    ("reclaim", "补领", "重新发送本人已分配的奖励，不重复扣点。"),
    ("orders", "兑换记录", "查看本人的兑换订单。"),
    ("status", "服务器状态", "仅返回服务器在线或离线。"),
    ("tps", "tps", "执行 TPS 查询并返回清洗后的 RCON 文字。"),
    ("mspt", "mspt", "执行 MSPT 查询并返回清洗后的 RCON 文字。"),
    ("ping", "ping", "执行玩家延迟查询并返回清洗后的 RCON 文字。"),
    ("performance", "性能图", "查看 TPS、MSPT 与延迟的历史 PNG 图片。"),
    ("rcon", "cmd", "管理员直接执行服务器命令：cmd 游戏命令。"),
    ("help", "mc帮助", "查看本插件帮助；触发名称可在管理行为中修改。"),
    ("points_add", "添加点数", "管理员添加点数：添加点数 QQ号 数量 [原因]。"),
    ("points_sub", "扣除点数", "管理员扣除点数：扣除点数 QQ号 数量 [原因]。"),
    ("points_set", "设置点数", "管理员设置点数：设置点数 QQ号 数量 [原因]。"),
    ("order_reconcile", "订单核对", "管理员核对订单：订单核对 订单ID success或failed 核对依据。"),
)

ADMIN_ACTIONS = frozenset({"points_add", "points_sub", "points_set", "order_reconcile", "rcon"})
_PREFIX_FILTER_TYPE = None


def with_plugin_prefix(original, prefix_getter):
    """Keep a real CommandFilter so AstrBot still manages this exact action.

    AstrBot filters run synchronously. Temporarily adapting only this filter's
    input supports a plugin wake prefix without changing another handler's text,
    wake state, permissions, or global bot configuration.
    """
    global _PREFIX_FILTER_TYPE
    if _PREFIX_FILTER_TYPE is None:
        base = _framework().CommandFilter

        class PluginCommandFilter(base):
            def _match(self, event, cfg):
                matched = super().filter(event, cfg)
                if matched and self.handler_md.handler_name == "cmd_rcon":
                    # Framework GreedyStr first collapses every whitespace run,
                    # including whitespace inside JSON strings. Only the raw
                    # console action must retain the original argument body.
                    # Keep framework matching and permissions; replace only its
                    # parsed args after a successful match against a live name.
                    text = event.get_message_str().lstrip()
                    for name in sorted(self.get_complete_command_names(), key=len, reverse=True):
                        pattern = r"\s+".join(re.escape(part) for part in name.split())
                        match = re.match(pattern + r"(?:\s+|$)", text)
                        if match:
                            event.set_extra("parsed_params", {"args": text[match.end():]})
                            break
                return matched

            def filter(self, event, cfg):
                prefix = str(self.prefix_getter() or "").strip()
                if not prefix:
                    return self._match(event, cfg)
                text = event.get_message_str().strip()
                if not text.startswith(prefix):
                    # The framework may already have stripped a global wake
                    # prefix which is also part (or all) of this plugin prefix.
                    original = str(getattr(event.message_obj, "message_str", "") or "").strip()
                    if not event.is_at_or_wake_command or not original.startswith(prefix):
                        return False
                    text = original
                text = text[len(prefix):].lstrip()
                previous_text = event.message_str
                previous_wake = event.is_at_or_wake_command
                try:
                    event.message_str = text
                    event.is_at_or_wake_command = True
                    return self._match(event, cfg)
                finally:
                    event.message_str = previous_text
                    event.is_at_or_wake_command = previous_wake

        _PREFIX_FILTER_TYPE = PluginCommandFilter
    adapted = copy.copy(original)
    adapted.__class__ = _PREFIX_FILTER_TYPE
    adapted.prefix_getter = prefix_getter
    return adapted


def _framework() -> SimpleNamespace:
    """Load the supported framework APIs lazily for core-only test runs."""
    from astrbot.api import sp
    from astrbot.core.star.command_management import list_commands, sync_command_configs
    from astrbot.core.star.filter.command import CommandFilter, GreedyStr
    from astrbot.core.star.filter.command_group import CommandGroupFilter
    from astrbot.core.star.filter.permission import COMMAND_PERMISSION_TYPES, PermissionTypeFilter
    from astrbot.core.star.register.star_handler import register_command
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, star_handlers_registry

    return SimpleNamespace(
        sp=sp, list_commands=list_commands, sync_command_configs=sync_command_configs,
        CommandFilter=CommandFilter, CommandGroupFilter=CommandGroupFilter, GreedyStr=GreedyStr,
        permissions=COMMAND_PERMISSION_TYPES, PermissionTypeFilter=PermissionTypeFilter,
        register_command=register_command, star_map=star_map,
        EventType=EventType, Metadata=StarHandlerMetadata, registry=star_handlers_registry,
    )


def _handler(action: str, module: str, name: str, description: str):
    async def invoke(plugin, event, args):
        await plugin._execute_action(action, event, args)

    # CommandFilter inspects the *unbound* signature and skips self and event.
    # Concrete annotations avoid eval_str resolving a factory's local scope.
    invoke.__name__ = name
    invoke.__qualname__ = name
    invoke.__module__ = module
    invoke.__doc__ = description
    invoke.__annotations__ = {"args": _framework().GreedyStr}
    return invoke


def install_builtin_commands(plugin_class: type) -> type:
    """Register each built-in independently during module import."""
    framework = _framework()
    for action, name, description in BUILTINS:
        method_name = "cmd_" + action
        raw = _handler(action, plugin_class.__module__, method_name, description)
        registered = framework.register_command(name, priority=20)(raw)
        setattr(plugin_class, method_name, registered)
    return plugin_class


class CommandRegistryBridge:
    """Bind runtime custom actions into the real framework registry.

    Rename/alias/entry-enabled overrides remain owned by AstrBot. The plugin's
    action.enabled remains a business availability switch checked by Application.
    """

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.module = plugin.__class__.__module__
        self._owned: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self._signature: tuple | None = None
        # Built-ins were registered during module import. Adapt their filter
        # objects while preserving stable IDs and any persisted overrides.
        framework = _framework()
        for metadata in framework.registry.get_handlers_by_module_name(self.module):
            metadata.event_filters = [
                with_plugin_prefix(item, self._prefix) if isinstance(item, framework.CommandFilter) else item
                for item in metadata.event_filters
            ]

    def _prefix(self) -> str:
        app = getattr(self.plugin, "app", None)
        return str(app.settings.data.get("command_prefix", "")) if app is not None else ""

    def handler_name(self, action_id: str) -> str:
        digest = hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:24]
        return "cmd_custom_" + digest

    def full_name(self, action_id: str) -> str:
        return self.module + "_" + self.handler_name(action_id)

    def _validate(self, definitions: list[dict]) -> None:
        if not isinstance(definitions, list) or any(not isinstance(item, dict) for item in definitions):
            raise ValueError("自定义指令必须是对象列表。")
        ids: set[str] = set()
        triggers: set[str] = set()
        framework = _framework()
        owned_prefix = self.module + "_cmd_custom_"
        for metadata in framework.registry:
            if metadata.handler_full_name.startswith(owned_prefix):
                continue
            for command_filter in metadata.event_filters:
                if isinstance(command_filter, (framework.CommandFilter, framework.CommandGroupFilter)):
                    triggers.update(command_filter.get_complete_command_names())
        for definition in definitions:
            action_id = str(definition.get("id", "")).strip()
            if not action_id or action_id in ids:
                raise ValueError("自定义指令 ID 不能为空或重复。")
            ids.add(action_id)
            if not isinstance(definition.get("aliases", []), list):
                raise ValueError("指令别名必须是列表。")
            names = [definition.get("name", ""), *definition.get("aliases", [])]
            existing = framework.registry.get_handler_by_full_name(self.full_name(action_id))
            if existing:
                # Management overrides survive changing the default definition;
                # their live names must also be reserved during GUI validation.
                for current_filter in existing.event_filters:
                    if not isinstance(current_filter, framework.CommandFilter):
                        continue
                    if current_filter.command_name != current_filter._original_command_name:
                        names.append(current_filter.command_name)
                    original_aliases = set(existing.extras_configs.get("yuma_default_aliases", []))
                    if current_filter.alias != original_aliases:
                        names.extend(current_filter.alias)
            local: set[str] = set()
            for raw in names:
                name = str(raw).strip()
                if not name or len(name) > 64 or any(c in name for c in "\r\n\t"):
                    raise ValueError("指令名称或别名无效。")
                if name in local:
                    continue
                if name in triggers:
                    raise ValueError(f"指令触发名称冲突：{name}")
                local.add(name)
            triggers.update(local)

    def validate(self, definitions: list[dict]) -> None:
        self._validate(definitions)

    async def reconcile(self, definitions: list[dict]) -> None:
        """Replace registrations as a complete set before syncing overrides."""
        async with self._lock:
            self._validate(definitions)
            signature = tuple(
                (str(d.get("id")), str(d.get("name")), tuple(d.get("aliases", [])),
                 str(d.get("description", "")))
                for d in definitions
            )
            if signature == self._signature:
                return
            framework = _framework()
            plugin_metadata = framework.star_map.get(self.module)
            if plugin_metadata is None:
                raise RuntimeError("AstrBot 未注册本插件元数据，无法注册自定义指令。")
            stored_permissions = await framework.sp.global_get("alter_cmd", {})
            plugin_permissions = stored_permissions.get(plugin_metadata.name, {})
            replacements = {}
            for definition in definitions:
                action_id = str(definition["id"])
                method_name = self.handler_name(action_id)
                raw = _handler(
                    "custom:" + action_id, self.module, method_name,
                    str(definition.get("description", "自定义 Minecraft 指令")),
                )
                metadata = framework.Metadata(
                    event_type=framework.EventType.AdapterMessageEvent,
                    handler_full_name=self.full_name(action_id),
                    handler_name=method_name,
                    handler_module_path=self.module,
                    handler=raw,
                    event_filters=[],
                    desc=raw.__doc__ or "",
                    extras_configs={"priority": 20, "yuma_default_aliases": list(definition.get("aliases", []))},
                )
                command_filter = framework.CommandFilter(
                    str(definition["name"]), set(definition.get("aliases", [])), metadata,
                )
                metadata.event_filters.append(with_plugin_prefix(command_filter, self._prefix))
                permission_name = plugin_permissions.get(method_name, {}).get("permission")
                if permission_name in framework.permissions:
                    metadata.event_filters.append(framework.PermissionTypeFilter(
                        framework.permissions[permission_name],
                    ))
                # initialize() runs after AstrBot's automatic method binding.
                metadata.handler = functools.partial(raw, self.plugin)
                replacements[metadata.handler_full_name] = metadata

            previous = dict(self._owned)
            previous_names = list(plugin_metadata.star_handler_full_names)
            for name, metadata in previous.items():
                if framework.registry.get_handler_by_full_name(name) is metadata:
                    framework.registry.remove(metadata)
            for name, metadata in replacements.items():
                stale = framework.registry.get_handler_by_full_name(name)
                if stale is not None:
                    framework.registry.remove(stale)
                framework.registry.append(metadata)
            plugin_metadata.star_handler_full_names = [
                name for name in previous_names if name not in previous
            ]
            plugin_metadata.star_handler_full_names.extend(
                name for name in replacements if name not in plugin_metadata.star_handler_full_names
            )
            try:
                # Never sync while the registry is temporarily empty: the core
                # would delete persisted rename/alias records as stale.
                await framework.sync_command_configs()
            except BaseException:
                for metadata in replacements.values():
                    framework.registry.remove(metadata)
                for metadata in previous.values():
                    framework.registry.append(metadata)
                plugin_metadata.star_handler_full_names = previous_names
                raise
            self._owned = replacements
            self._signature = signature

    async def effective_commands(self) -> list[dict]:
        records = await _framework().list_commands()
        return [record for record in records if record.get("module_path") == self.module]

    def close(self) -> None:
        """Remove only this instance's handlers; don't delete persisted overrides."""
        framework = _framework()
        metadata = framework.star_map.get(self.module)
        removed = set()
        for name, handler in self._owned.items():
            if framework.registry.get_handler_by_full_name(name) is handler:
                framework.registry.remove(handler)
                removed.add(name)
        if metadata is not None:
            metadata.star_handler_full_names = [
                name for name in metadata.star_handler_full_names if name not in removed
            ]
        self._owned.clear()
        self._signature = None

    async def shutdown(self) -> None:
        """Wait for registration transactions before unloading this instance."""
        async with self._lock:
            self.close()
