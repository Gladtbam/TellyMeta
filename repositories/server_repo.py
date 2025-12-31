from collections.abc import Sequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models.orm import ServerInstance

class ServerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, name: str, server_type: str, url: str, api_key: str, priority: int = 0) -> ServerInstance:
        """添加服务器"""
        instance = ServerInstance(
            name=name,
            server_type=server_type,
            url=url,
            api_key=api_key,
            priority=priority
        )
        self.session.add(instance)
        await self.session.commit()
        await self.session.refresh(instance)
        return instance

    async def get_all(self) -> Sequence[ServerInstance]:
        """获取所有服务器"""
        stmt = select(ServerInstance).order_by(ServerInstance.priority.desc())
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_all_enabled(self) -> Sequence[ServerInstance]:
        """获取已启用的服务器"""
        stmt = select(ServerInstance).where(
            ServerInstance.is_enabled.is_(True)).order_by(ServerInstance.priority.desc())
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_type(self, server_type: str) -> Sequence[ServerInstance]:
        """根据类型获取服务器 (Sonarr/Radarr/Emby...)"""
        stmt = select(ServerInstance).where(
            ServerInstance.server_type == server_type,
            ServerInstance.is_enabled.is_(True)
        ).order_by(ServerInstance.priority.desc())
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_id(self, server_id: int) -> ServerInstance | None:
        """通过 ID 获取服务器"""
        return await self.session.get(ServerInstance, server_id)

    async def delete(self, server_id: int) -> None:
        """删除服务器"""
        server = await self.get_by_id(server_id)
        if server:
            await self.session.delete(server)
            await self.session.commit()

    async def update_basic_info(self, server_id: int, **kwargs) -> ServerInstance | None:
        """更新服务器基础信息 (name, url, api_key)
        Args:
            server_id (int): 服务器 ID
            **kwargs: 要更新的字段，如 name='NewName', url='http://...'
        """
        server = await self.get_by_id(server_id)
        if not server:
            return None

        for key, value in kwargs.items():
            if hasattr(server, key) and value is not None:
                setattr(server, key, value)

        await self.session.commit()
        await self.session.refresh(server)
        return server

    async def toggle_enabled(self, server_id: int) -> ServerInstance | None:
        """切换启用/禁用状态"""
        server = await self.get_by_id(server_id)
        if server:
            server.is_enabled = not server.is_enabled
            await self.session.commit()
            await self.session.refresh(server)
        return server

    async def update_policy_config(
        self,
        server_id: int,
        mode: str | None = None,
        count: int | None = None,
        time: str | None = None,
        external_url: str | None = None
    ) -> ServerInstance | None:
        """更新注册策略"""
        server = await self.get_by_id(server_id)
        if server:
            if mode is not None:
                server.registration_mode = mode
            if count is not None:
                server.registration_count_limit = count
            if time is not None:
                server.registration_time_limit = time
            if external_url is not None:
                server.registration_external_url = external_url
            await self.session.commit()
            await self.session.refresh(server)
        return server

    async def update_expiry_config(
        self,
        server_id: int,
        expiry_days: int | None = None,
        code_days: int | None = None
    ) -> ServerInstance | None:
        """更新有效期配置"""
        server = await self.get_by_id(server_id)
        if server:
            if expiry_days is not None:
                server.registration_expiry_days = expiry_days
            if code_days is not None:
                server.code_expiry_days = code_days
            await self.session.commit()
            await self.session.refresh(server)
        return server

    async def update_nsfw_config(
        self,
        server_id: int,
        enabled: bool | None = None,
        lib_ids: str | None = None,
        sub_lib_ids: str | None = None
    ) -> ServerInstance | None:
        """更新 NSFW 配置"""
        server = await self.get_by_id(server_id)
        if server:
            if enabled is not None:
                server.nsfw_enabled = enabled
            if lib_ids is not None:
                server.nsfw_library_ids = lib_ids
            if sub_lib_ids is not None:
                server.nsfw_sub_library_ids = sub_lib_ids
            await self.session.commit()
            await self.session.refresh(server)
        return server
