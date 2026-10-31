# 模块接口约定

版本：1.1.1，2026-10-31。以 [完整拓扑](ARCHITECTURE.md)、[业务规则](BUSINESS.md) 和当前实现为准。旧插件仅为迁移输入，不决定新插件行为。

| 模块 | 主要接口 | 职责 |
| --- | --- | --- |
| core.config.Settings | save / public / server / route | 校验、原子保存、token掩码、共享scope策略一致性 |
| core.text | clean_text / parse_rules / parse_signin / render_command / render_template | 清洗、限时完整匹配、简单变量模板 |
| core.store.Store | bind_name / begin_binding / complete_binding / unbind / binding / snapshot / connection及业务事务 | 名称与真实UUID唯一身份、冷却、账本、订单与显式迁移 |
| services.domain.Domain | execute / on_game_event / admin / recover / close | 绑定、签到、点数商店 |
| services.gateway.Gateway | start / reload / request / rcon / broadcast / status / close | 正向连接、反向鉴权接入、关联、重连与错误语义 |
| services.media.MediaService | start / reload / public_source_url / publish / publish_record / status / close | 公开源链接校验、只读限时媒体与缓存 |
| services.relay.Relay | from_qq / from_game / close | 富消息、组件渲染、引用、UMO |
| services.monitor.Monitor | start / reload / sample / history / render / status / close | 采样、历史、PNG |
| services.application.Application | start / execute / web / snapshot / close | 组合入口、自定义命令、配置回滚和任务清理 |
| adapters.astrbot_commands | CommandRegistryBridge | 稳定动作ID与管理行为持久化 |
| main.py | AstrBot Star和Plugin API | 权限、AI分流、UMO快照、私聊交付确认、鉴权GUI |

业务上下文：`{umo, user_id, scope, server_id, is_admin, message_id, reply_private?, confirmation_command?}`；回复：`{text, status, image_path?}`。私聊交付回调只有 OneBot 返回真实 message_id 才返回成功。

## 配置 v3

`Settings.data` 是校验后的当前配置，`save(patch)` 原子保存，`public()` 遮蔽服务器 token。配置 `schema_version=3`，与仍为 2 的业务数据库身份 schema 分开管理。全局字段默认：`binding_verification_enabled=false`、`points_name="点数"`、`command_prefix=""`。同一 `scope` 的路由必须使用相同签到时区及奖励范围。

启动加载现有配置时，缺少版本字段按 v1 处理，先沿用 v1 → v2 的表情类型迁移；随后对配置 v2 执行一次 v3 迁移：每台服务器中，只有精确等于 `spark tps` 的 `tps_command` 改为 `tps`、精确等于 `spark ping` 的 `ping_command` 改为 `ping`，其他自定义命令不改。迁移结果进入经校验的内存配置，下次保存写入 `schema_version=3`。已经保存为 v3 后主动设置回 Spark，后续加载不再替换。新配置及监控缺省查询统一为 `tps` / `mspt` / `ping`。

服务器沿用稳定 `id` 和 QueQiao 鉴权 `name`，新增 `display_name=""`、`connection_mode="forward"`、`reverse_host="0.0.0.0"`、`reverse_port=8081`、`reverse_path="/queqiao"`。正向使用 `url` 主动连接；反向监听并接受 QueQiao 连接，要求非空 token，按路径、`x-self-name` 与 `Authorization: Bearer ...` 校验。`display_name` 只用于显示，空值回退到 `name`。

路由 `types` 为 QQ → MC 的消息组成白名单，`events` 为 MC → QQ 的事件白名单；GUI 将它们表现为复选框，底层仍是字符串数组。新增表情类型 `face`。`signin_prefixes` 仅用于规则识别前清洗，`templates.signin` 与 `templates.signin_already` 分别覆盖首次群成功及群当日重复回复。未覆盖使用默认模板。

## 身份与签到结果

`Store.binding(qq_id)` 返回 `null` 或：

```text
{qq_id, identity, mc_uuid, player, server_id, verified, version, updated, cooldown_until}
```

免验证绑定使用 `identity=name:<casefold玩家名>`、`mc_uuid=null`、`verified=false`；真实验证使用 `identity=uuid:<真实UUID>`、非空 `mc_uuid`、`verified=true`。QQ、忽略大小写的玩家名和非空 UUID 各自唯一。业务快照比较 `identity + version`，兼容旧快照中的真实 UUID，不因两个未验证 UUID 都为空而判为同一绑定。

`bind_name` 在事务中直接绑定；`begin_binding/complete_binding` 只用于普通用户验证码模式。同名未验证身份升级不重置 QQ/名称冷却，但保留目标 UUID 的冷却检查。原插件自身的绑定表升级至身份 schema v2，与显式导入旧积分插件数据库、配置升级至 v3 是三个不同流程。

管理员命令支持 `绑定 QQ号 MC名`、`修改绑定 QQ号 MC名`、`解绑 QQ号`；省略目标 QQ 则为管理员本人操作。通过权限校验后统一调用 `Domain._admin_binding`，由 `bind_name(..., force=True)` 或 `unbind(..., force=True)` 跳过本次冷却检查并直接办理，不生成游戏验证码。QQ/玩家名/已知 UUID 的唯一约束、已有其他绑定必须显式修改的规则仍生效。成功操作作废目标 QQ 的旧验证码；实际变更继续写入绑定历史、审计与冷却，普通用户后续操作不会因管理覆盖而免 CD。同名同服重复确认也取消待验证请求；只有待验证请求时管理员解绑可取消请求并记录冷却。

签到操作只有游戏 `SIGNED/ALREADY_SIGNED` 才能进入群签到事务。结果包含 `group_status`、`game_status`、`points_delta`、`balance`、`day`、`game_continuous_days`、`rcon`、`points_name`。`rcon` 经通用颜色/控制码清洗但保留插件前缀；解析专用前缀不影响这个展示字段。回包及当次货币名随已确认操作保存，恢复本地结算不再次执行游戏命令。

## 命令与查询边界

内置稳定动作共 23 个。`status` 只返回连接在线/离线；`tps/mspt/ping` 执行当前服务器配置的对应查询，默认分别为 `tps` / `mspt` / `ping`，返回清洗后的实际回包文本，不接收时间范围；`performance` 返回历史 PNG，默认触发名“性能图”。后台采样继续独立运行。Paper TPS 的三个窗口可解析；普通 `ping` 若只返回 `Pong!`、错误提示或不含可识别的毫秒值，监控保存 `null` 与不可用原因，不伪造数值或拿 SLP RTT 替代，QQ 文本查询仍回显清洗原文。

管理员 `cmd` 将后续正文作为一条原样 RCON 命令发送到当前群路由服务器，无需绑定；JSON 等字面量不经过模板插值，响应使用通用清洗后的实际回包。同一 UMO/消息 ID/action 不重复发送，结果未知时不自动重试，详见 [cmd 使用说明](COMMANDS.md#管理员原样-rconcmd)。

独立 `command_prefix` 只包装本插件的 AstrBot CommandFilter，不改变稳定处理器 ID、管理行为覆盖或其他插件。空前缀沿用框架唤醒，非空前缀要求匹配配置值再解析生效命令。自定义回复上下文含 `points_name` 和完整清洗 `rcon`；`cleaned` 仍是当前解析器使用的去前缀文本。

`MediaService.public_source_url(source)` 校验并返回公开 HTTP(S) 源链接，不下载也不要求缓存服务启用。`publish` / `publish_record` 仍要求可用的缓存服务；引用与 QQ 表情可用文字悬浮展示，不依赖 ChatImage。

## GUI 与持久化

GUI 使用 AstrBot Plugin Views SDK 调用 GET state、POST settings、POST action、GET metrics。配置提交为 `{revision, settings}`，动作提交为 `{action, payload}`。均要求框架已认证用户名，不另开匿名管理服务。后台动作见GUI表单、各模块与文档。GUI 的 `binding_unbind` 通过同一个 `Domain._admin_binding` 管理路径处理，无需前端另传 `force`，与群内管理员解绑共享免 CD、验证码作废、冷却保留和审计规则。

`preview` 动作只处理载荷中的样本文本，不执行 RCON、不写余额；`rcon` 动作才会调用真实服务器。前端应明确显示此区别。状态快照不包含兑换码、私聊奖励或明文 token。

连接错误分三类：GatewayUnavailable 明确未发送；OutcomeUnknown 可能已执行，禁止自动重发或退款；GatewayError 为其他接口错误。RCON 无法证明未执行的错误转为 OutcomeUnknown。

数据保存到 AstrBot 为插件分配的数据目录。SQLite事务只做本地写入，网络请求位于事务外；持久操作ID与状态处理不确定性。框架模块以外不导入AstrBot，核心测试可独立运行。
