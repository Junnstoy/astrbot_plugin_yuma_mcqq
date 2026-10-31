# 模块接口约定

版本：1.0.0，2026-10-31。以 [完整拓扑](ARCHITECTURE.md)、[业务规则](BUSINESS.md) 和当前实现为准。旧插件仅为迁移输入，不决定新插件行为。

| 模块 | 主要接口 | 职责 |
| --- | --- | --- |
| core.config.Settings | save / public / server / route | 校验、原子保存、token掩码、共享scope策略一致性 |
| core.text | clean_text / parse_rules / parse_signin / render_command / render_template | 清洗、限时完整匹配、简单变量模板 |
| core.store.Store | binding / snapshot / connection及业务事务 | 唯一身份、冷却、账本、订单与显式迁移 |
| services.domain.Domain | execute / on_game_event / admin / recover / close | 绑定、签到、点数商店 |
| services.gateway.Gateway | start / reload / request / rcon / broadcast / status / close | QueQiao鉴权、关联、重连与错误语义 |
| services.media.MediaService | start / reload / publish / publish_record / status / close | 只读限时媒体与缓存 |
| services.relay.Relay | from_qq / from_game / close | 富消息、组件渲染、引用、UMO |
| services.monitor.Monitor | start / reload / sample / history / render / status / close | 采样、历史、PNG |
| services.application.Application | start / execute / web / snapshot / close | 组合入口、自定义命令、配置回滚和任务清理 |
| adapters.astrbot_commands | CommandRegistryBridge | 稳定动作ID与管理行为持久化 |
| main.py | AstrBot Star和Plugin API | 权限、AI分流、UMO快照、私聊交付确认、鉴权GUI |

业务上下文：`{umo, user_id, scope, server_id, is_admin, message_id, reply_private?, confirmation_command?}`；回复：`{text, status, image_path?}`。私聊交付回调只有 OneBot 返回真实 message_id 才返回成功。

GUI 使用 AstrBot Plugin Views SDK 调用 GET state、POST settings、POST action、GET metrics。配置提交为 `{revision, settings}`，动作提交为 `{action, payload}`。均要求框架已认证用户名，不另开匿名管理服务。后台动作见GUI表单、各模块与文档。

连接错误分三类：GatewayUnavailable 明确未发送；OutcomeUnknown 可能已执行，禁止自动重发或退款；GatewayError 为其他接口错误。RCON 无法证明未执行的错误转为 OutcomeUnknown。

数据保存到 AstrBot 为插件分配的数据目录。SQLite事务只做本地写入，网络请求位于事务外；持久操作ID与状态处理不确定性。框架模块以外不导入AstrBot，核心测试可独立运行。
