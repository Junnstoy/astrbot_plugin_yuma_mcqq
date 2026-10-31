# 模块接口约定

版本：1.2.0，2026-10-31。以 [完整拓扑](ARCHITECTURE.md)、[业务规则](BUSINESS.md) 和当前实现为准。旧插件仅为迁移输入，不决定新插件行为。

| 模块 | 主要接口 | 职责 |
| --- | --- | --- |
| core.config.Settings | save / public / server / route | 校验、原子保存、token掩码、共享scope策略一致性 |
| core.text | clean_text / parse_rules / parse_signin / render_command / render_template | 清洗、限时完整匹配、简单变量模板 |
| core.store.Store | bind_name / begin_binding / complete_binding / unbind / binding / snapshot / preview_legacy / import_legacy / connection及业务事务 | 名称与真实UUID唯一身份、冷却、账本、订单与显式迁移 |
| services.domain.Domain | execute / on_game_event / admin / preview_signin / recover / close | 绑定、签到、纯预览、点数商店 |
| services.gateway.Gateway | start / reload / request / rcon / broadcast / status / close | 正向连接、反向鉴权接入、关联、重连与错误语义 |
| services.media.MediaService | start / reload / public_source_url / publish / publish_record / status / close | 公开源链接校验、只读限时媒体与缓存 |
| services.relay.Relay | from_qq / from_game / close | 富消息、组件渲染、引用、UMO |
| services.monitor.Monitor | start / reload / sample / history / render / status / close | 采样、历史、PNG |
| services.application.Application | start / execute / web / snapshot / close | 组合入口、自定义命令、配置回滚和任务清理 |
| adapters.astrbot_commands | CommandRegistryBridge | 稳定动作ID与管理行为持久化 |
| main.py | AstrBot Star和Plugin API | 权限、AI分流、UMO快照、私聊交付确认、鉴权GUI |

业务上下文：`{umo, user_id, scope, server_id, is_admin, message_id, reply_private?, confirmation_command?}`；回复：`{text, status, image_path?}`。私聊交付回调只有 OneBot 返回真实 message_id 才返回成功。

## 配置 v3

本节保留“配置 v3”标题以兼容已有链接，1.2.0 的当前配置为 `schema_version=4`。`Settings.data` 是校验后的当前配置，`save(patch)` 原子保存，`public()` 遮蔽服务器 token。配置 schema 与仍为 2 的业务数据库身份 schema 分开管理。全局字段默认：`binding_verification_enabled=false`、`points_name="点数"`、`command_prefix=""`。同一 `scope` 的路由必须使用相同签到时区及奖励范围。

启动加载现有配置时，缺少版本字段按 v1 处理，先沿用 v1 → v2 的表情类型迁移；随后对配置 v2 执行一次 v3 迁移：每台服务器中，只有精确等于 `spark tps` 的 `tps_command` 改为 `tps`、精确等于 `spark ping` 的 `ping_command` 改为 `ping`，其他自定义命令不改。完成这一步后主动设置回 Spark，后续加载不再替换。新配置及监控缺省查询统一为 `tps` / `mspt` / `ping`。

v3 → v4 只调整精确匹配的历史官方默认值：路由 `prefix` 从 `[{group}] {sender}：` 改为 `[{group}] {sender}{reply}：`；`templates.signin` 若等于 `签到成功✅️\n本次获得 {points_delta}{points_name}，当前{points_name}：{balance}\n{rcon}`，或 `templates.signin_already` 若等于 `签到失败❌️ 今日已签到，请不要重复签到。`，删除该项覆盖以使用当前三行默认。这里 `\n` 表示真实换行。其余自定义前缀和模板保留；已经是 v4 的配置不会再次执行该转换。迁移结果进入经校验的内存配置，下次保存写入 `schema_version=4`。

服务器沿用稳定 `id` 和 QueQiao 鉴权 `name`，新增 `display_name=""`、`connection_mode="forward"`、`reverse_host="0.0.0.0"`、`reverse_port=8081`、`reverse_path="/queqiao"`。正向使用 `url` 主动连接；反向监听并接受 QueQiao 连接，要求非空 token，按路径、`x-self-name` 与 `Authorization: Bearer ...` 校验。`display_name` 只用于显示，空值回退到 `name`。

路由 `types` 为 QQ → MC 的消息组成白名单，`events` 为 MC → QQ 的事件白名单；GUI 将它们表现为复选框，底层仍是字符串数组，消息类型含表情 `face`。`signin_prefixes` 仅用于规则识别前清洗，`templates.signin` 与 `templates.signin_already` 分别覆盖首次群成功及群当日重复回复。未覆盖均使用 `玩家: {player}\n{group_result}\n{game_result}`。

路由 `prefix` 默认 `[{group}] {sender}{reply}：`，`{reply}` 放置具有悬浮内容的引用组件，无引用时为空；不含此变量的自定义前缀继续在正文展示引用。`templates.reply_hover` 默认 `{sender}\n{message}\nDate: {time}`，仅接受这三个变量。`message_colors` 包含 `link/image/record/video/file/forward/reply/face/at` 九类，前六类默认 `blue`，其余依次为 `gray/green/yellow`。颜色接受 Minecraft 的 16 个标准名称或 `#RRGGBB`；缺项补默认，未知类别或非法颜色被拒绝。名称清单及示例见 [转发配置](COMMANDS.md#转发类型与游戏事件)。

## 身份与签到结果

`Store.binding(qq_id)` 返回 `null` 或：

```text
{qq_id, identity, mc_uuid, player, server_id, verified, version, updated, cooldown_until}
```

免验证绑定使用 `identity=name:<casefold玩家名>`、`mc_uuid=null`、`verified=false`；真实验证使用 `identity=uuid:<真实UUID>`、非空 `mc_uuid`、`verified=true`。QQ、忽略大小写的玩家名和非空 UUID 各自唯一。业务快照比较 `identity + version`，兼容旧快照中的真实 UUID，不因两个未验证 UUID 都为空而判为同一绑定。

`bind_name` 在事务中直接绑定；`begin_binding/complete_binding` 只用于普通用户验证码模式。同名未验证身份升级不重置 QQ/名称冷却，但保留目标 UUID 的冷却检查。原插件自身的绑定表升级至身份 schema v2，与显式导入旧积分插件数据库、配置升级至 v4 是三个不同流程。

管理员命令支持 `绑定 QQ号 MC名`、`修改绑定 QQ号 MC名`、`解绑 QQ号`；省略目标 QQ 则为管理员本人操作。通过权限校验后统一调用 `Domain._admin_binding`，由 `bind_name(..., force=True)` 或 `unbind(..., force=True)` 跳过本次冷却检查并直接办理，不生成游戏验证码。QQ/玩家名/已知 UUID 的唯一约束、已有其他绑定必须显式修改的规则仍生效。成功操作作废目标 QQ 的旧验证码；实际变更继续写入绑定历史、审计与冷却，普通用户后续操作不会因管理覆盖而免 CD。同名同服重复确认也取消待验证请求；只有待验证请求时管理员解绑可取消请求并记录冷却。

管理员单参数纯数字按缺少 MC 名处理；管理员绑定数字玩家名须显式传 QQ 和 MC 名两个参数。普通成员的单参数数字仍是合法 MC 玩家名，不能使用代操作语法。

签到操作只有游戏 `SIGNED/ALREADY_SIGNED` 才能进入群签到事务。结果包含 `group_status`、`game_status`、`points_delta`、`balance`、`day`、`game_continuous_days`、`rcon`、`points_name`；展示字段新增 `group_result`、`game_result` 和 `days`（连续天数别名），兼容原有身份与状态文本字段。两个结果独立组成四种组合：群重复也保留本次游戏成功及天数，不取旧日签到记录的游戏结果替代。游戏重复没有连续天数时留空，旧成功结果缺少天数时只显示成功，不虚构数值。完整默认文案见 [两阶段签到](BUSINESS.md#两阶段签到)。

`rcon` 经通用颜色/控制码清洗但保留插件前缀；解析专用前缀不影响这个展示字段。当前操作的游戏状态、回包、天数及货币名随已确认操作保存；同消息重放和恢复使用该操作的数据，恢复本地结算不再次执行游戏命令。未知或失败的游戏回包不结算群奖励，也不伪装为群或游戏已签到。

## 命令与查询边界

内置稳定动作共 23 个。`status` 只返回连接在线/离线；`tps/mspt/ping` 执行当前服务器配置的对应查询，默认分别为 `tps` / `mspt` / `ping`，返回清洗后的实际回包文本，不接收时间范围；`performance` 返回历史 PNG，默认触发名“性能图”。后台采样继续独立运行。Paper TPS 的三个窗口可解析；普通 `ping` 若只返回 `Pong!`、错误提示或不含可识别的毫秒值，监控保存 `null` 与不可用原因，不伪造数值或拿 SLP RTT 替代，QQ 文本查询仍回显清洗原文。

管理员 `cmd` 将后续正文作为一条原样 RCON 命令发送到当前群路由服务器，无需绑定；JSON 等字面量不经过模板插值，响应使用通用清洗后的实际回包。同一 UMO/消息 ID/action 不重复发送，结果未知时不自动重试，详见 [cmd 使用说明](COMMANDS.md#管理员原样-rconcmd)。

独立 `command_prefix` 只包装本插件的 AstrBot CommandFilter，不改变稳定处理器 ID、管理行为覆盖或其他插件。空前缀沿用框架唤醒，非空前缀要求匹配配置值再解析生效命令。自定义回复上下文含 `points_name` 和完整清洗 `rcon`；`cleaned` 仍是当前解析器使用的去前缀文本。

`MediaService.public_source_url(source)` 校验并返回公开 HTTP(S) 源链接，不下载也不要求缓存服务启用。`publish` / `publish_record` 仍要求可用的缓存服务；引用与 QQ 表情可用文字悬浮展示，不依赖 ChatImage。

## GUI 与持久化

GUI 使用 AstrBot Plugin Views SDK 调用 GET state、POST settings、POST action、GET metrics。配置提交为 `{revision, settings}`，动作提交为 `{action, payload}`。均要求框架已认证用户名，不另开匿名管理服务。后台动作见GUI表单、各模块与文档。GUI 的 `binding_unbind` 通过同一个 `Domain._admin_binding` 管理路径处理，无需前端另传 `force`，与群内管理员解绑共享免 CD、验证码作废、冷却保留和审计规则。

`preview` 动作只处理自定义指令样本文本。签到专用 `preview_signin` 动作接受 `{raw, player, route_umo?, group_signed?, points_delta?, balance?}`，通过已配置 UMO 选择当前路由；未选择时用默认规则和模板，指定不存在的路由会被拒绝。两个数值是模拟奖励及最终余额，范围为 0–10^12，默认均为 1。

同步纯函数 `Domain.preview_signin(raw, player, route, group_signed=False, points_delta=1, balance=1)` 返回 `{status, fields, cleaned, preview, data}`；`status` 为识别的游戏状态，`preview` 为两阶段回复，`data` 为模拟结果和模板变量。群重复把模拟奖励置零，识别失败时群状态为 `NOT_SETTLED`。两个预览均不读取真实账户、不写数据库、不执行 RCON；`rcon` 动作才会调用真实服务器。前端应明确显示此区别。状态快照不包含兑换码、私聊奖励或明文 token。

分页动作 `dashboard_page` 接受 `{table, page, page_size, scope?}`，只允许 bindings/balances/ledger/signins/runs/audit 六类。每页 10/20/50/100；返回 `{rows,total,page,page_size,pages}`，空表保持第 1/1 页，越界钳位。余额、流水、签到先按 scope 过滤；全局绑定、运行及审计不伪造作用域归属。

服务器 ID 动作 `server_rename` 接受 `{server_id,new_server_id,revision}`，与主入口配置保存锁串行，关联数据同步迁移。维护门禁阻止新的业务，`state.maintenance` 供 GUI 提示。数据目录内 `server-rename.json` 记录 ID、配置摘要和操作 ID，不保存 token；逐库完成标记支持失败重试/启动恢复。禁止用普通配置增删绕过有关联数据的 ID 修改。

`state.help_commands` 为 GUI 提供完整可编辑帮助元数据，包含稳定 action_id、当前名称/别名、默认及实际说明、显隐/排序和权限提示。配置 `help` 与命令执行启停独立；引用标签配置为 `route.templates.reply_label`，仅支持 `{sender}`。

## 显式旧数据迁移

`Store.preview_legacy(path, scope_map=None, *, binding_server_id="", server_map=None, routes=None, binding_choices=None)` 返回源文件 `digest`、绑定计划 `plan_digest`、`binding_plan` 与统计；`Store.import_legacy` 另接收 `expected_digest`、`actor` 和 `plan_digest`。后台 `import_preview/import_apply` 接受路径、`scope_map` 及这三项绑定选项；`routes` 由 Domain 取当前已校验配置，不能用客户端载荷替换。

`binding_server_id` 指定默认目标服，`server_map` 为来源账号或会话提供服映射；映射值必须是已配置服务器。`binding_choices` 是来源 `account_key` 列表：省略表示选全部，空数组表示只迁账本并归档旧绑定。计划可以从当前路由推导唯一目标服，不能确定时要求管理员补映射。选中的 QQ、玩家名或现有绑定冲突会阻止导入，不自动覆盖；同名同服已有绑定保留真实 UUID、验证状态、版本和冷却。

`import_apply` 始终要求 `confirmed=true` 和匹配的源文件 `digest`；有选中绑定时还必须提供匹配的 `plan_digest`。计划摘要涵盖映射、选择、已完成来源项与相关目标身份状态，变更后必须重新预览。新 GUI 始终提交 `digest + plan_digest + confirmed`；旧账本专用调用使用 `binding_choices=[]` 时可不提交计划摘要，但若提供就必须匹配。

已迁入同一源文件的账本可以单独补迁绑定，不重复增加余额或历史记录；结果中的 `already_imported=true` 不表示本次没有新增绑定，应同时查看 `bindings_added`。来源名称绑定建立 `verified=false`、无 UUID 的名称身份。已完成来源键持久化，因此用户后来主动解绑或修改后，重复导入不会复活原绑定。旧奖励快照和已用码继续按原迁移约定保留，不自动重发外部交付。备份、一致性校验和冲突选择步骤见 [迁移说明](MIGRATION.md)。

## 运行与持久化边界

连接错误分三类：GatewayUnavailable 明确未发送；OutcomeUnknown 可能已执行，禁止自动重发或退款；GatewayError 为其他接口错误。RCON 无法证明未执行的错误转为 OutcomeUnknown。

数据保存到 AstrBot 为插件分配的数据目录。SQLite事务只做本地写入，网络请求位于事务外；持久操作ID与状态处理不确定性。框架模块以外不导入AstrBot，核心测试可独立运行。
