from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import BotConfiguration, LibraryBindingModel


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

    async def get_all_library_bindings(self) -> dict[str, LibraryBindingModel]:
        """获取所有媒体库绑定配置"""
        stmt = select(BotConfiguration).where(BotConfiguration.key.like('binding:%'))
        result = await self.session.execute(stmt)
        bindings = {}
        for config in result.scalars().all():
            model = LibraryBindingModel.from_db_config(config)
            bindings[model.library_name] = model
        return bindings

    async def get_library_binding(self, library_name: str) -> LibraryBindingModel:
        """通过媒体库名称获取媒体库绑定配置"""
        key = f'binding:{library_name}'
        config = await self.session.get(BotConfiguration, key)
        if config:
            return LibraryBindingModel.from_db_config(config)
        return LibraryBindingModel(library_name=library_name)

    async def set_library_binding(self, binding: LibraryBindingModel) -> BotConfiguration:
        """设置媒体库绑定配置"""
        key = binding.to_config_key()
        value = binding.to_config_value()
        setting = await self.session.get(BotConfiguration, key)

        if not setting:
            setting = BotConfiguration(key=key, value=value)
            self.session.add(setting)
        else:
            setting.value = value
        await self.session.commit()
        await self.session.refresh(setting)
        return setting
