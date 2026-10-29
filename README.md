# nonebot-plugin-duty-bot

值班排班 QQ 机器人插件。对接值班排班系统的机器人 HTTP API，提供 QQ-学号绑定、个人排班查询、值班前提醒、每周排班预告与群播报。

仅支持 OneBot V11 适配器。

## 安装

使用 NB-CLI（推荐）：

```bash
nb plugin install nonebot-plugin-duty-bot
```

或使用 pip：

```bash
pip install nonebot-plugin-duty-bot
```

然后在 NoneBot 入口加载插件（任选其一）：

```python
nonebot.load_plugin("nonebot_plugin_duty_bot")
```

或在 `pyproject.toml` 的 `[tool.nonebot]` 中：

```toml
[tool.nonebot]
plugins = ["nonebot_plugin_duty_bot"]
```

## 配置

在 `.env` / `.env.prod` 中配置（均可留空，插件零配置可加载，未配置的功能会跳过并记录警告）：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `dutybot_api_base_url` | str | `""` | 值班系统 Bot API 地址，如 `http://127.0.0.1:8787` |
| `dutybot_api_key` | str | `""` | Bearer 访问密钥（服务端 `BOT_API_KEY`） |
| `dutybot_group_id` | int | `0` | 值班群号（v1 仅支持单群） |
| `dutybot_admins` | set[int] | 空集合 | 管理员 QQ 白名单，如 `[10000, 10001]` |
| `dutybot_remind_interval` | int | `30` | 值班提醒轮询间隔（分钟） |
| `dutybot_remind_window` | int | `120` | 值班提醒窗口：班次开始前多少分钟内提醒 |
| `dutybot_personal_weekly_cron` | str | `"0 19 * * sun"` | 个人每周排班预告 cron（北京时间） |
| `dutybot_group_weekly_cron` | str | `"0 21 * * sun"` | 群每周排班预告 cron（北京时间） |
| `dutybot_tomorrow_cron` | str | `"0 21 * * *"` | 明日值班播报 cron（北京时间） |
| `dutybot_rate_private_per_min` | int | `20` | 私聊发送限流（条/分钟） |
| `dutybot_rate_group_per_min` | int | `10` | 群聊发送限流（条/分钟） |
| `dutybot_queue_max_size` | int | `200` | 发送队列积压上限，超过后丢弃最早的播报/预告类消息 |
| `dutybot_roster_cache_seconds` | int | `60` | 整体排班查询短时缓存时长（秒） |
| `dutybot_api_timeout` | float | `10.0` | 值班系统接口调用超时（秒） |

示例：

```ini
dutybot_api_base_url=http://127.0.0.1:8787
dutybot_api_key=your-bot-api-key
dutybot_group_id=123456789
dutybot_admins=[10000]
```

## 命令列表

命令前缀默认为 `/`（由宿主 `COMMAND_START` 配置决定，下文以 `/` 为例）。

普通用户（私聊）：

| 命令 | 说明 |
| --- | --- |
| `/绑定 学号 姓名` | 自助绑定（姓名须与排班中一致） |
| `/我的排班` | 查询本人全部排班（图片输出） |
| `/今日值班` | 查询本人今日排班（纯文本，无排班则回复"今日无排班"） |
| `/解绑` | 解除本人绑定 |
| `/帮助` | 查看菜单 |

群内（仅配置群响应，无需 @）：

| 命令 | 说明 |
| --- | --- |
| `/今日值班` | 今日值班名单（按班次聚合，图片输出；无班次纯文本） |
| `/帮助` | 通用菜单 |

管理员（私聊；`dutybot_admins` 白名单）：

| 命令 | 说明 |
| --- | --- |
| `/状态` | 运行体检：连接、接口探测、绑定数、任务最近结果与下次执行、进程时长 |
| `/绑定列表` | 查看全部绑定（分段展示） |
| `/绑定 QQ号 学号 姓名` | 人工绑定（无排班学生使用） |
| `/解绑 QQ号` | 解除指定绑定 |
| `/重播 [明日\|每周]` | 补发发送失败的群播报；无参自动选最近失败期 |

## 定时任务

均按北京时间执行，不并发，异常记录后不中断调度：

- **值班前提醒**：每 `dutybot_remind_interval` 分钟轮询，班次开始前 `dutybot_remind_window` 分钟内私聊提醒，逐班次去重（重启后窗口内允许重复一次）
- **个人每周排班预告**：`dutybot_personal_weekly_cron`（默认周日 19:00），私聊推送下周排班图片
- **群每周排班预告**：`dutybot_group_weekly_cron`（默认周日 21:00），下周排班按日期分组图片播报
- **明日值班播报**：`dutybot_tomorrow_cron`（默认每日 21:00），明日值班图片播报

## 部署注意事项

- 绑定关系与任务状态持久化在 SQLite 数据库（`dutybot.db`），数据目录经 nonebot-plugin-localstore 获取。容器部署需将 localstore 数据目录挂载为持久卷，否则重启后绑定丢失。
- 启动时自动执行数据库迁移并在迁移前备份原文件；数据库版本高于代码版本时拒绝启动。
- 发送队列不持久化，重启后清空；错过的提醒由轮询补上，错过的群播报可用 `/重播` 补发。
- 出站消息统一经发送队列串行发送，私聊/群聊分别限流；命令即时回复优先且不被丢弃，积压超限时丢弃最早的播报/预告类消息并记录日志。

## 开发

```bash
pip install -e ".[test]"
pytest
```
