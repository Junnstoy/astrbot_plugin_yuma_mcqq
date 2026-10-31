# 验证记录与部署验收

验证日期：2026-10-31。运行环境：Windows、Python 3.12.15、已安装的 AstrBot 4.29.0；GUI 使用本地 Edge。测试使用工作区临时数据、模拟网关、本地测试端口和合成消息，不接入运行中的机器人或 Minecraft 服务。

## 已执行验证

执行命令：

```sh
python -m unittest discover -s tests -v
python -m compileall -q core services adapters main.py
node --check views/dashboard/app.js
python tests/gui_smoke.py
```

1.1 本地全量结果：**116 / 116 项通过**，耗时30.276秒，无跳过；覆盖真实 AstrBot 集成、业务与身份升级、组合服务、文本/配置、正反向传输、媒体与监控，包含官方协议名称编码兼容回归。Python 编译、JavaScript 语法、Git 空白检查均通过。1.0 的历史基线为 66 项通过。

框架测试显式设置独立 ASTRBOT_ROOT；不存在 AstrBot 的环境会明确 skip 该组测试，不能据此声称框架集成通过。浏览器脚本需要 Playwright 和 MCQQ_BROWSER，可选，不属于插件运行依赖。

| 测试组 | 关注的行为 |
| --- | --- |
| Application | 同消息并发只发一次、UNKNOWN持久化、原UMO、重载取消先落库、配置失败回滚、权限拒绝、预览不发RCON；TPS/MSPT/ping返回实际文本、状态仅在线/离线、预览货币占位符 |
| 真实AstrBot集成 | 22个独立内置入口、自定义命令注册和参数、管理行为改名/别名/启停/权限跨重载、冲突和取消回滚、GUI认证、原UMO、OneBot消息ID、AI分流；独立前缀与全局唤醒、别名和权限共同生效 |
| Domain/Store | 唯一绑定与冷却、绑定版本、签到并发/重复/未知/跨日、游戏先行、库存和冻结防透支、同奖励补领、RCON不自动重发退款、调整审计、迁移重复/已用码/已付款补领；免验证默认、同名验证升级、旧库事务升级和历史CD、成功/群重复模板及币名 |
| Text/Config | 两条真实签到样本、颜色清洗、错误玩家、多规则歧义、正则限时、模板注入、原子配置、掩码凭据、参数范围和共享scope策略；旧配置补充face一次、反向鉴权配置校验 |
| Gateway/Media/Relay/Monitor | 本地WS握手和echo、事件回调不死锁、断线/超时/API500、媒体跳转和DNS拒私网、到期和重载、记录转义、跨群引用拒绝、富消息顺序、精确UMO、监控null和PNG；反向鉴权/多服/重连/关闭、真实AstrBot引用对象、CQ/raw链、颜色悬浮与重置、URL查询参数、表情/骰子/大表情、公开源链接、死亡空ID去重 |
| 浏览器GUI | 7页切换、命令新增、解析预览、游戏事件复选框、反向连接字段、默认免验证/前缀/币名；移动端无横向溢出、无JS异常；纯HTTP页面也可使用 |

截图：[桌面总览](screenshots/dashboard.png)、[商店](screenshots/shop.png)、[移动端](screenshots/mobile.png)、[群服路由](screenshots/routes.png)、[转发与事件表单](screenshots/route-editor.png)、[反向连接表单](screenshots/reverse-editor.png)。截图均为合成数据，页面有对应标记。

浏览器最终结果：7页、5项表单流程通过，移动端无横向溢出，JS异常0。Mermaid 11.12.0解析并渲染4/4张图通过；更新后的[完整拓扑SVG](architecture.svg)包含65节点、99连线，使用纯SVG文本，在Edge独立打开验证。

Windows受限沙箱无法完成Python事件循环的本机socketpair初始化，已用仅涉及临时数据库和模拟服务的允许环境运行异步测试。此限制与插件业务网络连通性无关；没有通过访问生产服务规避测试。

CI 配置在 `.github/workflows/tests.yml`，核心测试覆盖Python 3.11/3.12。远程CI结果以PR检查为准，不把尚未运行的版本写作本地已验证。

## 实服验收步骤（尚未执行）

1. 在测试群和Paper测试服配置完整UMO、QueQiao name/token、RCON，确认连接、在线名单与普通双向文字。
2. 分别关闭/开启游戏验证码测试绑定；检查重复QQ、忽略大小写的重复玩家名、已知UUID、错误玩家验证码、过期验证码和同名补充验证；测试换绑/解绑冷却。
3. 首次签到、同日重复、已在游戏签到但尚未群签到、服务断开、未知回包；核对唯一账本和点数。
4. 分别测试图片、语音、视频、文件、QQ表情、分享链接、嵌套合并消息及引用；确认媒体公网可达和期限、原版组件点击/悬浮颜色。测试死亡等事件开关、独立显示名和正反向连接。
5. 在管理行为改名/加别名/停用后重载，检查生效及帮助输出；分别验证独立前缀和AstrBot唤醒，AI唤醒消息不进入游戏。
6. 用明确标记的测试商品检查私聊交付、码不复用、重复确认、补领、限购、库存；对RCON超时检查冻结保留并人工核对，不再次发奖。
7. 核对实际Paper/Spark输出：TPS/MSPT/ping为清洗后的原RCON文本，服务器状态只为在线/离线，性能图为历史PNG；停止服务器后样本应缺失。修改采样间隔并确认GUI/PNG。
8. 如迁移，先停旧业务并制作一致性备份；按迁移指南preview/digest/scope_map/apply，再核对余额、签到、已用码与待补领。不要直接读写运行库。

验收后记录具体AstrBot、NapCat、Paper、QueQiao和LiteSignIn版本及实际结果。无实服连接不影响代码/隔离测试交付，但不能宣称已完成上线验收。
