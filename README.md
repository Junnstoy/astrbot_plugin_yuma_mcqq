# Yuma MCQQ

独立的 AstrBot 插件，连接 **NapCat/OneBot ↔ Paper + QueQiao V2**。按新设计重写；旧插件只用于显式数据迁移。

![管理界面（合成测试数据）](docs/screenshots/dashboard.png)

## 已实现

- QQ ID 与 MC UUID 全局双向唯一绑定；游戏验证码验证，修改/解绑保留冷却、余额和历史。
- **游戏签到 → 群签到**：先执行 `signin click 玩家`，识别“成功”或“今天已经签到”后才检查群签到，一日只发一次点数。
- 一种点数货币、点数商店、私聊文本/唯一码/游戏命令商品、确认兑换、补领和未知交付核对。
- 每条自定义 RCON 指令独立参数、清洗前缀、解析规则及回复模板；共用颜色/控制码清洗器。
- 内置和自定义指令均注册到 AstrBot **管理行为**，可改触发名、别名和启停；遵循会话唤醒头与权限。
- 按原始 **UMO** 回复和路由；明确指令先处理，AI 唤醒消息交回 AstrBot，普通消息最后转发。
- 文本、@、图片、链接、合并消息、语音、视频、文件、引用；原版客户端使用点击链接和文字悬浮。
- TPS、MSPT、玩家 ping、机器人到服务器 RTT 定时采样，历史 PNG 回复和 GUI 曲线；缺失数据保留为空。
- AstrBot 内嵌管理 GUI：连接/路由、命令、绑定/点数、商店/订单、监控、解析预览与迁移。

## 安装与首次配置

要求 Python 3.11+、AstrBot **4.29+ / <5**、NapCat/OneBot、Paper 和官方 [QueQiao](https://github.com/17TheWord/QueQiao)。已在真实 AstrBot 4.29.0 框架中进行隔离集成测试。动态命令桥接使用 AstrBot 内部注册接口，升级框架应重新跑集成测试。

开发通过 `feat/session-checkin` 分支及 Pull Request 进行。安装时确认使用包含本次实现的分支；合并前 `main` 不含这些代码。将该分支检出为 AstrBot 的 `data/plugins/astrbot_plugin_yuma_mcqq`，在 AstrBot 使用的 Python 环境安装 `requirements.txt`，然后加载插件。合并后也可直接从仓库 URL 安装。

1. 在 Paper 安装 QueQiao 与 LiteSignIn，启用 QueQiao WebSocket 和其 RCON 支持；先在服务器确认 `signin click 玩家名` 可用。
2. 打开 AstrBot → 插件详情 → 插件视图 → `dashboard`。添加服务器，填写 WebSocket 地址、对应的 `server_name` 和 token。
3. 在目标 QQ 群执行 AstrBot `/sid`，将得到的完整 UMO 添加为群服路由；配置点数奖励、管理员与转发方向。
4. 在 AstrBot 管理行为中设置所需指令名称。用户发送 `绑定 玩家名`，再由指定玩家在游戏聊天中发送 `bind 验证码`。
5. 使用 `签到` 验证游戏、群签到顺序；在商店 GUI 添加商品并上架。游戏交付商品必须填写可确认成功的回包规则。
6. 若需要媒体点击链接，在全局设置配置 `media.public_base` 和可供玩家浏览器访问的媒体服务；启用后先测试图片/语音链接。详见 [传输与部署](docs/TRANSPORT.md)。

命令均需按当前会话的 AstrBot 唤醒方式触发，例如 `/签到` 或 @机器人 后发送；这里展示的是默认命令名。首次创建后配置由插件 GUI 管理并保存到插件数据目录，基础配置面板中的 `enabled` 仅用于首次初始化。

| 默认指令 | 用途 |
| --- | --- |
| `mc帮助` | 显示当前管理行为生效后的名称 |
| `绑定 玩家名` / `我的绑定` / `修改绑定 玩家名` / `解绑` | 账号验证及变更 |
| `签到` / `点数` / `点数排行` | 两阶段签到和账户查询 |
| `点数商店` / `兑换 商品ID` / `确认兑换 订单ID` | 浏览及购买一件商品 |
| `兑换记录` / `补领 订单ID` | 查看订单，重新私聊同一份已分配奖励 |
| `服务器状态` / `tps` / `mspt` / `ping` | 获取性能历史图，可附小时数 1–168 |
| `添加点数` / `扣除点数` / `设置点数` | 管理员：QQ或绑定名、数量、可选原因 |
| `订单核对 订单ID success或failed 依据` | 管理员核实交付后结算 |

## 文档

- [完整流程拓扑与模块结构](docs/ARCHITECTURE.md)
- [绑定、签到、商店状态机](docs/BUSINESS.md)
- [自定义命令、清洗与模板](docs/COMMANDS.md)
- [媒体、QueQiao、监控部署和边界](docs/TRANSPORT.md)
- [旧数据迁移、作用域映射和回滚](docs/MIGRATION.md)
- [测试记录与实服验收步骤](docs/VALIDATION.md)
- [设计决策和开发记录](docs/DEVELOPMENT.md)、[变更记录](CHANGELOG.md)

尚未连接真实 QQ/Paper 做端到端验收；部署前按验证文档核对具体 QueQiao、LiteSignIn 和 Paper 输出。媒体服务默认关闭；RCON 超时、断线和未知回包不会自动重试交付。语音编码可能需要下载后使用支持该格式的播放器，详情见传输文档。

## 本地验证

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q core services adapters main.py
node --check views/dashboard/app.js
```

未安装 AstrBot 时框架集成测试明确跳过，其余测试使用临时数据库和模拟服务。GUI 验收脚本为 `tests/gui_smoke.py`，需要额外安装 Playwright 并设置 `MCQQ_BROWSER` 指向 Chromium/Edge；截图只含合成数据。CI 运行 Python 3.11/3.12 的核心测试。
