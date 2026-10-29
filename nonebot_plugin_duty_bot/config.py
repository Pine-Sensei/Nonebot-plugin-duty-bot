"""插件配置模型。

所有配置项均带默认值，保证插件在未配置时也能正常导入（零配置可加载）；
需要配置的功能在运行时检查配置完整性并跳过，记录警告日志。
cron 配置在加载时预校验，非法时带字段名直接报错（fail fast）。
"""

from apscheduler.triggers.cron import CronTrigger
from nonebot import get_plugin_config
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from .timeutil import BEIJING


class Config(BaseModel):
    """值班排班机器人插件配置。"""

    dutybot_api_base_url: str = ""
    """值班系统 Bot API 地址，如 http://127.0.0.1:8787"""

    dutybot_api_key: str = ""
    """Bearer 访问密钥（服务端 BOT_API_KEY）"""

    dutybot_group_id: int = 0
    """值班群号（v1 仅支持单群）"""

    dutybot_admins: set[int] = Field(default_factory=set)
    """管理员 QQ 白名单"""

    dutybot_remind_interval: int = 30
    """值班提醒轮询间隔（分钟）"""

    dutybot_remind_window: int = 120
    """值班提醒窗口：班次开始前多少分钟内提醒"""

    dutybot_personal_weekly_cron: str = Field(default="0 19 * * sun", validate_default=True)
    """个人每周排班预告 cron（默认周日 19:00，北京时间）"""

    dutybot_group_weekly_cron: str = Field(default="0 21 * * sun", validate_default=True)
    """群每周排班预告 cron（默认周日 21:00，北京时间）"""

    dutybot_tomorrow_cron: str = Field(default="0 21 * * *", validate_default=True)
    """明日值班播报 cron（默认每日 21:00，北京时间）"""

    dutybot_rate_private_per_min: int = 20
    """私聊发送限流：每分钟最多发送条数"""

    dutybot_rate_group_per_min: int = 10
    """群聊发送限流：每分钟最多发送条数"""

    dutybot_queue_max_size: int = 200
    """发送队列积压上限，超过后丢弃最早的播报/预告类消息"""

    dutybot_roster_cache_seconds: int = 60
    """整体排班查询短时缓存时长（秒）"""

    dutybot_api_timeout: float = 10.0
    """值班系统接口调用超时（秒）"""

    @field_validator("dutybot_remind_interval")
    @classmethod
    def _check_interval(cls, v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("dutybot_remind_interval 必须为正整数（分钟）")

    @field_validator("dutybot_remind_window")
    @classmethod
    def _check_window(cls, v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("dutybot_remind_window 必须为正整数（分钟）")

    @field_validator(
        "dutybot_personal_weekly_cron",
        "dutybot_group_weekly_cron",
        "dutybot_tomorrow_cron",
    )
    @classmethod
    def _check_cron(cls, v: str, info: ValidationInfo) -> str:
        """用与任务注册一致的解析方式预校验 cron，非法时报出字段名。"""
        try:
            CronTrigger.from_crontab(v, timezone=BEIJING)
        except ValueError as e:
            raise ValueError(f"{info.field_name} 不是合法的 cron 表达式（{v}）: {e}") from e
        return v


plugin_config = get_plugin_config(Config)
