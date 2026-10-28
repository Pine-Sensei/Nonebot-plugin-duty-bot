"""nonebot-plugin-duty-bot：值班排班 QQ 机器人插件。

启动顺序（spec）：读取配置 → 定位持久化数据目录 → 完成迁移与版本校验
（失败即拒绝启动）→ 注册定时任务 → 启动 Bot（由宿主完成）。
"""

from nonebot import get_driver, logger, require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_htmlkit")

from .config import Config  # noqa: E402
from .config import plugin_config  # noqa: E402

__plugin_meta__ = PluginMetadata(
    name="值班排班机器人",
    description="对接值班排班系统：QQ-学号绑定、个人排班查询、值班前提醒、每周预告与群播报",
    usage=(
        "私聊：/绑定 学号 姓名、/我的排班、/今日值班、/解绑、/帮助\n"
        "群内（配置群）：/今日值班\n"
        "管理员：/状态、/绑定列表、/绑定 QQ号 学号 姓名、/解绑 QQ号、/重播 [明日|每周]\n"
        "需在 .env 配置 dutybot_api_base_url、dutybot_api_key、dutybot_group_id、"
        "dutybot_admins 等配置项"
    ),
    type="application",
    homepage="https://github.com/duty-bot/nonebot-plugin-duty-bot",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

driver = get_driver()


def check_startup_config() -> None:
    """启动时一次性配置体检：关键项未配置仅记录警告，不阻断启动。"""
    if not plugin_config.dutybot_api_base_url or not plugin_config.dutybot_api_key:
        logger.warning("值班系统 API 未配置，绑定/查询/播报相关功能将跳过")
    if not plugin_config.dutybot_group_id:
        logger.warning("值班群号未配置，群播报功能将跳过")


@driver.on_startup
async def _startup() -> None:
    """启动准备：配置检查 → 存储迁移 → 发送队列 → 注册定时任务。"""
    check_startup_config()
    import nonebot_plugin_localstore as store
    from nonebot_plugin_apscheduler import scheduler

    from . import storage
    from .jobs import register_jobs
    from .sender import init_global

    db_file = store.get_plugin_data_file("dutybot.db")

    # 迁移失败或数据库版本高于代码版本时拒绝启动
    try:
        await storage.init_global(db_file)
    except Exception as e:
        logger.critical(f"数据库初始化失败，拒绝启动: {e}")
        raise SystemExit(1) from e

    init_global(
        private_per_min=plugin_config.dutybot_rate_private_per_min,
        group_per_min=plugin_config.dutybot_rate_group_per_min,
        max_size=plugin_config.dutybot_queue_max_size,
    )
    register_jobs(scheduler)
    logger.info("值班排班机器人插件启动完成")


@driver.on_shutdown
async def _shutdown() -> None:
    """终止处理：停止队列、关闭存储与 API 连接（队列不持久化）。"""
    from . import storage
    from .api import close_client
    from .sender import close_global

    await close_global()
    await storage.close_global()
    await close_client()


@driver.on_bot_connect
async def _bot_connected(bot) -> None:
    logger.info(f"Bot 连接建立: {bot.type}({bot.self_id})")


@driver.on_bot_disconnect
async def _bot_disconnected(bot) -> None:
    logger.info(f"Bot 连接断开: {bot.type}({bot.self_id})")


# 导入命令处理器（触发事件响应器注册）
from . import handlers  # noqa: E402,F401
