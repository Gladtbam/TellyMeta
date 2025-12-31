from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import BotConfiguration, LibraryBindingModel


class ConfigRepository:
    cache: dict[str, str] = {}

    # System Feature Keys
    KEY_ENABLE_POINTS = "system:enable_points"
    KEY_ENABLE_VERIFICATION = "system:enable_verification"
    KEY_ENABLE_REQUESTMEDIA = "system:enable_requestmedia"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @classmethod
    async def load_all_to_cache(cls, session: AsyncSession):
        """加载所有配置到缓存，并初始化默认配置"""

        # Load all existing configs
        stmt = select(BotConfiguration)
        result = await session.execute(stmt)
        configs = result.scalars().all()

        cls.cache.clear()
        for config in configs:
            cls.cache[config.key] = config.value

        # 定义默认值以确保它们存在于数据库（持久性）和缓存中
        # 字典格式：{key:value}
        persistent_defaults = {
            cls.KEY_ENABLE_POINTS: "true",
            cls.KEY_ENABLE_VERIFICATION: "true",
            cls.KEY_ENABLE_REQUESTMEDIA: "true",
        }

        new_items = []
        for key, value in persistent_defaults.items():
            if key not in cls.cache:
                cls.cache[key] = value
                new_items.append(BotConfiguration(key=key, value=value))

        if new_items:
            session.add_all(new_items)
            await session.commit()

    async def get_settings(self, key: str, default: str | None = None) -> str | None:
        """通过键获取配置值，优先读取缓存
        Args:
            key (str): 配置键
            default (str | None): 默认值，可选
        Returns:
            str | None: 如果找到配置则返回配置值，否则返回默认值
        """
        if key in self.cache:
            return self.cache[key]

        value = await self.session.get(BotConfiguration, key)
        if value:
            self.cache[key] = value.value
            return value.value

        return default

    async def set_settings(self, key: str, value: str) -> BotConfiguration:
        """设置配置值，同时更新缓存
        Args:
            key (str): 配置键
            value (str): 配置值
        """
        # Update Cache immediately
        self.cache[key] = value

        setting = await self.session.get(BotConfiguration, key)

        if setting:
            setting.value = value
        else:
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
