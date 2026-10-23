# 群签到积分

AstrBot 插件：按会话独立配置每日签到积分，并通过 Minecraft 鹊桥插件同步游戏签到。

仓库：`Junnstoy/astrbot_plugin_yuma_mcqq`；AstrBot 内的插件标识保持 `astrbot_plugin_checkin_points`。

## 功能

- 每个会话、每个用户、每天一次签到，SQLite 事务与唯一约束防止重复发奖。
- 闭区间随机积分；指定日期 `YYYY-MM-DD` 或每年重复日期 `MM-DD` 的倍率。
- 具体日期规则优先于年度规则，倍率不叠乘，最终积分四舍五入。
- 每会话独立时区、多个签到时间窗，支持跨午夜；日期仍以当地自然日计。
- AstrBot 插件管理页配置规则、查看最近签到记录与 MC 指令发送状态。
- 实时复用 `astrbot_plugin_minecraft_queqiao` 的绑定，SQLite 保存全量同步快照；解绑也同步删除。
- MC 指令在积分提交后执行，重复签到不会重发。发送中断或超时记为结果未知，不自动重试奖励指令。

## 安装与使用

需要 AstrBot >=4.29.0、Python >=3.11。通过插件仓库安装，或将本目录放入 `data/plugins/astrbot_plugin_checkin_points`，安装 `requirements.txt` 后重启/重载插件。

打开插件的「签到积分」管理页，新增会话 ID（如 `mybot:GroupMessage:123456789`），填写规则并保存。新会话默认禁用，需显式开启。插件配置中的启用开关控制全局签到。

| 指令 | 用途 |
|---|---|
| `签到` / `checkin` | 每日签到 |
| `积分` / `points` | 查询本会话累计积分 |

指令遵守 AstrBot 全局唤醒规则。例如唤醒头为 `#` 时发送 `#签到`、`#积分`。可以在「管理行为」中重命名。

积分按会话独立；同一用户在不同群的积分分别计算。时间窗按分钟包含起止点，`00:00`–`23:59` 表示全天；跨午夜窗口例如 `22:00`–`02:00`。若变更时区，当日归属也随之改变，请在无签到活动时调整。

## MC 联动

安装并配置鹊桥互通插件后，在管理页选择 `server_name`、开启 MC 联动，填写该游戏签到插件支持的**控制台指令**。本插件不会假设服务器安装了哪一种签到插件。

每行一条指令，可用 `{player}`（绑定游戏名）、`{points}`（本次积分）、`{user_id}`（账号 ID）。指令由管理员配置，不能由群消息传入；不需要开头的 `/`。玩家名验证为 1–16 位字母、数字或下划线。

LiteSignIn 示例：`litesignin click {player}`。该命令通过现有鹊桥插件的 RCON 执行通道发送。

开启「必须绑定」后，未绑定玩家不会领取此次积分；关闭后可以领取积分，但跳过 MC 同步。MC 未启用时不要求绑定。绑定优先读取鹊桥已加载实例；实例未加载时只读其本地 `bindings.json`，不修改原文件。

数据库积分与外部 MC 无法成为同一个原子事务。积分成功后，即使 MC 不可用也保留积分，管理页显示 `failed` / `unknown`。`sent` 表示收到指令通道响应，并不保证游戏签到插件接受了命令，请结合响应文本检查。多条指令在遇到未确认结果时停止，不能自动重试整组。

进程重启后，尚未尝试发送的 `pending` 可以继续处理；发送中的 `sending` 转为 `unknown`，避免重复游戏奖励。对未知结果请先人工核查游戏记录。本版不提供盲目重试按钮。

## 数据与开发

数据保存在 `data/plugin_data/astrbot_plugin_checkin_points/checkin.sqlite3`。仓库不包含真实群号配置、绑定或积分数据库。备份时停止插件后复制此数据目录。

结构：`main.py` 为 AstrBot 入口与 Web API，`core.py` 为规则验证，`store.py` 为 SQLite 仓库，`pages/dashboard/` 为管理页，`tests/` 为离线测试。管理页使用 AstrBot 官方 bridge 和面板认证，不另设登录或暴露 RCON 密码。

```shell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

开发流程：`main` 保留基线，功能在 `feat/session-checkin` 开发，提交后通过 PR 评审合并。GitHub Actions 对 Python 3.11/3.12 执行离线测试。

参考：[AstrBot 官方插件模板](https://github.com/Soulter/helloworld)、[Plugin Views 文档](https://docs.astrbot.app/en/dev/star/guides/plugin-pages.html)。现有 [astrbot_qiandao](https://github.com/qgx1992/astrbot_qiandao) 和 [astrbot_plugin_sign](https://github.com/FengYing1314/astrbot_plugin_sign) 未覆盖所需会话 UI 和 MC 联动，因此本插件独立实现，没有复制其实现代码。
