import logging
import re

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import BotConfiguration

logger = logging.getLogger(__name__)

class ConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_settings(self, key: str, default: str | None = None) -> str | None:
        """通过键获取配置值，如果配置不存在则返回默认值
        Args:
            key (str): 配置键
            default (str | None): 默认值，可选
        Returns:
            str | None: 如果找到配置则返回配置值，否则返回默认值
        """
        value = await self.session.get(BotConfiguration, key)
        return value.value if value else default

    async def set_settings(self, key: str, value: str) -> BotConfiguration:
        """设置配置值，如果配置不存在则创建新配置
        Args:
            key (str): 配置键
            value (str): 配置值
        """
        setting = await self.session.get(BotConfiguration, key)

        if setting:
            # 更新现有配置
            setting.value = value
        else:
            # 创建新配置
            setting = BotConfiguration(key=key, value=value)
            self.session.add(setting)

        await self.session.commit()
        await self.session.refresh(setting)
        return setting
