from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.billing_anchor import (
    backfill_billing_cycle,
    calculate_registration_cycle,
    calculate_renewal_cycle,
)
from models.orm import MediaUser, ServerInstance
from repositories.config_repo import ConfigRepository


class MediaRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, user_id: int, server_id: int) -> MediaUser | None:
        """通过 ID 获取 Media 用户
        Args:
            user_id (int): Telegram 用户ID
            server_id (int): 媒体服务器 ID
        Returns:
            MediaUser | None: 如果找到用户则返回用户对象，否则返回None
        """
        return await self.session.get(MediaUser, (user_id, server_id))

    async def get_all_by_id(self, user_id: int) -> Sequence[MediaUser]:
        """获取指定 Telegram 用户的所有媒体账户
        Args:
            user_id (int): Telegram 用户ID
        Returns:
            Sequence[MediaUser]: 该用户的所有账户列表
        """
        stmt = select(MediaUser).where(MediaUser.id == user_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_all_by_server_id(self, server_id: int) -> Sequence[MediaUser]:
        """获取指定服务器上的所有媒体账户

        Args:
            server_id (int): 服务器 ID

        Returns:
            Sequence[MediaUser]: 该服务器的所有媒体账户列表
        """
        stmt = select(MediaUser).where(MediaUser.server_id == server_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_media_id(
        self, media_id: str, server_id: int | None = None
    ) -> MediaUser | None:
        """通过MediaUser ID获取 MediaUser 用户
        Args:
           media_id (str): Media 用户的Media ID
        Returns:
           MediaUser | None: 如果找到用户则返回用户对象，否则返回None
        """
        stmt = select(MediaUser).where(MediaUser.media_id == media_id)
        if server_id is not None:
            stmt = stmt.where(MediaUser.server_id == server_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        user_id: int,
        server_id: int,
        media_id: str,
        media_name: str,
        *,
        expiry_months: int = 1,
        now: datetime | None = None,
    ) -> MediaUser:
        """创建新的媒体账号，并按锚定自然月周期计算首期有效期与周期边界

        Args:
            user_id (int): 关联的 Telegram 用户 ID
            server_id (int): 服务器 ID
            media_id (str): 媒体账号在外部服务器上的 ID
            media_name (str): 媒体账号用户名
            expiry_months (int): 有效自然月数（默认 1，必须 >= 1）
            now (datetime | None): 可注入的当前时间（用于确定性测试）

        Returns:
            MediaUser: 创建的媒体账号对象
        """
        cycle = calculate_registration_cycle(months=expiry_months, now=now)
        media_user = MediaUser(
            id=user_id,
            server_id=server_id,
            media_id=media_id,
            media_name=media_name,
            expires_at=cycle.expires_at,
            anchor_day=cycle.anchor_day,
            cycle_started_at=cycle.cycle_started_at,
            next_reset_at=cycle.next_reset_at,
        )
        self.session.add(media_user)
        await self.session.commit()
        await self.session.refresh(media_user)
        return media_user

    async def delete(self, media_user: MediaUser) -> None:
        """删除 Media 用户
        Args:
            media_user (MediaUser): 需要删除的 Media 用户对象
        """
        await self.session.delete(media_user)
        await self.session.commit()

    async def find_expired_for_ban(self) -> Sequence[MediaUser]:
        """查找所有已过期且尚未标记为封禁的 Media 用户。

        仅执行查询，不在外部媒体服务器禁用成功前修改本地状态。

        Returns:
            Sequence[MediaUser]: 过期且未被封禁的 Media 用户列表
        """
        stmt = select(MediaUser).where(
            MediaUser.expires_at < datetime.now(),
            MediaUser.is_banned.is_(False),
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def find_auto_renew_candidates(
        self, deadline: datetime
    ) -> Sequence[MediaUser]:
        """查找在临近到期截止时间前满足自动保活初步条件的媒体账号列表

        Args:
            deadline: 临近到期截止时间点（通常为 ``now + pre_expiry_window``）

        Returns:
            开启自动保活、未封禁、所属服务器启用保活且观看时长已达门槛的账号列表
        """
        stmt = (
            select(MediaUser)
            .join(ServerInstance, MediaUser.server_id == ServerInstance.id)
            .where(
                MediaUser.auto_renew.is_(True),
                MediaUser.is_banned.is_(False),
                MediaUser.expires_at <= deadline,
                ServerInstance.is_enabled.is_(True),
                ServerInstance.enable_watch_time_renewal.is_(True),
                ServerInstance.watch_time_renewal_threshold_seconds > 0,
                MediaUser.cycle_watch_time_seconds
                >= ServerInstance.watch_time_renewal_threshold_seconds,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def mark_banned(
        self, media_user: MediaUser, delete_at: datetime | None = None
    ) -> MediaUser:
        """将 Media 用户标记为已封禁，并设置待删除时间。

        默认从到期时间起满 7 天后允许自动删除。

        Args:
            media_user (MediaUser): 需要标记封禁的 Media 用户对象
            delete_at (datetime | None): 自定义待删除时间，默认为 expires_at + 7 天

        Returns:
            MediaUser: 更新后的 Media 用户对象
        """
        media_user.is_banned = True
        if delete_at is not None:
            media_user.delete_at = delete_at
        else:
            grace_days = (await ConfigRepository(self.session).get_policy_config())[
                "account_delete_grace_days"
            ]
            media_user.delete_at = media_user.expires_at + timedelta(days=grace_days)
        await self.session.commit()
        await self.session.refresh(media_user)
        return media_user

    async def backfill_missing_delete_at(self) -> int:
        """为已封禁但缺少待删除时间的历史记录补全 delete_at。

        Returns:
            int: 本次补全的记录数量
        """
        stmt = select(MediaUser).where(
            MediaUser.is_banned.is_(True),
            MediaUser.delete_at.is_(None),
            MediaUser.expires_at < datetime.now(),
        )
        result = await self.session.execute(stmt)
        users = result.scalars().all()
        if not users:
            return 0
        grace_days = (await ConfigRepository(self.session).get_policy_config())[
            "account_delete_grace_days"
        ]
        for user in users:
            user.delete_at = user.expires_at + timedelta(days=grace_days)
        await self.session.commit()
        return len(users)

    async def find_ban(self) -> Sequence[MediaUser]:
        """查找所有已封禁且已到达待删除时间的 Media 用户。

        仅执行查询，必须先确认媒体服务器账户已删除或已不存在后再由调用方移除本地记录。

        Returns:
            Sequence[MediaUser]: 到达删除时间的被封禁 Media 用户列表
        """
        stmt = select(MediaUser).where(
            MediaUser.delete_at.is_not(None),
            MediaUser.delete_at <= datetime.now(),
            MediaUser.is_banned.is_(True),
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def backfill_missing_billing_anchors(
        self, *, now: datetime | None = None
    ) -> int:
        """批量为缺少锚定基准或周期边界的存量媒体账号按现有到期日平滑回填

        Args:
            now: 可注入的当前时间

        Returns:
            int: 本次补全的媒体账号记录数量
        """
        stmt = select(MediaUser).where(
            or_(
                MediaUser.anchor_day.is_(None),
                MediaUser.cycle_started_at.is_(None),
                MediaUser.next_reset_at.is_(None),
            )
        )
        result = await self.session.execute(stmt)
        users = result.scalars().all()
        if not users:
            return 0

        current_now = now or datetime.now()
        for user in users:
            anchor_day, cycle_started_at, next_reset_at = backfill_billing_cycle(
                current_expires_at=user.expires_at,
                anchor_day=user.anchor_day,
                current_cycle_started_at=user.cycle_started_at,
                current_next_reset_at=user.next_reset_at,
                now=current_now,
            )
            user.anchor_day = anchor_day
            user.cycle_started_at = cycle_started_at
            user.next_reset_at = next_reset_at
        await self.session.commit()
        return len(users)

    async def extend_anchored_cycle(
        self,
        media_user: MediaUser,
        months: int,
        *,
        now: datetime | None = None,
    ) -> MediaUser:
        """按锚定自然月周期延长媒体账号有效期、清零本期保活时长并保留历史总时长

        Args:
            media_user: 需要续期的媒体账号对象
            months: 延长的有效自然月数（>= 1）
            now: 可注入的当前时间

        Returns:
            MediaUser: 更新后的媒体账号对象
        """
        cycle = calculate_renewal_cycle(
            current_expires_at=media_user.expires_at,
            anchor_day=media_user.anchor_day,
            current_cycle_started_at=media_user.cycle_started_at,
            current_next_reset_at=media_user.next_reset_at,
            months=months,
            now=now,
        )
        media_user.anchor_day = cycle.anchor_day
        media_user.cycle_started_at = cycle.cycle_started_at
        media_user.next_reset_at = cycle.next_reset_at
        media_user.expires_at = cycle.expires_at
        media_user.is_banned = False  # 续期时自动解封
        media_user.delete_at = None  # 清除删除时间
        # 仅重置本期保活计数与亚秒余数，绝不清除历史累计总时长 total_watch_time_seconds
        media_user.cycle_watch_time_seconds = 0
        media_user.watch_time_remainder_ms = 0
        await self.session.commit()
        await self.session.refresh(media_user)
        return media_user

    async def set_auto_renew(self, media_user: MediaUser, enabled: bool) -> MediaUser:
        """持久化更新媒体账号的自动保活开关状态

        Args:
            media_user: 目标媒体账号对象
            enabled: 目标开关状态

        Returns:
            刷新后的 ``MediaUser`` 对象
        """
        media_user.auto_renew = enabled
        await self.session.commit()
        await self.session.refresh(media_user)
        return media_user

    async def extend_expiry(self, media_user: MediaUser, days: int) -> MediaUser:
        """延长 Media 用户的过期时间
        Args:
            media_user ( Media ): 需要延长过期时间的 Media 用户对象
            days (int): 延长的天数
        Returns:
            MediaUser: 更新后的 Media 用户对象
        """
        base_date = max(media_user.expires_at, datetime.now())
        media_user.expires_at = base_date + timedelta(days=days)
        media_user.is_banned = False  # 续期时自动解封
        media_user.delete_at = None  # 清除删除时间
        await self.session.commit()
        await self.session.refresh(media_user)
        return media_user

    async def ban(self, user: MediaUser | Sequence[MediaUser]) -> None:
        """封禁 Media 用户
        Args:
            user (MediaUser | Sequence[MediaUser]): 需要封禁的Emby用户对象或用户列表
        """
        if isinstance(user, MediaUser):
            user = [user]
        for u in user:
            u.is_banned = True
        await self.session.commit()
