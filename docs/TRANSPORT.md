# 传输、富消息与性能采样

本文对应 1.4.2 版本。模块按新设计独立实现，旧插件仅用于显式数据迁移。协议目标为 QueQiao V2、NapCat/OneBot 和 Paper；已经使用模拟协议与真实 AstrBot 消息类测试，**尚未连接真实 QQ/Paper 服务进行联调**。

## QueQiao 连接与命令结果

每台服务器独立选择 `servers[].connection_mode`。`name` 是协议身份，必须匹配 QueQiao 的 `server_name`；`display_name` 仅用于 QQ 消息和性能图说明，留空回退到 `name`。群显示名称由 `routes[].display_name` 控制，不改变 UMO、服务器身份或点数作用域。

| 模式 | 发起连接的一侧 | 插件配置 |
| --- | --- | --- |
| `forward` 正向 | AstrBot 插件连接 QueQiao WebSocket Server | `url`、`name`、与 QueQiao 一致的 `token` |
| `reverse` 反向 | QueQiao WebSocket Client 连接 AstrBot 插件 | `reverse_host`、`reverse_port`、`reverse_path`、`name`、必填 `token` |

正向握手携带 `x-self-name`、配置了令牌时的 `Authorization: Bearer ...`，以及插件来源标记。反向模式由插件监听，例如 `0.0.0.0:8081/queqiao`，QueQiao 的 `websocket_client.url_list` 指向这个地址。多台反向服务器可以共用同一监听地址、端口与路径，按唯一的 `x-self-name` 和各自 token 区分；不同路径也可复用同一端口。重复连接、路径不符、缺身份、token 不符及插件自连会被拒绝。正向模式兼容 QueQiao 关闭鉴权的配置，反向模式不允许空 token。[QueQiao 官方连接说明](https://github.com/17TheWord/QueQiao#快速开始)、[官方 Server/Client 配置](https://queqiao-docs.pages.dev/config/)

协议身份 `name` 在配置中保持原文；正向发送 `x-self-name` 时做一次 UTF-8 URL 表单编码，反向收到该头时解码一次，与 QueQiao 的 `URLEncoder` / `URLDecoder` 行为一致。中文、空格、字面量 `+` 和 `%` 因此不会在握手时改变身份。`Authorization` 头里的 token 保持原样，不做 URL 编码或解码。[官方握手校验源码](https://github.com/17TheWord/queqiao-core/blob/main/src/main/java/io/github/theword/queqiao/core/websocket/WsServer.java)、[官方反向客户端源码](https://github.com/17TheWord/queqiao-core/blob/main/src/main/java/io/github/theword/queqiao/core/websocket/WsClient.java)

反向接收示例只需将以下服务器字段加入 GUI 中的服务器记录，其余字段使用默认值。QueQiao 的 `server_name` 设置为 `PaperMain`，`access_token` 与插件相同，并启用 `websocket_client`。真实公网部署可在前面加 TLS 反向代理，令牌不写进 URL。

```json
{
  "id": "paper-main", "name": "PaperMain", "display_name": "幻域生存服",
  "connection_mode": "reverse", "reverse_host": "0.0.0.0",
  "reverse_port": 8081, "reverse_path": "/queqiao",
  "token": "替换为你自己的随机令牌"
}
```

请求使用 `{api, data, echo}`。`send_rcon_command` 的 `data` 是 `{command: "..."}`；成功回包的顶层 `data` 是 RCON 原始文本。`broadcast` 的 `data.message` 是 Minecraft 文本组件数组。请求回包按每条连接的唯一 `echo` 关联，服务端事件由单独队列消费，事件处理期间再次发起 RCON 不会阻塞回包读取。[V2 总览](https://queqiao-docs.pages.dev/api/v2/)、[RCON 接口](https://queqiao-docs.pages.dev/api/v2/rcon-command)、[广播接口](https://queqiao-docs.pages.dev/api/v2/broadcast)

API 的 `code: 200, status: SUCCESS` 仅说明收到了接口结果。签到成功、重复签到、商店奖励交付等业务结论仍由对应命令的结果解析器决定，不能仅凭 `SUCCESS` 认定。

| 情况 | 传输层结论 | 业务处理要求 |
| --- | --- | --- |
| WebSocket 未连接或请求容量已满 | `GatewayUnavailable` | 未发送，可提示稍后重试 |
| RCON 返回 `503 / FAILED` | `GatewayUnavailable` | 官方语义为 RCON 未启用或未连接 |
| 请求发出后超时、断连、接口名不匹配、回包缺少状态 | `OutcomeUnknown` | 不自动重发，保留待核对状态 |
| RCON 返回其他错误，包括 `500` | `OutcomeUnknown` | 命令可能已经生效，不自动退款或重发奖励 |
| RCON 成功回包 `data` 不是字符串 | `OutcomeUnknown` | 不把未知数据结构当成奖励成功 |
| 非 RCON 接口明确拒绝 | `GatewayError` | 按明确失败处理 |

这里对 RCON 错误采用保守语义：当前官方实现把发送后的读取/执行异常映射为 `500`，旧版本也可能没有细分失败阶段。收到错误包不能证明游戏内副作用没有发生。[官方 RCON API 源码](https://github.com/17TheWord/queqiao-core/blob/0ee1b4c69f2d152a61c697b77d05fcb4ac7c5e7d/src/main/java/io/github/theword/queqiao/core/api/SendRconCommandApi.java)、[官方 RCON 客户端](https://github.com/17TheWord/queqiao-core/blob/0ee1b4c69f2d152a61c697b77d05fcb4ac7c5e7d/src/main/java/io/github/theword/queqiao/core/rcon/RconClient.java)

正向连接会以 1–60 秒退避重连，反向连接等待 QueQiao 客户端按其配置重新连接；两种模式均不保存待重放命令。每服最多 128 个等待回包的请求、256 条待消费事件，WebSocket 单包限制 2 MiB。命令禁止换行和 NUL，UTF-8 长度限制 1446 字节。未确认的旧回包不会匹配重连后的新请求。

## QQ ↔ Minecraft 富消息

普通转发先排除明确指令、AI 唤醒/已接管、已停止或已消费的事件。1.4.2 使用优先级 10 的前置入口，在本插件命令 20 之后、内建群聊上下文 0 之前把普通消息发送到游戏；当前群可能主动回复或配置无法可靠读取时，改由 -1000 入口在后续未接管时转发。前置入口不停止群聊记录或图片转述，同一事件只尝试转发一次。第三方 AI 插件要排他接管，应以高于 10 的优先级提前设置 `ai_claimed` 或停止事件，后续未标记的接管无法预判。

传输层支持原始 OneBot 字典、对象、CQ 字符串以及 AstrBot 完整消息链，按消息段顺序合并适配层已解析的字段。游戏侧仅向配置中启用的精确 UMO 回发。机器人自己的 QQ 消息和私聊不进入普通群转发，双方消息有去重和短期限流。优先级调整不跳过 AstrBot 在插件前的图片本地化；1.4.1 的临时慢阶段计时已在 1.4.2 移除，具体顺序见 [升级说明](UPGRADE_1_4.md#图片转发顺序)。

| OneBot 内容 | 原版 Minecraft 表现 |
| --- | --- |
| 文字、@ | 文字或黄色提及；支持 Minecraft 颜色和样式 |
| QQ `face` / `emoji` / `mface` | `[表情]`，悬浮显示名称或表情 ID；大表情有公开 URL 时可点击 |
| `dice` / `rps` | `[骰子：结果]` / `[猜拳：结果]`，同样由 `face` 类型开关控制 |
| HTTP(S)、裸 `www.` 链接、可识别分享卡片 | 可点击 `[链接]` / 卡片标题，悬浮显示地址；`www.` 补为 HTTPS |
| 图片 | `[图片]` 点击 QQ 资源原链接，或已发布的缓存链接 |
| 语音 | `[语音]` 点击资源链接；转码缓存可打开浏览器播放器/下载页 |
| 视频 | `[视频]` 点击 QQ 资源原链接，或缓存播放器/下载页 |
| 文件 | `[文件：名称]` 点击下载 |
| 引用 | `[回复 某人]`，悬浮分行显示原作者、消息与原消息时间；附件显示类型摘要 |
| 合并消息 | 数量标签、前几条悬浮摘要、完整记录限时页面 |

引用按需调用 `get_msg`，从 AstrBot 的 `get_group_id` / `message_obj.group_id` 取得当前会话，校验返回消息属于当前群或私聊；无法确认归属时仅显示占位。适配时优先保留 `model_dump()` / pydantic.v1 `.dict()` 的字段，避免 `Reply.toDict()` 仅保留 ID 而丢失引用链；通过会话校验后可用这条已解析链补全悬浮内容。不会因为存在一段引用文本就跳过归属校验。[AstrBot 官方消息组件实现](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/message/components.py)

QQ → MC 的 `prefix` 支持引用组件占位符 `{reply}`，可独立决定引用标签的位置。例如将原来的 `&e[{group}]&e{sender}&8:` 改成：

```text
&e[{group}]&e{sender}{reply}&8:
```

`{reply}` 插入 `[回复 作者/ID]` 标签及其悬浮事件，不把引用降为纯文本；标签默认灰色，可通过下述消息颜色配置修改，仍保留原有尾随空格。前后 `&` / `§` 颜色与样式连续生效，引用本身不会改变后续文字的颜色状态，支持两种 Minecraft 组件格式。无引用或关闭路由 `reply` 类型时，占位符为空且不查询引用内容。

模板包含 `{reply}` 时，当前消息的顶层引用只在指定槽位展示，正文不再重复追加；其他正文段的顺序不变，合并消息内部的引用位置也不变。重复写多个 `{reply}` 会在每个位置按原顺序渲染同一组引用组件，每条引用只查询一次，仍遵守前缀长度上限。模板没有该占位符则兼容旧行为，引用继续出现在原消息段位置。`{{reply}}` 表示字面文字 `{reply}`，昵称或群名中的同名文字不被当作槽位。

1.2.0 的新默认前缀为 `[{group}] {sender}{reply}：`。配置升级只迁移旧官方默认前缀；已有自定义前缀保持不变，需要按希望的位置手动加入 `{reply}`。组件插槽仍经过同一会话归属校验，不开放跨群引用，也不引入 ChatImage。

引用悬浮模板由 `routes[].templates.reply_hover` 配置，默认是三个独立行：

```text
{sender}
{message}
Date: {time}
```

`{sender}` 是被引用消息的作者名称或 ID；`{message}` 保留原消息文字、颜色、URL 原文及附件类型摘要；`{time}` 是原消息时间，按当前路由 `timezone` 显示为 `HH:MM:SS`。优先使用同会话 `get_msg` 回包的 `time` / `timestamp`，缺失时使用已校验归属的引用组件 `timestamp` / `time`，支持 Unix 秒和毫秒。缺失或无法解释的时间显示“未知”，不使用当前时间；跨群引用的作者、内容和时间均不会展示。例如：

```text
占星术士
www.google.com
Date: 11:11:11
```

GUI 中可以分别修改语义标签颜色，保存到每条群路由的 `message_colors`。支持 Minecraft 的 16 个标准颜色名或 `#RRGGBB`：

| 配置键 | 标签与默认颜色 |
| --- | --- |
| `link` | 文字中的 HTTP(S) / `www.` 链接和分享卡片，`blue` |
| `image` / `record` / `video` / `file` | 图片 / 语音 / 视频 / 文件，分别默认 `blue` |
| `forward` | 合并消息，`blue` |
| `reply` | 引用，`gray` |
| `face` | 普通表情、大表情、骰子、猜拳，`green` |
| `at` | 提及，`yellow` |

例如 `"message_colors": {"link": "gold", "image": "#12ABEF", "reply": "gray"}`；未列出的类型沿用默认值。类型颜色也用于该类型的“暂不可用”标签，通用截断提示仍为灰色。普通文字和前缀继续按自身的 Minecraft 色码渲染，标签不会把自己的颜色传递给后续正文。颜色配置与悬浮模板同时支持 legacy / modern 组件格式，不改变链接地址或引用归属校验。

语音、视频、图片、文件有公开 HTTP(S) 原地址时直接提供点击链接，不要求先启用媒体服务，也不在转发时抓取该地址。没有公开地址才按需查询 NapCat：语音使用 `get_record(out_format="mp3")`，图片使用 `get_image`，视频使用 `get_file`，文件优先 `get_group_file_url` / `get_private_file_url`，再尝试 `get_file`。NapCat 的 `get_record` 可能把 `url` 返回为转码后的本地路径；若同时有 `base64`，插件可通过有大小限制的媒体缓存发布它，不因此开放任意本地路径。[NapCat GetMsg](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/msg/GetMsg.ts)、[GetRecord](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/file/GetRecord.ts)、[GetFile](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/file/GetFile.ts)

合并消息按需调用 `get_forward_msg`，只展开当前消息实际携带的转发 ID，限制嵌套深度和节点数量；完整页面需要可选媒体服务，未开启时仍有文字悬浮摘要。无法获取附件时保留“暂不可用”标签，普通文字继续转发。实现参考用户指定项目的标签与引用交互，但不引入 NoneBot 依赖，不生成 ChatImage 的 CICode。[参考项目的消息解析](https://github.com/17TheWord/nonebot-plugin-mcqq/blob/main/nonebot_plugin_mcqq/utils/parse_qq_msg.py)

QQ→MC 支持 `§a` / `&a` 等传统色码、`§l` / `&l` 等样式、`&#12abef` / `§#12abef` 与 `&x&1&2&3&4&5&6` / `§x§1§2§3§4§5§6` 十六进制色码；它们转换为 JSON 的 `color`、`bold` 等属性，`r` 重置样式。引用悬浮中的色码同样转换为样式，悬浮正文里的 URL 保留原文。MC→QQ 清理这些控制码。URL 先单独识别，`?a=1&b=2` 中的 `&b` 保持原样，不当作色码。

MC 的聊天、加入、离开、死亡和成就事件可按路由开关。死亡采用官方 `notice` / `PlayerDeathEvent` / `death.text` 模型，同时支持由 `sub_type: player_death` 识别事件；成就兼容 `translate` / `translation`、旧 `text` 和 `display.title`。空或 `null` 消息 ID 不参与去重，有 ID 时按服务器、事件类型和 ID 区分，聊天不会把同 ID 的死亡事件误去重。通知限流也与聊天分开。若实服仍不转发死亡，请核对 QueQiao 的 `subscribe_event.player_death` 与该路由的事件开关；插件不能恢复服务端从未发送的事件。游戏命令不作为普通消息转发。[官方死亡事件](https://queqiao-docs.pages.dev/events/v2/notice/player-death-event)、[死亡事件源码](https://github.com/17TheWord/queqiao-core/blob/main/src/main/java/io/github/theword/queqiao/core/event/PlayerDeathEvent.java)、[Paper 事件订阅实现](https://github.com/17TheWord/QueQiao/blob/main/paper/origin/src/main/java/com/github/theword/queqiao/EventProcessor.java)、[官方成就事件](https://queqiao-docs.pages.dev/events/v2/notice/player-achievement-event)

`servers[].component_version` 控制发送给 QueQiao 的 JSON 组件格式：

- `legacy`：`clickEvent.value`、`hoverEvent.contents`，默认用于旧版组件反序列化器。
- `modern`：`click_event.url`、`hover_event.value`，对应 Minecraft 1.21.5 起的新格式。

选择依据是 Paper/QueQiao 实际使用的组件反序列化器；跨版本代理场景应测试两种字段格式。插件只生成打开 HTTP(S) 地址和显示文字的行为，不把用户内容转成执行命令。原版客户端不会在聊天栏内直接播放语音或内嵌图片。[Minecraft 1.21.5 官方组件变更](https://www.minecraft.net/en-us/article/minecraft-java-edition-1-21-5)

## 独立媒体服务

媒体服务只提供只读、随机 token 的临时内容，和 AstrBot 管理接口分离。示例配置：

```json
{
  "media": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8766,
    "public_base": "https://media.example.com",
    "ttl_seconds": 86400,
    "max_bytes": 26214400,
    "max_cache_bytes": 536870912,
    "trusted_local_roots": [],
    "allowed_hosts": []
  }
}
```

这个服务是可选功能：QQ 已提供的公开资源原链接始终可以直接点击；需要发布 Base64/可信本地素材或完整合并页面时才需要它。域名由部署者实际配置，并将 `/m/` 和 `/v/` 反向代理到 `host:port`；使用基础路径时反向代理应剥离该路径后转发。未开启或没有 `public_base` 时，不伪造缓存地址：只有缺少公开原链接的附件显示“暂不可用”，合并消息保留悬浮摘要。原 QQ 地址的有效期由 QQ 决定，插件不能延长它。

URL 的持有人在有效期内可以访问对应内容，链接可能被收件人转发，因此仅把已授权转发的当前会话素材发布到这里。服务不包含管理员页面、奖励码、订单详情、目录列表或上传接口。

直链只接受无凭据 HTTP(S)，拒绝私网/环回/链路本地 IP 字面地址和本地网络主机名；`allowed_hosts` 非空时额外限制精确域名。直链不做服务器端 HTTP 读取。缓存服务下载时进一步校验每次跳转和实际 DNS 解析结果，所有地址都必须是公网地址，连接使用已校验的解析结果。

未配置可信目录时，任何本地路径都不可发布；需要共享 NapCat 转码文件时，只挂载专用媒体缓存目录并加入 `trusted_local_roots`，不要加入整个 AstrBot 数据目录。符号链接按真实路径检查，UNC/网络共享路径被拒绝。API 返回的 Base64 内容经过编码验证及解码前后大小检查，支持辨认常见图片、MP3、WAV、Ogg 和 MP4 文件头。

下载有总超时、跳转次数、单文件和总缓存限制。内容使用随机文件名，索引位于插件数据目录的 `media/index.sqlite3`，每分钟清理过期文件；过期请求立即返回 404。合并记录对文字做 HTML 转义，只生成安全链接。响应包含 CSP、`nosniff` 和不缓存头；未知类型及普通文件强制下载。配置重载保留索引和未过期缓存。

## 性能数据与图表

从 1.3 起，GUI 提供数值纵轴；GUI 与 PNG 的 MSPT 均按 `原始毫秒数 / 50` 显示为 ticks，并注明 `1 tick = 50 ms`。例如 50 ms 对应 1 tick，12 ms 对应 0.24 ticks。采样数据库仍保存毫秒，RCON 原文回复不变。该显示值是按固定标准换算的耗时，不是 TPS。[Paper MSPT 定义](https://docs.papermc.io/paper/reference/commands/#mspt)

`服务器状态` 仅返回当前路由的“在线”或“离线”，依据 QueQiao 连接状态，不等同于完整游戏健康检查。`tps`、`mspt`、`ping` 指令直接执行对应 RCON 查询，并清洗后返回完整文字，不再返回曲线图。`性能图` 是独立动作，读取历史并生成 PNG，可传小时数，默认 24 小时。实际触发名都以 AstrBot 管理行为为准。

定时器启动后自动采样；同一服务器不重叠探测。每服可配置 `tps_command`、`mspt_command`、`ping_command`，默认分别为 `tps`、`mspt`、`ping`。Paper 的 `tps` 返回最近 1、5、15 分钟三个窗口，采样选取第一个窗口并保存其余窗口；颜色码不会影响识别。[Paper TPS 命令实现](https://github.com/PaperMC/Paper/blob/main/paper-server/src/main/java/org/spigotmc/TicksPerSecondCommand.java)

普通 `ping` 的实现取决于服务端安装的插件，不能保证存在毫秒数，例如 EssentialsX 无参数时只发送 `pong` 文案。采样识别带 `ms` 的已支持数值格式；`Pong!`、仅玩家可用、未知命令或其他无法解析的回包记为 `null` 和不可用原因，不写成 0，也不使用 SLP RTT 代替玩家延迟。QQ 的 `ping` 文本查询仍返回实际回包经通用清洗后的原文，不受图表能否解析数值影响。[EssentialsX ping 实现](https://github.com/EssentialsX/Essentials/blob/2.x/Essentials/src/main/java/com/earth2me/essentials/commands/Commandping.java)

管理员仍可显式配置 Spark 等查询命令；解析器保留对 `[⚡]` 前缀、多行/连续回包和统计窗口的支持。启动读取旧配置 `schema_version=2` 时，仅把精确的旧默认值 `spark tps` / `spark ping` 一次性迁移为 `tps` / `ping`，其他自定义命令不变。保存为配置 v3 或更高版本后再次主动选择 Spark，后续启动不会替换。[配置迁移约定](IMPLEMENTATION_CONTRACT.md)、[Spark 命令说明](https://spark.lucko.me/docs/Command-Usage)、[Spark 指标输出定义](https://github.com/lucko/spark/blob/master/spark-common/src/main/java/me/lucko/spark/common/command/modules/HealthModule.java)

| 字段 | 含义 |
| --- | --- |
| `timestamp` | 采样开始时刻的 Unix 秒数 |
| `tps` | 第一个已报告窗口的 TPS；窗口保存在 `values.tps` |
| `mspt` | Paper 的平均 tick 时长或 Spark 的中位数，统计口径和窗口保存在 `values.mspt` |
| `player_ping_ms` | 服务端报告的玩家延迟；没有可识别数值则为空。显式配置 Spark 时使用当前在线玩家中位数，并保存当前/15 分钟统计 |
| `slp_rtt_ms` | 机器人到 `game_host:game_port` 的 Minecraft 状态协议 ping/pong 往返耗时 |
| `errors`、`status` | 每项不可用原因以及完整/部分/缺失状态 |

玩家延迟与机器人到服务器 RTT 是两条不同网络路径，GUI 和 PNG 分别标注；未设置 `game_host` 时 SLP 一项留空。采样持久化在 `metrics.sqlite3`，按 `monitor.retention_days` 清理，历史查询升序返回且保留 `null`。PNG 使用四个独立面板，缺失点断线，时区使用插件配置。进程停机期间没有探测，不补造历史样本。

绑定默认 `binding_verification_enabled: false`：普通 QQ 用户直接提交玩家名完成唯一绑定，存储不会伪造一个“已验证 UUID”。开启此选项后，普通用户才需要游戏内一次性验证码，通过经鉴权的玩家事件取得真实 UUID。管理员替他人或自己绑定、修改绑定、解绑均直接办理，免本次冷却检查且无需验证码；操作会作废目标 QQ 的旧验证码，唯一约束仍保留，也不清除该用户后续普通操作的冷却。GUI 管理员解绑使用同一业务路径。此配置不会绕过传输鉴权，详情见 [业务说明](BUSINESS.md)。

离线测试覆盖请求/回包关联、正反向连接、事件中嵌套请求、断连/错误/超时的不确定结果、原链接与缓存边界、Base64 上限、私网跳转/DNS 拒绝、过期、HTML 转义、跨会话引用拒绝、真实 AstrBot 消息对象、表情和颜色、空 ID 死亡事件、监控缺失值及 PNG 输出。**真实 QQ 群、Paper 服务、客户端点击行为与实服 RCON 文案尚未联调**；离线协议测试不替代实际部署验收。
