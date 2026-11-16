import secrets
from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import ServerInstance

# 定义一个唯一的哨兵对象，用于区分 "未传参" 和 "传了 None"
_UNSET = object()


class ServerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        name: str,
        server_type: str,
        url: str,
        api_key: str,
        priority: int = 0,
        enable_traffic: bool = False,
        enable_traffic_alert: bool = True,
        traffic_quota_bytes: int = 0,
        qb_username: str | None = None,
        qb_torrent_limit: int = 43200,
        qb_server_id: int | None = None,
    ) -> ServerInstance:
        """添加服务器并持久化基础与流量配额配置"""
        token = secrets.token_urlsafe(32)
        instance = ServerInstance(
            name=name,
            server_type=server_type,
            url=url,
            api_key=api_key,
            priority=priority,
            webhook_token=token,
            enable_traffic=enable_traffic,
            enable_traffic_alert=enable_traffic_alert,
            traffic_quota_bytes=max(0, int(traffic_quota_bytes)),
            qb_username=qb_username,
            qb_torrent_limit=qb_torrent_limit,
            qb_server_id=qb_server_id,
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
        stmt = (
            select(ServerInstance)
            .where(ServerInstance.is_enabled.is_(True))
            .order_by(ServerInstance.priority.desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_type(self, server_type: str) -> Sequence[ServerInstance]:
        """根据类型获取服务器 (Sonarr/Radarr/Emby...)"""
        stmt = (
            select(ServerInstance)
            .where(
                ServerInstance.server_type == server_type,
                ServerInstance.is_enabled.is_(True),
            )
            .order_by(ServerInstance.priority.desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_id(
        self, server_id: int, *, fresh: bool = False
    ) -> ServerInstance | None:
        """通过 ID 获取服务器

        Args:
            server_id: 媒体服务器 ID
            fresh: 为 ``True`` 时强制从数据库刷新配置，绕过会话身份映射缓存

        Returns:
            ``ServerInstance`` 实例，不存在时返回 ``None``
        """
        if not fresh:
            return await self.session.get(ServerInstance, server_id)
        stmt = (
            select(ServerInstance)
            .where(ServerInstance.id == server_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_token(self, token: str) -> ServerInstance | None:
        """通过 Webhook Token 获取服务器"""
        stmt = select(ServerInstance).where(ServerInstance.webhook_token == token)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete(self, server_id: int) -> None:
        """删除服务器"""
        server = await self.get_by_id(server_id)
        if server:
            await self.session.delete(server)
            await self.session.commit()

    async def update_basic_info(
        self, server_id: int, **kwargs
    ) -> ServerInstance | None:
        """更新服务器基础信息 (name, url, api_key)
        Args:
            server_id (int): 服务器 ID
            **kwargs: 要更新的字段，如 name='NewName', url='http://...'
        """
        server = await self.get_by_id(server_id)
        if not server:
            return None

        nullable_fields = {"qb_server_id", "qb_username"}
        for key, value in kwargs.items():
            if hasattr(server, key) and (value is not None or key in nullable_fields):
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
        external_url: str | None = None,
        external_parser: str | None = None,
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
            if external_parser is not None:
                server.registration_external_parser = external_parser
            await self.session.commit()
            await self.session.refresh(server)
        return server

    async def update_expiry_config(
        self,
        server_id: int,
        code_days: int | None = None,
        expiry_months: int | None = None,
    ) -> ServerInstance | None:
        """更新服务器实例的有效期配置

        Args:
            server_id: 目标服务器实例 ID
            code_days: 激活码有效天数（为 None 时不修改）
            expiry_months: 账号有效自然月数（为 None 时不修改）

        Returns:
            更新并提交数据库事务后的 ``ServerInstance`` 对象；服务器不存在时返回 ``None``
        """
        server = await self.get_by_id(server_id)
        if server:
            if expiry_months is not None:
                server.registration_expiry_months = expiry_months
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
        sub_lib_ids: str | None = None,
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

    async def update_notify_config(
        self,
        server_id: int,
        notify_topic_id: int | object | None = _UNSET,
        request_notify_topic_id: int | object | None = _UNSET,
    ) -> ServerInstance | None:
        """更新通知话题配置"""
        server = await self.get_by_id(server_id)
        if server:
            if notify_topic_id is not _UNSET:
                server.notify_topic_id = cast(int | None, notify_topic_id)
            if request_notify_topic_id is not _UNSET:
                server.request_notify_topic_id = cast(
                    int | None, request_notify_topic_id
                )

            await self.session.commit()
            await self.session.refresh(server)
        return server
