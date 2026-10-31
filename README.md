# Yuma MCQQ

独立的 AstrBot 插件，连接 **NapCat/OneBot ↔ Paper + QueQiao V2**。按新设计重写；旧插件只用于显式数据迁移。

![管理界面（合成测试数据）](docs/screenshots/dashboard.png)

## 已实现

- 默认直接绑定玩家名，可选游戏验证码验证真实 UUID；QQ/玩家名全局唯一，已验证 UUID 也唯一，修改/解绑保留冷却、余额和历史。
- **游戏签到 → 群签到**：先执行 `signin click 玩家`，识别“成功”或“今天已经签到”后才检查群签到，一日只发一次点数。
- 一种群积分货币，显示名称可自定义；点数商店、私聊文本/唯一码/游戏命令商品、确认兑换、补领和未知交付核对。
- 每条自定义 RCON 指令独立参数、清洗前缀、解析规则及回复模板；共用颜色/控制码清洗器。
- 23 个内置指令和自定义指令均注册到 AstrBot **管理行为**，可改触发名、别名和启停；可使用 AstrBot 唤醒方式或设置本插件独立前缀。
- 按原始 **UMO** 回复和路由；明确指令先处理，AI 唤醒消息交回 AstrBot，普通消息最后转发。
- 文本、@、QQ 表情、图片、链接、合并消息、语音、视频、文件、引用；原版客户端使用点击链接和文字悬浮，支持颜色组件，无需 ChatImage。
- QueQiao 正向/反向连接、独立服务器显示名称；游戏聊天、加入、退出、死亡、成就事件分别开关。
- `tps` / `mspt` / `ping` 返回实际 RCON 文字；`服务器状态` 只回复在线/离线；`性能图` 提供定时采样的历史 PNG，GUI 查看曲线。
- AstrBot 内嵌管理 GUI：连接/路由、命令、绑定/点数、商店/订单、监控、解析预览与迁移。

## 安装与首次配置

要求 Python 3.11+、AstrBot **4.29+ / <5**、NapCat/OneBot、Paper 和官方 [QueQiao](https://github.com/17TheWord/QueQiao)。已在真实 AstrBot 4.29.0 框架中进行隔离集成测试。动态命令桥接使用 AstrBot 内部注册接口，升级框架应重新跑集成测试。

在 AstrBot 插件管理中使用[本仓库](https://github.com/Junnstoy/astrbot_plugin_yuma_mcqq)安装或更新，确认所安装代码的版本为 1.1.1。手动安装时将插件目录命名为 `astrbot_plugin_yuma_mcqq`，在 AstrBot 使用的 Python 环境安装 `requirements.txt`，然后加载插件。已有 1.0 配置和数据库的用户先看 [1.1 升级说明](docs/UPGRADE_1_1.md)，本次修订见 [1.1.1 更新说明](docs/UPGRADE_1_1_1.md)。

1. 在 Paper 安装 QueQiao 与 LiteSignIn，启用 QueQiao WebSocket 和其 RCON 支持；先在服务器确认 `signin click 玩家名` 可用。
2. 打开 AstrBot → 插件详情 → 插件视图 → `dashboard`。添加服务器，填写与 QueQiao 一致的鉴权名称 `name`；可另设 `display_name` 用于 QQ 展示。正向模式填写 QueQiao 的 WebSocket 地址；反向模式由 QueQiao 连接机器人监听地址，必须设置非空 token。
3. 在目标 QQ 群执行 AstrBot `/sid`，将得到的完整 UMO 添加为群服路由；配置点数奖励、管理员与转发方向。
4. 在 AstrBot 管理行为中设置所需指令名称。默认发送 `绑定 玩家名` 即可完成；只有打开“游戏验证码验证”后才需要在游戏发送 `bind 验证码`。全局设置可修改货币名称和本插件唤醒前缀。
5. 使用 `签到` 验证游戏、群签到顺序；路由表单可分别修改首次成功与重复签到回复。默认 LiteSignIn 回包无需编写规则；高级区的清洗前缀只影响识别，不会从 QQ 展示的 `{rcon}` 中删除插件前缀。
6. 在商店 GUI 添加商品并上架。游戏交付商品必须填写可确认成功的回包规则。通过转发类型与游戏事件复选框选择消息范围。
7. QQ 已提供的可公开访问图片、视频、语音和文件链接可直接点击，不要求开启缓存服务。本地媒体、需转存资源和完整合并消息页面才需要配置 `media.public_base` 等缓存服务选项。详见 [传输与部署](docs/TRANSPORT.md)。

`command_prefix` 留空时沿用当前会话的 AstrBot 唤醒方式，例如 `/签到` 或 @机器人 后发送；填写 `mc` 后，本插件命令使用 `mc签到` 或 `mc 签到` 这样的独立前缀。这里展示的是默认命令名，实际名称仍以管理行为为准。首次创建后配置由插件 GUI 管理并保存到插件数据目录，基础配置面板中的 `enabled` 仅用于首次初始化。

| 默认指令 | 用途 |
| --- | --- |
| `mc帮助` | 显示当前管理行为生效后的名称 |
| `绑定 玩家名` / `我的绑定` / `修改绑定 玩家名` / `解绑` | 直接绑定，或按开关验证后绑定；查询及变更 |
| `签到` / `点数` / `点数排行` | 两阶段签到和账户查询 |
| `点数商店` / `兑换 商品ID` / `确认兑换 订单ID` | 浏览及购买一件商品 |
| `兑换记录` / `补领 订单ID` | 查看订单，重新私聊同一份已分配奖励 |
| `服务器状态` | 仅回复 QueQiao 连接在线或离线，不需要参数 |
| `tps` / `mspt` / `ping` | 执行服务器配置的查询，返回清洗后的实际 RCON 文本，不需要参数 |
| `性能图 [小时数]` | 历史 PNG，默认 24 小时，范围 1–168 |
| `添加点数` / `扣除点数` / `设置点数` | 管理员：QQ或绑定名、数量、可选原因 |
| `绑定 QQ号 MC名` / `修改绑定 QQ号 MC名` / `解绑 QQ号` | 管理员代操作，直接完成且不受 CD 限制；仍保持绑定唯一性 |
| `cmd 游戏命令` | 管理员向当前群对应服务器发送原样 RCON，例如 `cmd cmi ping rincho` |
| `订单核对 订单ID success或failed 依据` | 管理员核实交付后结算 |

## 文档

- [完整流程拓扑与模块结构](docs/ARCHITECTURE.md)
- [绑定、签到、商店状态机](docs/BUSINESS.md)
- [自定义命令、清洗与模板](docs/COMMANDS.md)
- [媒体、QueQiao、监控部署和边界](docs/TRANSPORT.md)
- [旧数据迁移、作用域映射和回滚](docs/MIGRATION.md)
- [1.1 升级与八项反馈修复](docs/UPGRADE_1_1.md)
- [1.1.1 管理员绑定与查询命令修订](docs/UPGRADE_1_1_1.md)
- [测试记录与实服验收步骤](docs/VALIDATION.md)
- [设计决策和开发记录](docs/DEVELOPMENT.md)、[变更记录](CHANGELOG.md)

尚未连接真实 QQ/Paper 做端到端验收；部署前按验证文档核对具体 QueQiao、LiteSignIn 和 Paper 输出。GUI 的“模拟回复（不执行）”只解析粘贴回包，不能证明服务器已经执行。媒体缓存服务默认关闭；RCON 超时、断线和未知回包不会自动重试交付。语音链接能否直接播放取决于来源格式和浏览器。

## 本地验证

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q core services adapters main.py
node --check views/dashboard/app.js
```

未安装 AstrBot 时框架集成测试明确跳过，其余测试使用临时数据库和模拟服务。GUI 验收脚本为 `tests/gui_smoke.py`，需要额外安装 Playwright 并设置 `MCQQ_BROWSER` 指向 Chromium/Edge；截图只含合成数据。CI 运行 Python 3.11/3.12 的核心测试。
