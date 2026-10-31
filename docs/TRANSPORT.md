# 传输、富消息与性能采样

本模块按新设计独立实现，不读取旧插件配置或运行数据库。协议目标为 QueQiao V2、NapCat/OneBot 和 Paper；实际 QQ 客户端、Paper、QueQiao 组合仍需在测试群服验收。下面列出当前实现、部署要求和降级行为。

## QueQiao 连接与命令结果

插件作为 WebSocket 客户端连接每个 `servers[].url`。握手携带与 QueQiao 配置一致的 `x-self-name`、可选 `Authorization: Bearer ...`，以及插件来源标记。`name` 必须匹配 QueQiao 的 `server_name`。推荐跨机器使用 `wss://` 或可信私网连接；令牌只保存在管理配置中，状态接口不输出令牌。[QueQiao 官方连接说明](https://github.com/17TheWord/QueQiao#快速开始)

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

连接会以 1–60 秒退避重连，但不保存待重放命令。每服最多 128 个等待回包的请求、256 条待消费事件，WebSocket 单包限制 2 MiB。命令禁止换行和 NUL，UTF-8 长度限制 1446 字节。未确认的旧回包不会匹配重连后的新请求。

## QQ ↔ Minecraft 富消息

普通消息转发由主插件放在明确指令与 AI 唤醒判定之后。传输层使用完整 OneBot 消息链，按原顺序渲染；游戏侧仅向配置中启用的精确 UMO 回发。机器人自己的 QQ 消息会忽略，双方消息有去重和短期限流。

| OneBot 内容 | 原版 Minecraft 表现 |
| --- | --- |
| 文字、@ | 文字或黄色提及 |
| HTTP(S) 链接、可识别分享卡片 | 可点击 `[链接]` / 卡片标题，悬浮显示地址 |
| 图片 | `[图片]` 点击打开限时链接 |
| 语音 | `[语音]` 点击打开浏览器播放器/下载页 |
| 视频 | `[视频]` 点击打开浏览器播放器/下载页 |
| 文件 | `[文件：名称]` 点击下载 |
| 引用 | `[回复 某人]`，悬浮显示文字和附件类型摘要 |
| 合并消息 | 数量标签、前几条悬浮摘要、完整记录限时页面 |

引用按需调用 `get_msg`，校验返回消息属于当前群或当前私聊；无法确认归属时仅显示占位。合并消息按需调用 `get_forward_msg`，按当前消息实际携带的转发 ID 展开；限制嵌套深度、节点总量和单层节点数量。语音按需调用 `get_record(out_format="mp3")`；缺少来源的图片/文件分别查询 `get_image` / `get_file`。NapCat 无法返回可读来源时显示占位，普通文字仍然转发。

MC 的聊天、加入、离开、死亡和成就事件可按路由开关。兼容 QueQiao 的死亡 `text`，以及成就 `translate` / `translation`、旧 `text` 和 `display.title` 字段。游戏命令事件不作为普通消息转发。[官方死亡事件](https://queqiao-docs.pages.dev/events/v2/notice/player-death-event)、[官方成就事件](https://queqiao-docs.pages.dev/events/v2/notice/player-achievement-event)

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

域名需要由部署者实际配置，并将 `/m/` 和 `/v/` 反向代理到 `host:port`；若使用基础路径，则反向代理应剥离该基础路径后转发。浏览器播放地址需要玩家可访问。未开启或没有 `public_base` 时，不生成假链接：媒体显示“暂不可用”，合并消息保留悬浮摘要。

URL 的持有人在有效期内可以访问对应内容，链接可能被收件人转发，因此仅把已授权转发的当前会话素材发布到这里。服务不包含管理员页面、奖励码、订单详情、目录列表或上传接口。

网络下载只允许无凭据 HTTP(S)。每次跳转重新校验，DNS 解析的所有地址都必须是公网地址，连接使用已校验的解析结果；拒绝环回、私网、链路本地及云元数据地址。`allowed_hosts` 非空时额外限制精确域名。未配置可信目录时，任何本地路径都不可发布；需要 NapCat 转码文件时，只把专用媒体缓存目录共享挂载并加入 `trusted_local_roots`，不要加入整个 AstrBot 数据目录。符号链接按解析后的真实路径检查，UNC/网络共享路径被拒绝。

下载有总超时、跳转次数、单文件和总缓存限制。内容使用随机文件名，索引位于插件数据目录的 `media/index.sqlite3`，每分钟清理过期文件；过期请求立即返回 404。合并记录对文字做 HTML 转义，只生成安全链接。响应包含 CSP、`nosniff` 和不缓存头；未知类型及普通文件强制下载。配置重载保留索引和未过期缓存。

## 性能数据与图表

定时器启动后自动采样；同一服务器不重叠探测。每服可配置 `tps_command`、`mspt_command`、`ping_command`，默认分别为 `spark tps`、`mspt`、`spark ping`。Spark 需在服务端可用；也可按实际 Paper 命令修改。解析识别 Spark 的 `[⚡]` 前缀、多行/连续回包、窗口标签和颜色代码。无法解析或命令不存在时写入缺失值，不伪造正常数值。[Spark 命令说明](https://spark.lucko.me/docs/Command-Usage)、[Spark 指标输出定义](https://github.com/lucko/spark/blob/master/spark-common/src/main/java/me/lucko/spark/common/command/modules/HealthModule.java)

| 字段 | 含义 |
| --- | --- |
| `timestamp` | 采样开始时刻的 Unix 秒数 |
| `tps` | 第一个已报告窗口的 TPS；窗口保存在 `values.tps` |
| `mspt` | Paper 的平均 tick 时长或 Spark 的中位数，统计口径和窗口保存在 `values.mspt` |
| `player_ping_ms` | 服务端报告的玩家延迟；Spark 默认使用当前在线玩家中位数，并保存当前/15 分钟统计 |
| `slp_rtt_ms` | 机器人到 `game_host:game_port` 的 Minecraft 状态协议 ping/pong 往返耗时 |
| `errors`、`status` | 每项不可用原因以及完整/部分/缺失状态 |

玩家延迟与机器人到服务器 RTT 是两条不同网络路径，GUI 和 PNG 分别标注；未设置 `game_host` 时 SLP 一项留空。采样持久化在 `metrics.sqlite3`，按 `monitor.retention_days` 清理，历史查询升序返回且保留 `null`。PNG 使用四个独立面板，缺失点断线，时区使用插件配置。进程停机期间没有探测，不补造历史样本。

离线测试覆盖实际请求/回包相关性、事件中嵌套请求、断连/错误/超时的不确定结果、本地媒体目录边界、重定向与私网 DNS 拒绝、token 过期、HTML 转义、跨会话引用拒绝、富消息次序、监控缺失值和 PNG 输出。尚未以真实生产群或游戏服验证网络连通性、客户端点击行为和具体版本的 RCON 文案。
