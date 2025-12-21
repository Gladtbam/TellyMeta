from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import MediaUser


class MediaRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, user_id: int) -> MediaUser | None:
        """通过ID获取 Media 用户
        Args:
            user_id (int): MediaUser 用户ID
        Returns:
            MediaUser | None: 如果找到用户则返回用户对象，否则返回None
        """
        return await self.session.get(MediaUser, user_id)

    async def get_by_media_id(self, media_id: str) -> MediaUser | None:
        """通过MediaUser ID获取 MediaUser 用户
         Args:
            media_id (str): Media 用户的Media ID
         Returns:
            MediaUser | None: 如果找到用户则返回用户对象，否则返回None
        """
        stmt = select(MediaUser).where(MediaUser.media_id == media_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, user_id: int, media_id: str, media_name: str, expires_at: int) -> MediaUser:
        """创建新的 Media 用户
        Args:
            user_id (int): 关联的Telegram用户ID
            media_id (str): Media 用户的 Media ID
            media_name (str): Media 用户名
        Returns:
            MediaUser: 创建的 Media 用户对象
        """
        media_user = MediaUser(
            id=user_id,
            media_id=media_id,
            media_name=media_name,
            expires_at=datetime.now() + timedelta(days=expires_at),
        )
        self.session.add(media_user)
        await self.session.commit()
        return media_user

    async def delete(self, media_user: MediaUser) -> None:
        """删除 Media 用户
        Args:
            media_user (MediaUser): 需要删除的 Media 用户对象
        """
        await self.session.delete(media_user)

    async def find_expired_for_ban(self) -> Sequence[MediaUser]:
        """查找所有过期且未被封禁的 Media 用户，并进行封禁
        Returns:
            Sequence[ Media ]: 过期且未被封禁的 Media 用户列表
        """
        stmt = (update(MediaUser).where(
            MediaUser.expires_at < datetime.now(),
            MediaUser.is_banned.is_(False)
        )
        .values(is_banned=True)
        .returning(MediaUser))   # 返回被更新的 Media 对象
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalars().all()

    async def find_ban(self) -> Sequence[MediaUser]:
        """查找所有被封禁的 Media 用户，并进行删除
        Returns:
            Sequence[ MediaUser ]: 被封禁的 Media 用户列表
        """
        stmt = (delete(MediaUser).where(
            MediaUser.delete_at < datetime.now(),
            MediaUser.is_banned.is_(True)
        ).returning(MediaUser)) # 返回被删除的对象
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalars().all()

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
