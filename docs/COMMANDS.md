# 自定义指令、清洗与模板

每条自定义指令是独立 action，而不是共享一个正则入口。GUI 创建配置后即注册到 AstrBot 的管理行为；稳定 `id` 用于注册和操作去重。修改实际触发名、别名和入口启停请使用 AstrBot 管理行为，GUI 显示这些生效值。配置中的 `name/aliases` 是注册初始值；已有管理行为覆盖优先。

## 处理链

```mermaid
flowchart LR
  A[唤醒和名称命中] --> B[管理员 / 绑定 / 冷却]
  B --> C[按类型校验参数]
  C --> D[渲染一条RCON命令]
  D --> E[保存操作ID后发送]
  E --> F[共用清洗器]
  F --> G[当前指令解析器]
  G --> H[状态与命名字段]
  H --> I[当前状态的回复模板]
  I --> J[原始UMO]
```

清洗器去除 ANSI、Minecraft `§`/`&` 传统及十六进制颜色、控制字符、示例中的时间戳与 `Rcon:` 前缀，以及配置中显式给出的插件前缀。不会删除任意方括号内容，以免误删实际业务文本。

解析方式：`rules` 为完整正则匹配，`litesignin` 为已给定签到回包，`text` 为只清洗查询结果。`text` 的 `SUCCESS` 仅表示收到了文本，不能用来证明发奖或扣费已完成。多条规则同时匹配、超时、玩家不一致、空/未知回包均进入 `UNKNOWN`。

正则使用命名捕获 `(?P<player>...)` 等字段。每次匹配限时 50ms、输入限长；不要使用不完整的关键词“成功”判断关键操作。命令参数支持 `word`、`integer`（上下限）、`player`、`enum`（choices）。不支持任意 Python、属性访问或多条命令拼接。

模板只支持 `{字段名}`，花括号原文使用 `{{` 和 `}}`；不支持 `{obj.attr}`、`{x[0]}` 或格式转换。命令缺少参数会被拒绝，显示模板中不存在的字段为空。常用字段为 `player`、`player_name`、`user_id`、`date`、`cleaned`、`status` 和当前规则命名捕获。

## 只读查询示例

可在 GUI 按字段填写以下配置；这条动作允许无需绑定的成员查询在线名单：

```json
{
  "id": "online_list",
  "name": "在线玩家",
  "aliases": ["在线名单"],
  "enabled": true,
  "admin_only": false,
  "require_binding": false,
  "cooldown_seconds": 5,
  "server_id": "",
  "rcon": "list",
  "parameters": [],
  "parser": "text",
  "cleanup_prefixes": [],
  "rules": [],
  "templates": {"SUCCESS": "在线信息：\n{cleaned}", "UNKNOWN": "结果未识别，请联系管理员。"}
}
```

`server_id` 留空跟随当前路由；需要绑定的动作还会验证绑定服务器与目标一致。管理员创建的 RCON 模板按控制台权限运行，所以默认 `admin_only: true`；仅将明确允许成员使用的动作开放。

## LiteSignIn 签到

内置“签到”使用专门的两阶段业务，不应替换成只执行 RCON 的自定义命令。默认解析以下两条清洗后的结果：

```text
成功给玩家 rincho 签到， 当前该玩家已连续签到 1 天。
玩家 rincho 今天已经签到了，请不要重复签到。
```

第一条得到 `SIGNED` 和 `player/days`，第二条得到 `ALREADY_SIGNED` 和 `player`。两者都进入群签到判断。群服路由可配置 `signin_command`、`signin_prefixes`、`signin_rules` 和 `templates.signin`；`signin_rules` 留空沿用内置规则。自定义签到规则只接受 `SIGNED/ALREADY_SIGNED/FAILED`；成功或重复状态必须有命名捕获 `player` 用于核对。

GUI 的“回包测试”和“LiteSignIn 样本预览”只处理粘贴文本，展示清洗文本、状态、字段及模板结果，不连接 RCON。调整规则时先分别试成功、重复、失败、错误玩家及陌生回包，再保存并在实服验收。

未知自定义命令结果保存在操作记录，相同 UMO/消息 ID/action 不重发。**重新发送一条新消息是新请求**，因此涉及不可重复副作用的操作应设计服务器侧幂等或使用商店订单流，并先核对已有结果。
