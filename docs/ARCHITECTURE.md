# 完整流程拓扑

运行主体是一个 AstrBot 插件。QQ 消息由现有 NapCat/OneBot 接入，游戏侧使用 Paper 和官方 QueQiao；插件独立管理身份、点数、交付和配置。

[打开完整 SVG 拓扑，可独立查看和放大](architecture.svg)。下方 Mermaid 是可编辑源图；SVG 使用 Mermaid 11.12.0 生成，并通过浏览器验证。

```mermaid
flowchart TB
  QQ[QQ群 / 用户私聊] <--> OB[NapCat / OneBot]
  OB <--> AB[AstrBot 平台适配器]
  AB --> EV[事件快照：UMO、QQ、消息ID、完整消息链]
  EV --> ENABLE{会话启用 / 路由匹配}
  ENABLE -->|是| CMD{命中明确插件指令}
  ENABLE -->|否| RETURN[交回框架]
  CMD -->|是| AUTH[唤醒头 / 管理行为启停 / 权限检查]
  CMD -->|否| AI{AI 唤醒 / 已处理 / LLM接管}
  AI -->|是| LLM[AstrBot AI 会话]
  AI -->|否| RELAY[低优先级普通消息转发]
  AUTH --> ACT[稳定 action ID / Application 调度]

  subgraph DOMAIN[统一业务服务]
    ACT --> BIND[绑定 / 修改 / 解绑 / 查询]
    ACT --> SIGN[签到操作：保存日期与绑定快照]
    ACT --> POINT[点数 / 排行 / 管理调整]
    ACT --> SHOP[点数商店 / 订单 / 确认 / 补领]
    ACT --> CUSTOM[自定义命令：类型化参数 → RCON模板]
    ACT --> MONREAD[性能历史与PNG查询]
    BIND --> CODE[一次性验证码 / QQ与UUID唯一约束 / 冷却]
    SIGN --> GAME[先游戏签到]
    GAME --> PARSE[清洗 → 专用规则 → SIGNED或ALREADY_SIGNED]
    PARSE --> CREDIT[再检查群签到 → 唯一日期事务 → 点数账本]
    SHOP --> HOLD[冻结点数 / 库存 / 奖励快照]
    HOLD --> DELIVERY{交付方式}
    DELIVERY -->|文本或唯一码| PRIVATE[原OneBot账号私聊QQ]
    DELIVERY -->|游戏命令| RCONORDER[带订单ID的RCON模板]
    PRIVATE --> PROOF[依据消息ID或明确游戏回包确认]
    RCONORDER --> PROOF
    PROOF -->|成功| SETTLE[扣点与完成订单]
    PROOF -->|明确未发送| RELEASE[取消并释放冻结]
    PROOF -->|未知| UNKNOWN[保留冻结 / 管理员核对 / 禁止自动重发]
    CUSTOM --> CPARSE[通用清洗 + 每条命令专用解析与回复模板]
  end

  subgraph TRANSPORT[传输与展示]
    RELAY --> NORMAL["文本/@/图片/链接/合并/语音/视频/文件/引用"]
    NORMAL --> MEDIA[限时资源链接与合并消息详情页]
    NORMAL --> COMP[原版JSON组件：文字 / 点击URL / 引用悬浮]
    MEDIA --> COMP
    COMP --> GW[QueQiao网关：鉴权 / echo关联 / 重连 / 不重放副作用]
    GAME <--> GW
    CUSTOM <--> GW
    RCONORDER <--> GW
    GW <--> Q[Paper / QueQiao V2]
    Q <--> MC[Minecraft 玩家]
    Q --> LS[LiteSignIn / 服务端控制台指令]
    Q --> EVENTS[玩家聊天 / 加入 / 离开 / 死亡 / 成就]
    EVENTS --> VERIFY{是绑定验证码}
    VERIFY -->|是| CODE
    VERIFY -->|否| ROUTE[游戏消息模板 / 多目标原始UMO]
  end

  subgraph MONITOR[性能监控]
    TIMER[定时调度 / 防重叠采样] --> PROBE[TPS / MSPT / 玩家ping RCON + Server List Ping RTT]
    PROBE <--> GW
    PROBE --> HISTORY[时间戳 / 测量对象 / 窗口 / null缺失]
    HISTORY --> PLOT[GUI曲线 / PNG历史图]
    MONREAD --> PLOT
  end

  subgraph MANAGEMENT[管理与持久化]
    GUI[AstrBot已登录Plugin View GUI] --> API[鉴权Plugin API]
    API --> CONFIG[配置校验 / revision冲突检查 / 原子保存与失败回滚]
    CONFIG --> REG[管理行为：每条指令独立注册 / 改名 / 别名 / 启停]
    REG --> AUTH
    API --> ACT
    API --> HISTORY
    API --> PREVIEW[回包清洗和规则预览：不执行命令]
    API --> MIGRATE[显式旧备份 → 只读预览 → 摘要核对 → 单事务导入]
    DB[(SQLite：身份 / 冷却 / 签到 / 账本 / 订单 / 审计)]
    CODE --> DB
    CREDIT --> DB
    POINT --> DB
    HOLD --> DB
    SETTLE --> DB
    RELEASE --> DB
    UNKNOWN --> DB
    MIGRATE --> DB
    OPS[(自定义操作记录 / 监控历史 / 媒体索引)]
    CPARSE --> OPS
    HISTORY --> OPS
    MEDIA --> OPS
  end

  CREDIT --> OUT[按状态渲染回复 / 原始UMO发送]
  CPARSE --> OUT
  POINT --> OUT
  PLOT --> OUT
  ROUTE --> OUT
  CODE --> OUT
  SETTLE --> OUT
  UNKNOWN --> OUT
  OUT --> AB
  PRIVATE --> OB
```

## 关键顺序

1. 消息身份和 UMO 在入口固定。显式指令处理后停止继续流转；只有未被指令、AI 或其他处理器接管的普通群消息进入最后的转发处理器。
2. 游戏签到是每个新签到请求的第一项外部动作。重复的同一消息投递由操作 ID 去重；新的消息仍先询问游戏，再检查本群签到日期。
3. RCON 完整回包先经过共用清洗器，再由对应动作规则生成状态和字段，最后由该动作回复模板渲染。未知状态不会被当成成功。
4. 订单先冻结可用点数和库存，再交付。外部交付无法与 SQLite 原子提交，因此明确保存 sending/unknown 并要求核对，不承诺网络级“恰好一次”。
5. 插件重载先拒收并处理在途操作，保存未知结果，最后关闭后台任务和数据库；启动仅恢复可确定的本地事务，不自动重放外部命令。

## 代码布局

```text
main.py                       AstrBot 生命周期、消息和鉴权 API
adapters/astrbot_commands.py   4.29 指令注册兼容层
core/config.py                配置模型和原子持久化
core/text.py                  清洗、解析和模板
core/store.py                 事务账本、身份、订单和迁移
services/application.py       组合入口和自定义动作
services/domain.py            绑定、签到和商店
services/gateway.py           QueQiao V2
services/media.py             只读限时媒体服务
services/relay.py             富消息双向转发
services/monitor.py           周期采样和图片
views/dashboard/              内嵌 GUI
tests/                        核心、真实框架与浏览器验收
docs/                         设计、迁移、部署及验证记录
```

QQ 与 MC UUID 全局唯一；点数按 `scope + QQ` 隔离；消息按原始 UMO 路由；外部请求按服务器和 echo 关联。将这些标识分开，可避免改指令名、迁移群会话或多服回包串线破坏业务身份。
