"""Native AstrBot commands with session-specific prefixes.

The subclass preserves the original CommandFilter state so Dashboard renames,
aliases, permissions and disabled flags remain owned by AstrBot.
"""
from __future__ import annotations

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import star_handlers_registry

from .core import CHECKIN_COMMANDS


class SessionCommandFilter(CommandFilter):
    def get_complete_command_names(self) -> list[str]:
        selected = getattr(self, "_dispatch_command_name", None)
        if selected is not None:
            return [selected]
        # Native parsing checks aliases in order; a short alias must never
        # consume the beginning of a longer effective command name.
        return sorted(super().get_complete_command_names(), key=lambda name: (-len(name), name != self.command_name, name))

    def command_names(self) -> list[str]:
        """Current effective names, including aliases configured in Dashboard."""
        return list(self.get_complete_command_names())

    def _native_spelling(self, text: str) -> str:
        """Allow ASCII command spelling case-insensitively, preserving args."""
        for name in sorted(self.command_names(), key=len, reverse=True):
            if name.isascii() and (text.casefold() == name.casefold()
                                   or text.casefold().startswith(name.casefold() + " ")):
                return name + text[len(name):]
        return text

    def _exact_name(self, text: str) -> bool:
        return self._native_spelling(text) in self.command_names()

    def filter(self, event, cfg) -> bool:
        # Registry normally excludes disabled handlers. Keep direct filter calls
        # safe too; never make a second route around native management flags.
        if not self.handler_md.enabled or not self._plugin.config.get("enabled", True):
            return False
        session = event.unified_msg_origin
        settings = self._plugin._command_settings.get(session, {})
        text = " ".join(self._plugin.command_text(event).split())
        woken = bool(event.is_at_or_wake_command)
        active = None
        bare_confirmation = self._canonical == "兑换" and self._exact_name(text)
        if bare_confirmation:
            # This narrowly matched confirmation is the only normal path that
            # reads SQLite synchronously: unrelated chat uses the settings cache.
            active = self._plugin.store.active_purchase(session, str(event.get_sender_id()))
            if not active:
                return False
        if not woken and not bare_confirmation:
            return False

        prefix_key = "checkin_prefix" if self._canonical in CHECKIN_COMMANDS else "command_prefix"
        prefix = str(settings.get(prefix_key, "")).strip()
        candidate = text
        # A pending order's exact confirmation name remains the only prefix
        # bypass. Every other command uses its own group's configured prefix,
        # even when its effective name starts with that same word.
        if bare_confirmation:
            pass
        elif prefix and text.startswith(prefix + " "):
            candidate = text[len(prefix):].strip()
        elif prefix:
            return False
        candidate = self._native_spelling(candidate)

        # Reject argument tails before native dispatch. This also keeps a
        # disabled longer command (such as "签到 排行榜") from falling back
        # to its shorter parent command and generating a response.
        no_args = {"签到", "点数", "商店列表", "签到排行榜", "排行榜", "兑换", "补领", "帮助"}
        if self._canonical in no_args and not self._exact_name(candidate):
            return False

        if not settings.get("enabled", False):
            if self._canonical not in {"兑换", "补领"} or not self._exact_name(candidate):
                return False
            if active is None:
                active = self._plugin.store.active_purchase(session, str(event.get_sender_id()))
            if not active or active["status"] not in {"paid", "delivery_failed", "sending"}:
                return False

        selected = next((name for name in self.command_names()
                         if candidate == name or candidate.startswith(name + " ")), None)
        if selected is None:
            return False
        original_text, original_wake = event.message_str, event.is_at_or_wake_command
        original_selection = getattr(self, "_dispatch_command_name", None)
        try:
            event.message_str = candidate
            event.is_at_or_wake_command = woken or bare_confirmation
            # The host currently keeps checking aliases after consuming a
            # matching name. Expose only that longest match during parsing so
            # an argument equal to a second alias is not consumed as well.
            self._dispatch_command_name = selected
            # Keep native parameter parsing and custom command filters. The
            # waking stage consumes parsed_params after this method returns.
            return super().filter(event, cfg)
        finally:
            event.message_str = original_text
            event.is_at_or_wake_command = original_wake
            self._dispatch_command_name = original_selection


def install_command_filters(plugin, operations: dict[str, str]) -> dict[str, SessionCommandFilter]:
    """Wrap only this plugin's native registered commands, preserving metadata."""
    result = {}
    for handler in star_handlers_registry.get_handlers_by_module_name(plugin.__class__.__module__):
        canonical = operations.get(handler.handler_name)
        if canonical is None:
            continue
        for index, original in enumerate(handler.event_filters):
            if not isinstance(original, CommandFilter):
                continue
            if isinstance(original, SessionCommandFilter):
                wrapped = original
            else:
                wrapped = SessionCommandFilter.__new__(SessionCommandFilter)
                # Keep object-valued state (aliases, metadata and custom filters)
                # rather than resetting names or parameter definitions.
                wrapped.__dict__.update(original.__dict__)
                handler.event_filters[index] = wrapped
            wrapped._plugin = plugin
            wrapped._canonical = canonical
            result[canonical] = wrapped
    return result
