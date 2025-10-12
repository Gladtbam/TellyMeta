from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import Emby


class EmbyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, user_id: int) -> Emby | None:
        """通过ID获取Emby用户
        Args:
            user_id (int): Emby用户ID
        Returns:
            Emby | None: 如果找到用户则返回用户对象，否则返回None
        """
        return await self.session.get(Emby, user_id)

    async def get_by_emby_id(self, emby_id: str) -> Emby | None:
        """通过Emby ID获取Emby用户
         Args:
            emby_id (str): Emby用户的Emby ID
         Returns:
            Emby | None: 如果找到用户则返回用户对象，否则返回None
        """
        stmt = select(Emby).where(Emby.emby_id == emby_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, user_id: int, emby_id: str, emby_name: str) -> Emby:
        """创建新的Emby用户
        Args:
            user_id (int): 关联的Telegram用户ID
            emby_id (str): Emby用户的Emby ID
            emby_name (str): Emby用户名
        Returns:
            Emby: 创建的Emby用户对象
        """
        emby_user = Emby(
            id=user_id,
            emby_id=emby_id,
            emby_name=emby_name,
            expires_at=datetime.now() + timedelta(days=30),  # 默认30天有效期
        )
        self.session.add(emby_user)
        return emby_user

    async def delete(self, emby_user: Emby) -> None:
        """删除Emby用户
        Args:
            emby_user (Emby): 需要删除的Emby用户对象
        """
        await self.session.delete(emby_user)

    async def find_expired_for_ban(self) -> Sequence[Emby]:
        """查找所有过期且未被封禁的Emby用户，并进行封禁
        Returns:
            Sequence[Emby]: 过期且未被封禁的Emby用户列表
        """
        stmt = (update(Emby).where(
            Emby.expires_at < datetime.now(),
            Emby.is_banned.is_(False)
        )
        .values(is_banned=True)
        .returning(Emby))   # 返回被更新的 Emby 对象
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalars().all()

    async def find_ban(self) -> Sequence[Emby]:
        """查找所有被封禁的Emby用户，并进行删除
        Returns:
            Sequence[Emby]: 被封禁的Emby用户列表
        """
        stmt = (delete(Emby).where(
            Emby.delete_at < datetime.now(),
            Emby.is_banned.is_(True)
        ).returning(Emby)) # 返回被删除的对象
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalars().all()

    async def extend_expiry(self, emby_user: Emby, days: int) -> Emby:
        """延长Emby用户的过期时间
        Args:
            emby_user (Emby): 需要延长过期时间的Emby用户对象
            days (int): 延长的天数
        Returns:
            Emby: 更新后的Emby用户对象
        """
        base_date = max(emby_user.expires_at, datetime.now())
        emby_user.expires_at = base_date + timedelta(days=days)
        emby_user.is_banned = False  # 续期时自动解封
        emby_user.delete_at = None  # 清除删除时间
        await self.session.commit()
        await self.session.refresh(emby_user)
        return emby_user

    async def ban(self, user: Emby | Sequence[Emby]) -> None:
        """封禁Emby用户
        Args:
            user (Emby | Sequence[Emby]): 需要封禁的Emby用户对象或用户列表
        """
        if isinstance(user, Emby):
            user = [user]
        for u in user:
            u.is_banned = True
        await self.session.commit()
