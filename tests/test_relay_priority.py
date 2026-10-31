"""Exercise early relay ordering with AstrBot's actual handler dispatcher."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    from . import test_astrbot_integration as fixture
except ImportError:
    import test_astrbot_integration as fixture

if fixture.HAS_ASTRBOT:
    # Import the parent stage first, as framework bootstrap does, to avoid its
    # circular import with method.star_request.
    from astrbot.core.pipeline.process_stage.stage import StarRequestSubStage
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.star.session_plugin_manager import SessionPluginManager
    from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, StarHandlerRegistry
    from astrbot.core.message.components import At, Reply
    from astrbot.core.platform.message_type import MessageType


class DispatchEvent(fixture.FakeEvent):
    """The dispatcher additionally clears each handler's previous result."""
    plugins_name = None

    def clear_result(self):
        self._result = None

    def get_messages(self):
        return self.message_obj.message

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE

    def is_private_chat(self):
        return False


@unittest.skipUnless(fixture.HAS_ASTRBOT, "requires AstrBot 4.29 runtime")
class RelayPriorityTests(unittest.IsolatedAsyncioTestCase):
    # Reuse fixture lifecycle, not its test class: inheriting it would run all
    # unrelated integration tests a second time during unittest discovery.
    @classmethod
    def setUpClass(cls):
        fixture.MainIntegrationTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.MainIntegrationTests.tearDownClass.__func__(cls)

    async def asyncSetUp(self):
        await fixture.MainIntegrationTests.asyncSetUp(self)
        self.configure_active(False)

    async def asyncTearDown(self):
        await fixture.MainIntegrationTests.asyncTearDown(self)

    def configure_active(self, enabled, whitelist=None):
        self.plugin.context.get_config = Mock(return_value={
            "provider_ltm_settings": {
                "group_icl_enable": True,
                "image_caption": True,
                "image_caption_provider_id": "synthetic-caption-provider",
                "active_reply": {"enable": enabled, "whitelist": whitelist or []},
            },
        })

    def configure_chat(self, global_prefixes=None, provider_prefix="星乃", *, provider_enabled=True):
        self.configure_active(True)
        config = self.plugin.context.get_config.return_value
        config.update({
            "wake_prefix": ["#"] if global_prefixes is None else global_prefixes,
            "provider_settings": {"wake_prefix": provider_prefix, "enable": provider_enabled},
            "platform_settings": {}, "admins_id": [], "plugin_set": ["*"],
        })
        return config

    async def wake_image(self, text="", *, leading=None):
        """Run genuine framework waking/command filters; only persistence is isolated."""
        event = DispatchEvent(text)
        event.is_wake = False
        event.message_obj.raw_message = {}
        event.message_obj.message_str = text
        event.message_obj.message = ([leading] if leading else [])
        if text:
            event.message_obj.message.append(self.main.Plain(text))
        event.message_obj.message.append(self.main.Image.fromURL("https://example.com/synthetic.png"))
        registry = StarHandlerRegistry()
        for metadata in fixture.star_handlers_registry.get_handlers_by_module_name(self.main.__name__):
            if metadata.event_type == EventType.AdapterMessageEvent:
                registry.append(replace(metadata, handler=getattr(self.plugin, metadata.handler_name)))
        stage = WakingCheckStage()
        await stage.initialize(types.SimpleNamespace(
            astrbot_config=self.plugin.context.get_config.return_value,
            db_helper=None, astrbot_config_id="isolated-wake-prefix-tests", plugin_manager=None,
        ))
        with patch.object(fixture.star_handlers_registry, "get_handlers_by_event_type", return_value=list(registry)), \
                patch.object(SessionPluginManager, "filter_handlers_by_session",
                             AsyncMock(side_effect=lambda current, handlers: handlers)):
            await stage.process(event)
        self.assertFalse(event.is_stopped())
        return event

    def bound_handler(self, name):
        metadata = next(item for item in fixture.star_handlers_registry
                        if item.handler_module_path == self.main.__name__ and item.handler_name == name)
        return replace(metadata, handler=getattr(self.plugin, name))

    def listener(self, name, callback, priority=0):
        return StarHandlerMetadata(
            event_type=EventType.AdapterMessageEvent,
            handler_full_name=self.main.__name__ + "_" + name,
            handler_name=name, handler_module_path=self.main.__name__,
            handler=callback, event_filters=[], extras_configs={"priority": priority},
        )

    async def dispatch(self, event, *listeners):
        registry = StarHandlerRegistry()
        # Intentionally insert out of order; sorting is the real framework's.
        registry.append(self.bound_handler("ordinary_message"))
        for listener in listeners:
            registry.append(listener)
        registry.append(self.bound_handler("ordinary_message_early"))
        event.set_extra("activated_handlers", list(registry))
        async for _ in StarRequestSubStage().process(event):
            pass

    async def test_registered_command_priority_precedes_early_context_and_late_handlers(self):
        handlers = fixture.star_handlers_registry.get_handlers_by_module_name(self.main.__name__)
        commands = [handler for handler in handlers if handler.handler_name.startswith("cmd_")]
        self.assertEqual(len(commands), 23)
        self.assertTrue(all(handler.extras_configs["priority"] == 20 for handler in commands))
        early = self.bound_handler("ordinary_message_early")
        late = self.bound_handler("ordinary_message")
        self.assertEqual((early.extras_configs["priority"], late.extras_configs["priority"]), (10, -1000))
        context = self.listener("context_caption", AsyncMock())
        registry = StarHandlerRegistry()
        for handler in (late, context, early, commands[0]):
            registry.append(handler)
        self.assertEqual([handler.extras_configs["priority"] for handler in registry], [20, 10, 0, -1000])

    async def test_image_reaches_relay_before_caption_finishes_and_context_still_runs(self):
        caption_entered, caption_finish = asyncio.Event(), asyncio.Event()
        order = []
        event = DispatchEvent("ordinary image")
        event.message_obj.message = [self.main.Image.fromURL("https://example.com/synthetic.png")]
        original_components = list(event.message_obj.message)

        async def relay(current, route):
            self.assertIs(current, event)
            self.assertIs(route, self.route)
            order.append("relay")

        async def caption(current):
            order.append("caption_started")
            caption_entered.set()
            await caption_finish.wait()
            current.set_extra("synthetic_caption_recorded", True)
            order.append("caption_finished")

        self.plugin.app.relay_from_qq.side_effect = relay
        task = asyncio.create_task(self.dispatch(event, self.listener("context_caption", caption)))
        try:
            await asyncio.wait_for(caption_entered.wait(), 2)
            self.assertEqual(order, ["relay", "caption_started"])
            self.assertFalse(task.done())
            self.plugin.app.relay_from_qq.assert_awaited_once_with(event, self.route)
            caption_finish.set()
            await asyncio.wait_for(task, 2)
        finally:
            caption_finish.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(order, ["relay", "caption_started", "caption_finished"])
        self.assertTrue(event.get_extra("synthetic_caption_recorded"))
        self.assertEqual(event.message_obj.message, original_components)
        self.assertFalse(event.is_stopped())
        self.assertFalse(event.is_at_or_wake_command)
        self.assertFalse(event.call_llm)
        self.assertFalse(event.get_extra(self.main.CONSUMED_KEY, False))
        self.plugin.context.send_message.assert_not_awaited()
        self.plugin.context.get_config.assert_not_called()

    async def test_real_registered_command_consumes_event_before_any_relay(self):
        event = DispatchEvent("签到", ai=True)
        command = self.bound_handler("cmd_signin")
        event.set_extra("handlers_parsed_params", {command.handler_full_name: {"args": ""}})
        caption = AsyncMock()
        await self.dispatch(event, command, self.listener("context_caption", caption))
        self.assertTrue(event.is_stopped())
        self.assertTrue(event.get_extra(self.main.CONSUMED_KEY))
        self.plugin.app.execute.assert_awaited_once()
        self.plugin.app.relay_from_qq.assert_not_awaited()
        caption.assert_not_awaited()

    async def test_matching_other_plugin_command_with_empty_arguments_never_leaks(self):
        event = DispatchEvent("other-plugin-command")
        event.set_extra("handlers_parsed_params", {"other.plugin_cmd": {}})
        await self.plugin.ordinary_message_early(event)
        await self.plugin.ordinary_message(event)
        self.plugin.app.relay_from_qq.assert_not_awaited()
        self.assertFalse(event.is_stopped())

    async def test_wake_claim_sent_and_consumed_signals_block_both_paths(self):
        cases = [
            ("wake", lambda event: setattr(event, "is_at_or_wake_command", True)),
            ("llm", lambda event: setattr(event, "call_llm", True)),
            ("sent", lambda event: setattr(event, "_has_send_oper", True)),
            ("stopped", lambda event: event.stop_event()),
            ("consumed", lambda event: event.set_extra(self.main.CONSUMED_KEY, True)),
        ] + [(key, lambda event, key=key: event.set_extra(key, object())) for key in self.main.AI_TAKEOVER_KEYS]
        for name, modify in cases:
            with self.subTest(signal=name):
                event = DispatchEvent(name)
                modify(event)
                await self.plugin.ordinary_message_early(event)
                await self.plugin.ordinary_message(event)
        self.plugin.app.relay_from_qq.assert_not_awaited()

    async def test_active_reply_config_never_preemptively_delays_ordinary_messages(self):
        cases = [
            (False, []),
            (False, ["bot:GroupMessage:12345"]),
            (True, []),
            (True, ["bot:GroupMessage:12345"]),
            (True, ["12345"]),
            (True, ["another:GroupMessage:12345"]),
            (True, ["67890"]),
        ]
        for enabled, whitelist in cases:
            with self.subTest(enabled=enabled, whitelist=whitelist):
                self.configure_active(enabled, whitelist)
                self.plugin.app.relay_from_qq.reset_mock()
                event = DispatchEvent()
                await self.plugin.ordinary_message_early(event)
                self.plugin.app.relay_from_qq.assert_awaited_once_with(event, self.route)
                event.set_extra("provider_request", object())
                await self.plugin.ordinary_message(event)
                self.plugin.app.relay_from_qq.assert_awaited_once_with(event, self.route)

    async def test_active_reply_and_caption_continue_after_relay_without_second_broadcast(self):
        self.configure_active(True)
        for claims_ai in (False, True):
            with self.subTest(claims_ai=claims_ai):
                self.plugin.app.relay_from_qq.reset_mock()
                entered, release = asyncio.Event(), asyncio.Event()
                event = DispatchEvent()
                event.message_obj.message = [self.main.Image.fromURL("https://example.com/synthetic.png")]

                async def context(current):
                    self.plugin.app.relay_from_qq.assert_awaited_once_with(current, self.route)
                    entered.set()
                    await release.wait()
                    if claims_ai:
                        current.set_extra("provider_request", object())

                task = asyncio.create_task(self.dispatch(event, self.listener("active_context", context)))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    self.plugin.app.relay_from_qq.assert_awaited_once_with(event, self.route)
                    release.set()
                    await asyncio.wait_for(task, 2)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.plugin.app.relay_from_qq.assert_awaited_once_with(event, self.route)
                self.assertEqual(bool(event.get_extra("provider_request")), claims_ai)
                self.assertFalse(event.is_stopped())

    async def test_missing_optional_active_config_allows_early_relay(self):
        self.plugin.context.get_config = Mock(return_value={"wake_prefix": ["/"]})
        event = DispatchEvent()
        await self.plugin.ordinary_message_early(event)
        await self.plugin.ordinary_message(event)
        self.plugin.app.relay_from_qq.assert_awaited_once_with(event, self.route)

    async def test_invalid_config_protects_woken_messages_but_does_not_hold_ordinary_messages(self):
        for config in (None, {"provider_settings": None},
                       {"provider_settings": {"wake_prefix": []}},
                       {"wake_prefix": "#", "provider_settings": {"wake_prefix": "星乃"}},
                       {"wake_prefix": [1], "provider_settings": {"wake_prefix": "星乃"}},
                       RuntimeError("config unavailable")):
            with self.subTest(config=config):
                self.plugin.context.get_config = (Mock(side_effect=config) if isinstance(config, Exception)
                                                  else Mock(return_value=config))
                self.plugin.app.relay_from_qq.reset_mock()
                event = DispatchEvent("星乃 图片", ai=True)
                await self.plugin.ordinary_message_early(event)
                await self.plugin.ordinary_message(event)
                self.plugin.app.relay_from_qq.assert_not_awaited()
                ordinary = DispatchEvent("ordinary image")
                await self.plugin.ordinary_message_early(ordinary)
                await self.plugin.ordinary_message(ordinary)
                self.plugin.app.relay_from_qq.assert_awaited_once_with(ordinary, self.route)

    async def test_real_waking_distinguishes_llm_prefix_from_general_wake(self):
        cases = [
            ("#星乃 看图片", "星乃 看图片", True, False),
            ("", "", False, True),
            ("普通文字和图片", "普通文字和图片", False, True),
            ("#其他文字", "其他文字", True, True),
            ("星乃 看图片", "星乃 看图片", False, True),
        ]
        for original, processed, general_wake, should_relay in cases:
            with self.subTest(text=original):
                self.configure_chat()
                self.plugin.app.relay_from_qq.reset_mock()
                event = await self.wake_image(original)
                self.assertEqual(event.message_str, processed)
                self.assertEqual(event.is_at_or_wake_command, general_wake)
                await self.plugin.ordinary_message_early(event)
                await self.plugin.ordinary_message(event)
                self.assertEqual(self.plugin.app.relay_from_qq.await_count, int(should_relay))
                # The text component retains the original # for actual relay.
                self.assertEqual(event.message_obj.message_str, original)
                if original:
                    self.assertEqual(event.message_obj.message[0].text, original)
                self.assertFalse(event.is_stopped())
                if general_wake:
                    self.plugin.context.get_config.assert_called_with(event.unified_msg_origin)

    async def test_real_waking_and_provider_prefix_normalization_follow_current_configuration(self):
        cases = [
            (["!"], "小凛", "!小凛 看图", False),
            (["!"], "小凛", "!星乃 看图", True),
            (["#"], "#星乃", "#星乃 看图", False),
            (["#", "!"], "#星乃", "!星乃 看图", False),
            # Official normalization processes every configured prefix in order.
            (["#", "星"], "#星乃", "#乃 看图", False),
        ]
        for global_prefixes, provider_prefix, text, should_relay in cases:
            with self.subTest(global_prefixes=global_prefixes, provider_prefix=provider_prefix, text=text):
                self.configure_chat(global_prefixes, provider_prefix)
                self.plugin.app.relay_from_qq.reset_mock()
                event = await self.wake_image(text)
                await self.plugin.ordinary_message_early(event)
                self.assertEqual(self.plugin.app.relay_from_qq.await_count, int(should_relay))

    async def test_real_bot_mentions_and_replies_still_require_configured_llm_prefix(self):
        for leading in (At(qq="99999"), Reply(id="123", sender_id="99999")):
            for text, should_relay in (("星乃 看图片", False), ("普通图片", True), ("", True)):
                with self.subTest(leading=type(leading).__name__, text=text):
                    self.configure_chat()
                    self.plugin.app.relay_from_qq.reset_mock()
                    event = await self.wake_image(text, leading=leading)
                    self.assertTrue(event.is_at_or_wake_command)
                    await self.plugin.ordinary_message_early(event)
                    self.assertEqual(self.plugin.app.relay_from_qq.await_count, int(should_relay))

    async def test_real_empty_prefix_settings_match_framework_waking_semantics(self):
        cases = [
            (["#"], "", "#其他", None, False),
            (["#"], "", "", None, True),
            (["#"], "", "", At(qq="99999"), False),
            ([""], "星乃", "星乃 图片", None, False),
            ([""], "星乃", "普通图片", None, True),
            ([""], "", "", None, False),
            ([], "星乃", "#星乃 图片", None, True),
        ]
        for global_prefixes, provider_prefix, text, leading, should_relay in cases:
            with self.subTest(global_prefixes=global_prefixes, provider_prefix=provider_prefix, text=text, leading=leading):
                self.configure_chat(global_prefixes, provider_prefix)
                self.plugin.app.relay_from_qq.reset_mock()
                event = await self.wake_image(text, leading=leading)
                await self.plugin.ordinary_message_early(event)
                self.assertEqual(self.plugin.app.relay_from_qq.await_count, int(should_relay))

    async def test_explicit_ai_intent_is_protected_even_when_provider_is_disabled(self):
        self.configure_chat(provider_enabled=False)
        event = await self.wake_image("#星乃 私人问题")
        await self.plugin.ordinary_message_early(event)
        await self.plugin.ordinary_message(event)
        self.plugin.app.relay_from_qq.assert_not_awaited()

    async def test_webchat_exemption_and_lark_leading_space_match_provider_gate(self):
        for platform, text, should_relay in (("webchat", "其他文字", False),
                                            ("lark", "  星乃 图片", False),
                                            ("aiocqhttp", "  星乃 图片", True)):
            with self.subTest(platform=platform):
                self.configure_chat()
                self.plugin.app.relay_from_qq.reset_mock()
                event = DispatchEvent(text, ai=True)
                event.get_platform_name = lambda: platform
                await self.plugin.ordinary_message_early(event)
                self.assertEqual(self.plugin.app.relay_from_qq.await_count, int(should_relay))

    async def test_unrelated_low_priority_listener_runs_without_blocking_early_relay(self):
        order = []
        self.plugin.app.relay_from_qq.side_effect = lambda *_: order.append("relay")

        async def observer(event):
            order.append("observer")

        event = DispatchEvent()
        await self.dispatch(event, self.listener("ordinary_observer", observer, priority=-1))
        self.assertEqual(order, ["relay", "observer"])
        self.assertFalse(event.is_stopped())
        self.plugin.app.relay_from_qq.assert_awaited_once()

    async def test_higher_priority_ai_claim_blocks_early_and_late(self):
        async def claim(event):
            event.set_extra("ai_claimed", True)

        event = DispatchEvent()
        await self.dispatch(event, self.listener("third_party_ai", claim, priority=15))
        self.plugin.app.relay_from_qq.assert_not_awaited()

    async def test_repeated_and_concurrent_entry_attempts_only_one_broadcast(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def relay(*_):
            entered.set()
            await release.wait()

        self.plugin.app.relay_from_qq.side_effect = relay
        event = DispatchEvent()
        task = asyncio.create_task(self.plugin.ordinary_message_early(event))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await self.plugin.ordinary_message_early(event)
            await self.plugin.ordinary_message(event)
            release.set()
            await asyncio.wait_for(task, 2)
            await self.plugin.ordinary_message(event)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.plugin.app.relay_from_qq.assert_awaited_once_with(event, self.route)

    async def test_failed_early_broadcast_is_not_retried_or_consumed(self):
        self.plugin.app.relay_from_qq.side_effect = RuntimeError("uncertain delivery")
        event = DispatchEvent()
        with patch.object(self.main, "logger", Mock()) as logger:
            await self.plugin.ordinary_message_early(event)
            await self.plugin.ordinary_message(event)
            logger.error.assert_called_once()
        self.plugin.app.relay_from_qq.assert_awaited_once_with(event, self.route)
        self.assertFalse(event.is_stopped())
        self.plugin.context.send_message.assert_not_awaited()

    async def test_cancelled_early_broadcast_is_not_retried_by_late_handler(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def relay(*_):
            entered.set()
            await release.wait()

        self.plugin.app.relay_from_qq.side_effect = relay
        event = DispatchEvent()
        task = asyncio.create_task(self.plugin.ordinary_message_early(event))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.plugin.ordinary_message(event)
            self.plugin.app.relay_from_qq.assert_awaited_once_with(event, self.route)
            self.assertFalse(event.is_stopped())
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
