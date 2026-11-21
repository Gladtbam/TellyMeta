import secrets
from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.economy_policy import normalize_pricing_mode
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
        registration_expiry_months: int = 1,
        allow_request: bool = False,
        request_points_mode: str = "fixed",
        request_points_fixed: int = 0,
        request_points_ratio: float = 0.0,
        enable_watch_time: bool = True,
        enable_watch_time_renewal: bool = False,
        watch_time_renewal_threshold_seconds: int = 0,
        enable_traffic: bool = False,
        enable_traffic_alert: bool = True,
        enable_traffic_auto_suspend: bool = False,
        traffic_quota_bytes: int = 0,
        allow_traffic_pack: bool = False,
        traffic_pack_points_mode: str = "fixed",
        traffic_pack_points_fixed: int = 50,
        traffic_pack_points_ratio: float = 0.5,
        traffic_pack_size_gib: float = 10.0,
        default_user_policy: str | None = None,
        qb_username: str | None = None,
        qb_torrent_limit: int = 43200,
        qb_server_id: int | None = None,
    ) -> ServerInstance:
        """添加服务器并持久化有效期自然月数、独立求片/流量包定价与播放时长/流量配额配置

        Args:
            name: 服务器显示名称。
            server_type: 服务器类型标识（如 ``emby``、``jellyfin``）。
            url: 服务器访问基础 URL。
            api_key: 接口鉴权密钥或密码。
            priority: 列表排序优先级。
            registration_expiry_months: 注册及续期有效自然月数（最小 1）。
            allow_request: 是否开启求片功能（新服默认关闭）。
            request_points_mode: 求片定价模式（``fixed`` / ``ratio``）。
            request_points_fixed: 求片固定消耗积分（最小 0）。
            request_points_ratio: 求片动态比例系数（最小 0.0）。
            enable_watch_time: 是否开启有效播放时长采集。
            enable_watch_time_renewal: 是否开启时长保活续期。
            watch_time_renewal_threshold_seconds: 时长保活门槛秒数。
            enable_traffic: 是否开启流量采集。
            enable_traffic_alert: 是否开启超流通知。
            enable_traffic_auto_suspend: 是否开启超流自动停用。
            traffic_quota_bytes: 月基础流量配额字节数。
            allow_traffic_pack: 是否允许在该服兑换流量包（新服默认关闭）。
            traffic_pack_points_mode: 流量包定价模式（``fixed`` / ``ratio``）。
            traffic_pack_points_fixed: 流量包固定消耗积分（最小 1）。
            traffic_pack_points_ratio: 流量包相对续期门槛的比例系数（大于 0）。
            traffic_pack_size_gib: 单包流量容量（GiB，大于 0）。
            qb_username: qBittorrent 登录用户名。
            qb_torrent_limit: qBittorrent 活跃种子限制数。
            qb_server_id: 关联的 qBittorrent 服务器 ID。

        Returns:
            持久化并刷新后的 ``ServerInstance`` 实例。
        """
        token = secrets.token_urlsafe(32)
        normalized_req_mode = normalize_pricing_mode(request_points_mode)
        normalized_pack_mode = normalize_pricing_mode(traffic_pack_points_mode)
        instance = ServerInstance(
            name=name,
            server_type=server_type,
            url=url,
            api_key=api_key,
            priority=priority,
            webhook_token=token,
            registration_expiry_months=max(1, int(registration_expiry_months)),
            allow_request=allow_request,
            request_points_mode=normalized_req_mode,
            request_points_fixed=max(0, int(request_points_fixed)),
            request_points_ratio=max(0.0, float(request_points_ratio)),
            enable_watch_time=enable_watch_time,
            enable_watch_time_renewal=enable_watch_time_renewal,
            watch_time_renewal_threshold_seconds=max(
                0, int(watch_time_renewal_threshold_seconds)
            ),
            enable_traffic=enable_traffic,
            enable_traffic_alert=enable_traffic_alert,
            enable_traffic_auto_suspend=enable_traffic_auto_suspend,
            traffic_quota_bytes=max(0, int(traffic_quota_bytes)),
            allow_traffic_pack=allow_traffic_pack,
            traffic_pack_points_mode=normalized_pack_mode,
            traffic_pack_points_fixed=max(1, int(traffic_pack_points_fixed)),
            traffic_pack_points_ratio=max(1e-6, float(traffic_pack_points_ratio)),
            traffic_pack_size_gib=max(1e-6, float(traffic_pack_size_gib)),
            default_user_policy=default_user_policy,
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

        nullable_fields = {"qb_server_id", "qb_username", "default_user_policy"}
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
