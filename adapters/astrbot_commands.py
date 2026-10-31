"""AstrBot 4.29 command registry integration.

Only this module touches AstrBot's internal command registry. Business modules
do not import AstrBot. Stable handler names preserve management overrides when
an action is renamed, reordered, or the plugin is reloaded.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
from types import SimpleNamespace


BUILTINS = (
    ("signin", "签到", "先完成游戏签到，再结算本群点数签到。"),
    ("binding", "绑定", "绑定 MC 账号：绑定 玩家名。"),
    ("binding_status", "我的绑定", "查看当前绑定和换绑冷却。"),
    ("rebind", "修改绑定", "验证并修改绑定：修改绑定 新玩家名。"),
    ("unbind", "解绑", "解除绑定，保留绑定历史和冷却。"),
    ("balance", "点数", "查看本群可用点数与冻结点数。"),
    ("rank", "点数排行", "查看本群点数排行榜。"),
    ("shop", "点数商店", "浏览本群点数商店。"),
    ("purchase", "兑换", "兑换一件商品：兑换 商品ID。"),
    ("confirm", "确认兑换", "确认待处理的兑换：确认兑换 订单ID。"),
    ("reclaim", "补领", "重新发送本人已分配的奖励，不重复扣点。"),
    ("orders", "兑换记录", "查看本人的兑换订单。"),
    ("status", "服务器状态", "查看服务器连接和性能状态。"),
    ("tps", "tps", "查看服务器 TPS 历史图。"),
    ("mspt", "mspt", "查看服务器 MSPT 历史图。"),
    ("ping", "ping", "查看已标明测量对象的延迟。"),
    ("help", "mc帮助", "查看本插件帮助；触发名称可在管理行为中修改。"),
    ("points_add", "添加点数", "管理员添加点数：添加点数 QQ号 数量 [原因]。"),
    ("points_sub", "扣除点数", "管理员扣除点数：扣除点数 QQ号 数量 [原因]。"),
    ("points_set", "设置点数", "管理员设置点数：设置点数 QQ号 数量 [原因]。"),
    ("order_reconcile", "订单核对", "管理员核对订单：订单核对 订单ID success或failed 核对依据。"),
)

ADMIN_ACTIONS = frozenset({"points_add", "points_sub", "points_set", "order_reconcile"})


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
                metadata.event_filters.append(command_filter)
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
